"""The SQLite index is laid out from the view spec: columns, indexes, search, meta."""

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from mondo_evidence_browser.index import EvidenceIndex, build_index, column_value
from mondo_evidence_browser.mondo import MondoRelease
from mondo_evidence_browser.records import RecordBuilder
from mondo_evidence_browser.registry import Registry
from mondo_evidence_browser.score import ScoreRule
from mondo_evidence_browser.viewspec import build_view_spec

CONFIG = Path(__file__).parent.parent / "config"
PACKET = "pkt_9c18e79339f2"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return build_view_spec()


@pytest.fixture(scope="module")
def records(raw_packets_module: list[dict[str, Any]]) -> list[dict[str, Any]]:
    builder = RecordBuilder(
        rule=ScoreRule.from_yaml(CONFIG / "rules" / "inclusion-score.yaml"),
        registry=Registry.from_dir(CONFIG),
        mondo=MondoRelease.from_obo(Path(__file__).parent / "fixtures" / "mondo_subset.obo"),
        as_of=date(2026, 10, 20),
    )
    return [r for r in (builder.build(p) for p in raw_packets_module) if r]


@pytest.fixture(scope="module")
def raw_packets_module() -> list[dict[str, Any]]:
    with open(Path(__file__).parent / "fixtures" / "packets_000.jsonl") as f:
        return [json.loads(line) for line in f]


@pytest.fixture
def db(tmp_path: Path, spec: dict[str, Any], records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "evidence.db"
    build_index(path, records, spec, {"mondo_release": "releases/2026-10-06"})
    return path


def test_column_value_follows_the_path(records: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    record = next(r for r in records if r["id"] == PACKET)
    by_name = {c["name"]: c for c in spec["columns"]}
    assert column_value(record, by_name["statement__predicate__label"]) == "exact synonym"
    assert column_value(record, by_name["sources"]) == '["doid", "ordo"]'
    assert column_value(record, by_name["inclusion_score__value"]) == pytest.approx(0.99263, 1e-4)


def test_table_columns_and_types_come_from_the_spec(db: Path, spec: dict[str, Any]) -> None:
    conn = sqlite3.connect(db)
    info = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(records)")}
    ranks = {f"__rank__{c['name']}" for c in spec["columns"] if c["sortable"]}
    assert set(info) == {"rowid", "key", "tab"} | {c["name"] for c in spec["columns"]} | ranks
    assert info["inclusion_score__value"] == "REAL"
    assert info["statement__subject_label"] == "TEXT"


def test_sortable_and_filter_columns_are_indexed_per_tab(db: Path, spec: dict[str, Any]) -> None:
    conn = sqlite3.connect(db)
    indexed = {
        tuple(r[2] for r in conn.execute(f"PRAGMA index_info({name!r})"))
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'records' "
            "AND name LIKE 'ix_%'"
        )
    }
    meta = EvidenceIndex(db).meta()
    for column in spec["columns"]:
        if column["filter"]:
            assert ("tab", column["name"]) in indexed, column["name"]
        if column["sortable"]:
            assert ("tab", meta["rank_columns"][column["name"]]) in indexed, column["name"]


def test_sort_ranks_give_the_column_order_per_tab(db: Path, spec: dict[str, Any]) -> None:
    conn = sqlite3.connect(db)
    ranks = EvidenceIndex(db).meta()["rank_columns"]
    for column in [c for c in spec["columns"] if c["sortable"]]:
        name, rank = column["name"], ranks[column["name"]]
        for tab in ("IN_MONDO", "CANDIDATE"):
            by_value = [
                k
                for (k,) in conn.execute(
                    f'SELECT key FROM records WHERE tab = ? ORDER BY "{name}", rowid', (tab,)
                )
            ]
            by_rank = [
                k
                for (k,) in conn.execute(
                    f'SELECT key FROM records WHERE tab = ? ORDER BY "{rank}"', (tab,)
                )
            ]
            assert by_rank == by_value, name
            ranks_in_tab = [
                r for (r,) in conn.execute(f'SELECT "{rank}" FROM records WHERE tab = ?', (tab,))
            ]
            assert sorted(ranks_in_tab) == list(range(1, len(by_value) + 1))


def test_any_page_is_a_range_read_of_an_index(db: Path) -> None:
    """At 600K rows skipping to page N would read N pages of index first; ranks don't."""
    conn = sqlite3.connect(db)
    rank = EvidenceIndex(db).meta()["rank_columns"]["inclusion_score__value"]
    plan = " ".join(
        row[3]
        for row in conn.execute(
            f'EXPLAIN QUERY PLAN SELECT key FROM records WHERE tab = ? AND "{rank}" > ? '
            f'AND "{rank}" <= ? ORDER BY "{rank}" DESC',
            ("IN_MONDO", 4950, 5000),
        )
    )
    assert EvidenceIndex(db).meta()["rank_indexes"]["inclusion_score__value"] in plan
    assert "TEMP B-TREE" not in plan


def test_meta_has_counts_and_filter_values(db: Path, records: list[dict[str, Any]]) -> None:
    meta = EvidenceIndex(db).meta()
    counts = meta["tab_counts"]
    assert sum(counts.values()) == len(records) == 23
    assert counts["IN_MONDO"] == sum(r["mondo_membership"] == "IN_MONDO" for r in records)
    claim_types = meta["filter_values"]["CANDIDATE"]["claim_type"]
    assert claim_types == {"C003": counts["CANDIDATE"]}
    assert meta["mondo_release"] == "releases/2026-10-06"
    assert meta["schema_version"] == "0.1.0"
    assert meta["record_count"] == 23


def test_search_finds_records_by_label(db: Path) -> None:
    conn = sqlite3.connect(db)
    keys = {
        key
        for (key,) in conn.execute(
            "SELECT key FROM records WHERE rowid IN "
            "(SELECT rowid FROM search WHERE search MATCH ?)",
            ('"arginase"*',),
        )
    }
    assert PACKET in keys


def test_records_round_trip(db: Path, records: list[dict[str, Any]]) -> None:
    index = EvidenceIndex(db)
    original = next(r for r in records if r["id"] == PACKET)
    assert index.get(PACKET) == original
    assert index.get("pkt_nope") is None


def test_updating_a_record_refreshes_its_columns(db: Path, spec: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="view spec"):
        EvidenceIndex(db).put({"id": PACKET})
    index = EvidenceIndex(db, spec)
    record = index.get(PACKET)
    assert record is not None
    index.put({**record, "review_status": "accepted"})
    assert index.get(PACKET)["review_status"] == "accepted"  # type: ignore[index]
    (status,) = (
        sqlite3.connect(db)
        .execute("SELECT review_status FROM records WHERE key = ?", (PACKET,))
        .fetchone()
    )
    assert status == "accepted"


def test_database_is_ready_for_http_range_reads(db: Path) -> None:
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("PRAGMA page_size").fetchone()[0] == 4096


def test_rows_are_stored_in_the_default_order(db: Path, spec: dict[str, Any]) -> None:
    """So a page of the default view is a run of neighbouring rows: few, large reads."""
    conn = sqlite3.connect(db)
    column = spec["default_sort"]["column"]
    for tab in ("IN_MONDO", "CANDIDATE"):
        by_rowid = [
            k
            for (k,) in conn.execute("SELECT key FROM records WHERE tab = ? ORDER BY rowid", (tab,))
        ]
        values = [
            v
            for (v,) in conn.execute(
                f'SELECT "{column}" FROM records WHERE tab = ? ORDER BY rowid', (tab,)
            )
        ]
        assert values == sorted(values, reverse=True)
        assert len(by_rowid) == len(set(by_rowid))
    assert EvidenceIndex(db).meta()["clustered"] == spec["default_sort"]


def _fts_count(db: Path, match: str) -> int:
    (n,) = (
        sqlite3.connect(db)
        .execute("SELECT count(*) FROM search WHERE search MATCH ?", (match,))
        .fetchone()
    )
    return int(n)


def test_tab_and_filters_are_answerable_from_the_search_index(
    db: Path, records: list[dict[str, Any]]
) -> None:
    """A search with filters, and its count, never needs the (scattered) table rows."""
    tokens = EvidenceIndex(db).meta()["fts_tokens"]
    tab_col, tab_tokens = tokens["tab"]["column"], tokens["tab"]["tokens"]
    status = tokens["filters"]["review_status"]
    match = (
        f"{tab_col} : {tab_tokens['CANDIDATE']} AND "
        f'{status["column"]} : {status["tokens"]["unreviewed"]} AND "arginase"*'
    )
    expected = sum(
        1
        for r in records
        if r["mondo_membership"] == "CANDIDATE"
        and r["review_status"] == "unreviewed"
        and "arginase" in (r["statement"].get("subject_label") or "").lower()
    )
    assert expected > 0
    assert _fts_count(db, match) == expected
    assert _fts_count(db, f"{tab_col} : {tab_tokens['IN_MONDO']}") == sum(
        r["mondo_membership"] == "IN_MONDO" for r in records
    )


def test_counts_for_every_filter_combination_are_stored(
    db: Path, records: list[dict[str, Any]], spec: dict[str, Any]
) -> None:
    meta = EvidenceIndex(db).meta()
    columns = meta["filter_columns"]
    assert columns == [c["name"] for c in spec["columns"] if c["filter"]]
    by_column = {c["name"]: c for c in spec["columns"]}
    for tab, combos in meta["filter_combos"].items():
        assert sum(n for _, n in combos) == meta["tab_counts"][tab]
        for values, n in combos:
            matching = [
                r
                for r in records
                if r["mondo_membership"] == tab
                and all(
                    str(column_value(r, by_column[c])) == v
                    for c, v in zip(columns, values, strict=True)
                    if v is not None
                )
            ]
            assert n == len(matching)
