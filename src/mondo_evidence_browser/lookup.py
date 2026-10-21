"""Per-record lookups for the sign-off service, in small static shards.

The service has to check a sign-off against the record the curator saw: that it
exists, its record version and score rule version, and its statement. At 600K+
records it must not load the whole index for that, so the build writes those few
fields per record into 4,096 gzipped JSON shards (``lookup/<3 hex digits>.json.gz``,
the shard chosen by a hash of the record id) next to the index, plus
``lookup/meta.json``. The
service fetches only the shard it needs, from a folder or a URL (Hugging Face, Pages),
and caches it for a few minutes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from mondo_evidence_browser.index import EvidenceIndex

SHARD_WIDTH = 3
"""Hex digits of the shard name: 16**3 = 4,096 shards, ~150 records each at 600K."""

SCORE_FIELDS = ("value", "band", "rule_id", "rule_version")


def shard_of(key: str) -> str:
    """The shard holding a record id.

    >>> shard_of("pkt_9c18e79339f2")
    'b0a'
    """
    return hashlib.sha256(key.encode()).hexdigest()[:SHARD_WIDTH]


def lookup_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    """The fields of a record a sign-off needs, in the record's own shape."""
    score = record.get("inclusion_score") or {}
    statement = record.get("statement") or {}
    return {
        "id": record.get("id"),
        "record_version": record["record_version"],
        "inclusion_score": {k: score[k] for k in SCORE_FIELDS if k in score},
        "statement": {
            **{k: statement[k] for k in ("id", "subject", "object") if k in statement},
            "predicate": {"code": (statement.get("predicate") or {}).get("code")},
        },
    }


class LookupWriter:
    """Collects entries during a build (on disk, not in memory) and writes the shards."""

    def __init__(self) -> None:
        """Start an empty collection in a temporary database."""
        self._dir = tempfile.TemporaryDirectory()
        self._conn = sqlite3.connect(Path(self._dir.name) / "lookup.db")
        self._conn.execute(
            "CREATE TABLE entries (shard TEXT, key TEXT PRIMARY KEY, entry TEXT) WITHOUT ROWID"
        )

    def add(self, key: str, record: Mapping[str, Any]) -> None:
        """Add (or replace) the entry for one record."""
        self._conn.execute(
            "INSERT OR REPLACE INTO entries VALUES (?, ?, ?)",
            (shard_of(key), key, json.dumps(lookup_entry(record), separators=(",", ":"))),
        )

    def write(self, out_dir: Path, meta: Mapping[str, Any]) -> int:
        """Write every shard and ``meta.json`` into ``out_dir``; returns the shard count."""
        if out_dir.exists():
            for old in [*out_dir.glob("*.json"), *out_dir.glob("*.json.gz")]:
                old.unlink()
        out_dir.mkdir(parents=True, exist_ok=True)
        self._conn.execute("CREATE INDEX ix_shard ON entries (shard)")
        shards = 0
        current: str | None = None
        entries: dict[str, Any] = {}

        def flush() -> None:
            if current is not None:
                text = json.dumps(entries, separators=(",", ":"), sort_keys=True)
                compressed = gzip.compress(text.encode(), compresslevel=9, mtime=0)
                (out_dir / f"{current}.json.gz").write_bytes(compressed)

        for shard, key, entry in self._conn.execute(
            "SELECT shard, key, entry FROM entries ORDER BY shard, key"
        ):
            if shard != current:
                flush()
                shards += 1
                current, entries = shard, {}
            entries[key] = json.loads(entry)
        flush()
        (out_dir / "meta.json").write_text(json.dumps(dict(meta), indent=1))
        self._conn.close()
        self._dir.cleanup()
        return shards


class ShardLookup:
    """Read lookups from a folder or a base URL, caching each shard for ``ttl`` seconds."""

    def __init__(
        self,
        base: str | Path,
        transport: httpx.BaseTransport | None = None,
        ttl: float = 300.0,
    ) -> None:
        """Read shards under ``base`` (a folder, or a URL ending in ``lookup/``)."""
        self.base = base
        self.ttl = ttl
        self._client = (
            httpx.Client(transport=transport, timeout=20, follow_redirects=True)
            if isinstance(base, str)
            else None
        )
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _fetch(self, name: str) -> dict[str, Any]:
        if isinstance(self.base, Path):
            path = self.base / name
            content = path.read_bytes() if path.exists() else b"{}"
        else:
            assert self._client is not None
            response = self._client.get(self.base.rstrip("/") + "/" + name)
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            content = response.content
        # A host may already have unpacked a .gz file on the way (Content-Encoding).
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)
        data: dict[str, Any] = json.loads(content)
        return data

    def _shard(self, shard: str) -> dict[str, Any]:
        cached = self._cache.get(shard)
        if cached is not None and time.monotonic() - cached[0] < self.ttl:
            return cached[1]
        data = self._fetch(f"{shard}.json.gz")
        self._cache[shard] = (time.monotonic(), data)
        return data

    def get(self, key: str) -> dict[str, Any] | None:
        """The lookup entry for a record id, or None if there is no such record."""
        entry: dict[str, Any] | None = self._shard(shard_of(key)).get(key)
        return entry

    def refresh(self, key: str) -> None:
        """Forget the cached shard of ``key``, e.g. after a rebuild."""
        self._cache.pop(shard_of(key), None)

    def meta(self) -> dict[str, Any]:
        """Build metadata written next to the shards."""
        return self._fetch("meta.json")

    def note_status(self, record: Mapping[str, Any], status: str) -> None:
        """Lookups are rebuilt with the site; nothing to update here."""


class IndexSource:
    """A local index as a record source (``just serve``): updates statuses in place."""

    def __init__(self, index: EvidenceIndex) -> None:
        """Wrap an index opened with its view spec."""
        self.index = index

    def get(self, key: str) -> dict[str, Any] | None:
        """The full record."""
        return self.index.get(key)

    def refresh(self, key: str) -> None:
        """A local index is always current."""

    def meta(self) -> dict[str, Any]:
        """The index metadata."""
        return self.index.meta()

    def note_status(self, record: Mapping[str, Any], status: str) -> None:
        """Write the new review status into the index."""
        self.index.put({**record, "review_status": status})
