"""Per-record lookups: what the sign-off service needs, one small shard at a time.

At 600K+ records the service must never load the whole index; it fetches the one
shard holding the record it is asked about.
"""

import gzip
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from mondo_evidence_browser.build import BuildConfig, build
from mondo_evidence_browser.decisions import DecisionLog, Refusal, sign_off
from mondo_evidence_browser.index import EvidenceIndex
from mondo_evidence_browser.lookup import ShardLookup, lookup_entry, shard_of
from mondo_evidence_browser.registry import Registry
from mondo_evidence_browser.viewspec import build_view_spec

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = Path(__file__).parent.parent / "config"
PACKET = "pkt_9c18e79339f2"
CARBERRY = "ORCID:0000-0002-1825-0097"


@pytest.fixture
def built(tmp_path: Path) -> Path:
    build(
        BuildConfig(
            store_dir=FIXTURES,
            mondo_obo=FIXTURES / "mondo_subset.obo",
            config_dir=CONFIG,
            index_path=tmp_path / "evidence.db",
            site_dir=tmp_path / "site",
            as_of=date(2026, 10, 20),
        )
    )
    return tmp_path


def test_shards_are_stable_and_bounded() -> None:
    assert shard_of(PACKET) == shard_of(PACKET)
    assert len(shard_of(PACKET)) == 3
    assert all(c in "0123456789abcdef" for c in shard_of(PACKET))


def test_build_writes_lookups_next_to_the_index(built: Path) -> None:
    lookup_dir = built / "site" / "data" / "lookup"
    meta = json.loads((lookup_dir / "meta.json").read_text())
    assert meta["record_count"] == 23
    assert meta["schema_version"] == "0.1.0"
    index = EvidenceIndex(built / "evidence.db")
    lookup = ShardLookup(lookup_dir)
    for (key,) in index.conn.execute("SELECT key FROM records"):
        record = index.get(key)
        assert record is not None
        assert lookup.get(key) == lookup_entry(record)
    assert lookup.get("pkt_nope") is None
    shards = list(lookup_dir.glob("*.json.gz"))
    assert 0 < len(shards) <= 4096
    assert gzip.decompress(shards[0].read_bytes()).startswith(b"{")


def test_lookup_entry_keeps_only_what_a_sign_off_needs(built: Path) -> None:
    record = EvidenceIndex(built / "evidence.db").get(PACKET)
    assert record is not None
    entry = lookup_entry(record)
    assert set(entry) == {"id", "record_version", "inclusion_score", "statement"}
    assert set(entry["inclusion_score"]) <= {"value", "band", "rule_id", "rule_version"}
    assert entry["statement"]["predicate"] == {"code": "oio:hasExactSynonym"}
    assert "has_evidence_lines" not in entry


def _http(lookup_dir: Path, seen: list[str]) -> httpx.MockTransport:
    """A static host serving the lookup folder (like Hugging Face or Pages)."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        path = lookup_dir / request.url.path.rsplit("/", 1)[-1]
        if not path.exists():
            return httpx.Response(404)
        return httpx.Response(200, content=path.read_bytes())

    return httpx.MockTransport(handler)


def test_lookups_over_http_are_cached(built: Path) -> None:
    lookup_dir = built / "site" / "data" / "lookup"
    seen: list[str] = []
    lookup = ShardLookup("https://data.example.org/meb/lookup/", transport=_http(lookup_dir, seen))
    first = lookup.get(PACKET)
    assert first is not None and first["id"] == PACKET
    assert lookup.get(PACKET) == first
    assert seen == [f"/meb/lookup/{shard_of(PACKET)}.json.gz"]
    assert lookup.meta()["record_count"] == 23
    lookup.refresh(PACKET)
    lookup.get(PACKET)
    assert seen.count(f"/meb/lookup/{shard_of(PACKET)}.json.gz") == 2
    assert lookup.get("pkt_nope") is None


def _sign(source: Any, log: DecisionLog, seen_version: str, rule_version: str) -> Any:
    return sign_off(
        source,
        log,
        Registry.from_dir(CONFIG),
        build_view_spec()["form"],
        record_id=PACKET,
        seen_version=seen_version,
        seen_rule_version=rule_version,
        values={"outcome": "accepted"},
        orcid=CARBERRY,
        name="Josiah Carberry",
        now=datetime(2026, 10, 21, 12, tzinfo=UTC),
    )


def test_sign_off_works_from_lookups_alone(built: Path, tmp_path: Path) -> None:
    lookup = ShardLookup(built / "site" / "data" / "lookup")
    entry = lookup.get(PACKET)
    assert entry is not None
    log = DecisionLog(tmp_path / "decisions.jsonl")
    decision = _sign(
        log=log,
        source=lookup,
        seen_version=entry["record_version"],
        rule_version=entry["inclusion_score"]["rule_version"],
    )
    assert isinstance(decision, dict) and decision["packet_id"] == PACKET
    assert decision["statement"]["object"] == "Arginase deficiency"


def test_a_stale_cached_shard_is_refetched_before_refusing(built: Path, tmp_path: Path) -> None:
    """After a rebuild the browser may show a newer version than a cached shard."""
    lookup_dir = built / "site" / "data" / "lookup"
    seen: list[str] = []
    lookup = ShardLookup("https://data.example.org/lookup/", transport=_http(lookup_dir, seen))
    entry = lookup.get(PACKET)
    assert entry is not None
    shard = lookup_dir / f"{shard_of(PACKET)}.json.gz"
    rebuilt = json.loads(gzip.decompress(shard.read_bytes()))
    rebuilt[PACKET]["record_version"] = "v_newerversion000"
    shard.write_bytes(gzip.compress(json.dumps(rebuilt).encode()))
    log = DecisionLog(tmp_path / "decisions.jsonl")
    decision = _sign(lookup, log, "v_newerversion000", entry["inclusion_score"]["rule_version"])
    assert isinstance(decision, dict)
    refusal = _sign(lookup, log, "v_someoneelses0000", entry["inclusion_score"]["rule_version"])
    assert isinstance(refusal, Refusal) and refusal.status_code == 409


def test_shards_read_the_same_when_a_host_unpacks_them(built: Path) -> None:
    """Some hosts send .gz files with Content-Encoding: gzip, so the client unpacks them."""
    lookup_dir = built / "site" / "data" / "lookup"

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        data = (lookup_dir / name).read_bytes()
        if name.endswith(".gz"):  # already unpacked by the host on the way
            return httpx.Response(200, content=gzip.decompress(data))
        return httpx.Response(200, content=data)

    lookup = ShardLookup("https://data.example.org/lookup/", transport=httpx.MockTransport(handler))
    entry = lookup.get(PACKET)
    assert entry is not None and entry["id"] == PACKET
