"""The SQLite index behind the browser, laid out entirely from the view spec.

- ``records``: one row per record with the key, the tab value and one column per
  spec column (named ``a__b__c`` after its slot path). Filter columns are indexed on
  ``(tab, column)``. Every sortable column also gets a rank column: the row's position
  in that column's order within its tab (ties by rowid), indexed on ``(tab, rank)``.
  Any page of any order is then a range read of one index, whatever its depth, which
  is what keeps 600K+ records lazy over HTTP range requests.
- ``record_json``: the full record as zlib-compressed JSON, read one at a time for
  the detail view (the browser inflates it with ``DecompressionStream``).
- ``search``: a contentless FTS5 table over the spec's search columns, plus hidden
  token columns for the tab and every filter column. A search with filters, and its
  count, is answered by this index alone, without reading the (scattered) rows.
- Rows are stored in the default sort order within each tab, so a page of the default
  view is a run of neighbouring rows (few, large reads), and search results, which
  come back in row order, are already in that order.
- ``meta``: counts per tab, per filter value and per combination of filter values,
  the search tokens, the clustered order, schema version, build provenance.

The file uses rollback journaling and 4 KiB pages so it can be read lazily by
sql.js-httpvfs on a static host.
"""

from __future__ import annotations

import json
import os
import sqlite3
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SQL_TYPES = {"number": "REAL", "integer": "INTEGER", "boolean": "INTEGER"}


TAB_TOKEN_COLUMN = "zz_tab"


def filter_token_column(name: str) -> str:
    """The hidden search-index column holding a filter column's value tokens.

    >>> filter_token_column("review_status")
    'zz_f_review_status'
    """
    return f"zz_f_{name}"


def rank_column(name: str) -> str:
    """The rank column for a sortable column.

    >>> rank_column("inclusion_score__value")
    '__rank__inclusion_score__value'
    """
    return f"__rank__{name}"


def _q(name: str) -> str:
    """Quote an identifier taken from the schema.

    >>> _q('statement__subject')
    '"statement__subject"'
    """
    return '"' + name.replace('"', '""') + '"'


def column_value(record: Mapping[str, Any], column: Mapping[str, Any]) -> Any:
    """The value of a spec column in a record, as stored in SQLite.

    Multivalued values are stored as JSON arrays; booleans as 0/1.

    >>> column_value({"a": {"b": [1, 2]}}, {"path": ["a", "b"], "multivalued": True})
    '[1, 2]'
    """
    value: Any = record
    for step in column["path"]:
        if not isinstance(value, Mapping):
            return None
        value = value.get(step)
    if value is None:
        return None
    if column["multivalued"]:
        return json.dumps(value if isinstance(value, list) else [value], ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Mapping | list):
        return json.dumps(value, ensure_ascii=False)
    return value


def _pack(record: Mapping[str, Any]) -> bytes:
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return zlib.compress(text.encode(), 9)


def _unpack(blob: bytes) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(zlib.decompress(blob))
    return result


class _Layout:
    """Table definition and row values for one view spec."""

    def __init__(self, spec: Mapping[str, Any]) -> None:
        self.spec = spec
        self.columns = list(spec["columns"])
        self.key = spec["key"]
        self.tab_slot = spec["tabs"]["slot"] if spec.get("tabs") else None
        self.search = [c["name"] for c in self.columns if c["search"]]
        self.sortable = [c["name"] for c in self.columns if c["sortable"]]
        self.filters = [c for c in self.columns if c["filter"]]
        self.positions = {
            name: i for i, name in enumerate(["key", "tab", *[c["name"] for c in self.columns]])
        }

    @property
    def fts_columns(self) -> list[str]:
        """Search columns, then the token columns for the tab and each filter."""
        return [
            *self.search,
            TAB_TOKEN_COLUMN,
            *[filter_token_column(c["name"]) for c in self.filters],
        ]

    def schema_sql(self) -> str:
        cols = ",\n  ".join(
            [f"{_q(c['name'])} {SQL_TYPES.get(c['kind'], 'TEXT')}" for c in self.columns]
            + [f"{_q(rank_column(name))} INTEGER" for name in self.sortable]
        )
        statements = [
            "CREATE TABLE records (\n  rowid INTEGER PRIMARY KEY,\n  key TEXT NOT NULL UNIQUE,\n"
            f"  tab TEXT,\n  {cols}\n)",
            "CREATE TABLE record_json (rowid INTEGER PRIMARY KEY, json BLOB NOT NULL)",
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        ]
        for c in self.columns:
            if c["filter"]:
                statements.append(
                    f"CREATE INDEX {_q('ix_' + c['name'])} ON records (tab, {_q(c['name'])})"
                )
        names = ", ".join(_q(n) for n in self.fts_columns)
        statements.append(
            f"CREATE VIRTUAL TABLE search USING fts5({names}, content='', "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        statements.append(
            "CREATE TABLE staging (key TEXT PRIMARY KEY, tab TEXT, sort_value, "
            "params TEXT NOT NULL, json BLOB NOT NULL)"
        )
        return ";\n".join(statements) + ";"

    def row(self, record: Mapping[str, Any]) -> dict[str, Any]:
        row = {"key": record[self.key], "tab": record.get(self.tab_slot) if self.tab_slot else None}
        for c in self.columns:
            row[c["name"]] = column_value(record, c)
        return row

    def upsert_sql(self) -> str:
        names = ["key", "tab", *[c["name"] for c in self.columns]]
        cols = ", ".join(_q(n) for n in names)
        values = ", ".join(f":{i}" for i in range(len(names)))
        updates = ", ".join(f"{_q(n)} = excluded.{_q(n)}" for n in names[1:])
        return (
            f"INSERT INTO records ({cols}) VALUES ({values}) "
            f"ON CONFLICT(key) DO UPDATE SET {updates} RETURNING rowid"
        )

    def params(self, record: Mapping[str, Any]) -> dict[str, Any]:
        row = self.row(record)
        return {str(i): value for i, value in enumerate(row.values())}


def build_index(
    path: Path,
    records: Iterable[Mapping[str, Any]],
    spec: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> int:
    """Write a fresh index for ``records`` at ``path``; returns the record count.

    The file is built next to ``path`` and moved into place when complete.
    """
    layout = _Layout(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    building = path.with_name(path.name + ".building")
    building.unlink(missing_ok=True)
    conn = sqlite3.connect(building)
    conn.executescript(
        "PRAGMA page_size = 4096; PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;"
        + layout.schema_sql()
    )
    filters = layout.filters
    default = spec.get("default_sort")
    by_name = {c["name"]: c for c in layout.columns}
    tab_counts: Counter[str] = Counter()
    combos: Counter[tuple[str, tuple[str | None, ...]]] = Counter()
    for record in records:
        params = layout.params(record)
        key, tab = params["0"], params["1"] or ""
        sort_value = column_value(record, by_name[default["column"]]) if default else None
        conn.execute(
            "INSERT OR REPLACE INTO staging VALUES (?, ?, ?, ?, ?)",
            (key, tab, sort_value, json.dumps(params), _pack(record)),
        )
        values = tuple(None if (v := column_value(record, c)) is None else str(v) for c in filters)
        tab_counts[tab] += 1
        combos[(tab, values)] += 1

    # Opaque, exact tokens for the tab and the filter values in the search index.
    tab_tokens = {tab: f"t{i}" for i, tab in enumerate(sorted(tab_counts))}
    filter_tokens: dict[str, dict[str, str]] = {}
    for position, c in enumerate(filters):
        seen_values = sorted({value for _, v in combos if (value := v[position]) is not None})
        filter_tokens[c["name"]] = {value: f"v{i}" for i, value in enumerate(seen_values)}

    # Write rows in the default order within each tab.
    direction = "DESC" if default and default["direction"] == "desc" else "ASC"
    order = f"tab, sort_value {direction}, key" if default else "rowid"
    insert = layout.upsert_sql()
    fts_names = ", ".join(_q(n) for n in layout.fts_columns)
    fts_insert = (
        f"INSERT INTO search (rowid, {fts_names}) "
        f"VALUES (?, {', '.join('?' * len(layout.fts_columns))})"
    )
    writer = conn.cursor()
    for params_json, blob in conn.execute(f"SELECT params, json FROM staging ORDER BY {order}"):
        params = json.loads(params_json)
        (rowid,) = writer.execute(insert, params).fetchone()
        writer.execute("INSERT INTO record_json (rowid, json) VALUES (?, ?)", (rowid, blob))
        tab = params["1"] or ""
        fts_values = [params[str(layout.positions[name])] for name in layout.search]
        fts_values.append(tab_tokens[tab])
        for c in filters:
            value = params[str(layout.positions[c["name"]])]
            fts_values.append(
                filter_tokens[c["name"]].get(str(value)) if value is not None else None
            )
        writer.execute(fts_insert, (rowid, *fts_values))
    conn.execute("DROP TABLE staging")
    filter_values: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    for (tab, values), n in combos.items():
        for c, value in zip(filters, values, strict=True):
            if value is not None:
                filter_values[tab][c["name"]][value] += n
    for name in layout.sortable:
        rank = rank_column(name)
        conn.execute(
            f"UPDATE records SET {_q(rank)} = ranked.rn FROM ("
            f"SELECT rowid AS id, row_number() OVER "
            f"(PARTITION BY tab ORDER BY {_q(name)} ASC, rowid ASC) AS rn FROM records"
            f") AS ranked WHERE records.rowid = ranked.id"
        )
        conn.execute(f"CREATE INDEX {_q('ix_' + rank)} ON records (tab, {_q(rank)})")
    meta = {
        "schema_version": spec["schema"]["version"],
        "schema_name": spec["schema"]["name"],
        "record_count": sum(tab_counts.values()),
        "tab_counts": dict(tab_counts),
        "rank_columns": {name: rank_column(name) for name in layout.sortable},
        "rank_indexes": {name: "ix_" + rank_column(name) for name in layout.sortable},
        "filter_values": {
            t: {c: dict(v) for c, v in cols.items()} for t, cols in filter_values.items()
        },
        "filter_columns": [c["name"] for c in filters],
        "filter_combos": {
            tab: [[list(values), n] for (t, values), n in sorted(combos.items()) if t == tab]
            for tab in sorted(tab_counts)
        },
        "fts_tokens": {
            "tab": {"column": TAB_TOKEN_COLUMN, "tokens": tab_tokens},
            "filters": {
                c["name"]: {
                    "column": filter_token_column(c["name"]),
                    "tokens": filter_tokens[c["name"]],
                }
                for c in filters
            },
        },
        "clustered": dict(default) if default else None,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **provenance,
    }
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [(k, json.dumps(v)) for k, v in meta.items()],
    )
    conn.commit()
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("ANALYZE")
    conn.execute("VACUUM")
    conn.close()
    os.replace(building, path)
    return sum(tab_counts.values())


class EvidenceIndex:
    """Read records from a built index, and update them (sign-offs)."""

    def __init__(self, path: Path, spec: Mapping[str, Any] | None = None) -> None:
        """Open an index built by :func:`build_index`; ``spec`` is needed to update it."""
        if not path.exists():
            raise FileNotFoundError(f"no evidence index at {path}; run `just build` first")
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._layout = _Layout(spec) if spec is not None else None

    def meta(self) -> dict[str, Any]:
        """Build metadata, counts per tab and per filter value."""
        return {k: json.loads(v) for k, v in self.conn.execute("SELECT key, value FROM meta")}

    def get(self, key: str) -> dict[str, Any] | None:
        """The full record with this key."""
        row = self.conn.execute(
            "SELECT j.json FROM records r JOIN record_json j ON j.rowid = r.rowid WHERE r.key = ?",
            (key,),
        ).fetchone()
        return _unpack(row[0]) if row else None

    def put(self, record: Mapping[str, Any]) -> None:
        """Replace one record (its columns and JSON); used when a sign-off changes it."""
        if self._layout is None:
            raise ValueError("open the index with its view spec to update records")
        (rowid,) = self.conn.execute(
            self._layout.upsert_sql(), self._layout.params(record)
        ).fetchone()
        self.conn.execute("UPDATE record_json SET json = ? WHERE rowid = ?", (_pack(record), rowid))
        self.conn.commit()
