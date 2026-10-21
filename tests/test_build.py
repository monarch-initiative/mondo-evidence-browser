"""The build: packets + Mondo release + decision log -> index + static site."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from mondo_evidence_browser.build import BuildConfig, build
from mondo_evidence_browser.decisions import DecisionLog
from mondo_evidence_browser.index import EvidenceIndex
from mondo_evidence_browser.model import packet_version
from mondo_evidence_browser.viewspec import build_view_spec

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = Path(__file__).parent.parent / "config"
PACKET = "pkt_9c18e79339f2"


def config(tmp_path: Path, **overrides: Any) -> BuildConfig:
    values: dict[str, Any] = {
        "store_dir": FIXTURES,
        "mondo_obo": FIXTURES / "mondo_subset.obo",
        "config_dir": CONFIG,
        "index_path": tmp_path / "var" / "evidence.db",
        "site_dir": tmp_path / "site",
        "decisions_path": tmp_path / "decisions.jsonl",
        "as_of": date(2026, 10, 20),
    }
    values.update(overrides)
    return BuildConfig(**values)


def test_build_writes_a_complete_static_site(tmp_path: Path) -> None:
    report = build(config(tmp_path))
    assert report.records == 23 and report.skipped == 0
    assert sum(report.tab_counts.values()) == 23
    site = tmp_path / "site"
    for name in [
        "index.html",
        "record.html",
        "auth.html",
        "vendor/sql.js-httpvfs/index.js",
        "vendor/sql.js-httpvfs/sqlite.worker.js",
        "vendor/sql.js-httpvfs/sql-wasm.wasm",
        "vendor/sql.js-httpvfs/LICENSE",
        "data/evidence.db",
        "view-spec.json",
        "config.json",
        ".nojekyll",
    ]:
        assert (site / name).is_file(), name
    assert json.loads((site / "view-spec.json").read_text()) == build_view_spec()


def test_assets_are_versioned_by_content(tmp_path: Path) -> None:
    from mondo_evidence_browser.build import FRONTEND, assets_version

    build(config(tmp_path))
    site = tmp_path / "site"
    version = assets_version(FRONTEND / "assets")
    for name in ["app.css", "list.js", "record.js", "render.js", "db.js", "auth.js"]:
        assert (site / "assets" / version / name).is_file(), name
    for page in ["index.html", "record.html", "auth.html"]:
        html = (site / page).read_text()
        assert f"assets/{version}/" in html
        assert '"assets/app.css' not in html


def test_site_config_for_a_small_database(tmp_path: Path) -> None:
    build(config(tmp_path))
    site_config = json.loads((tmp_path / "site" / "config.json").read_text())
    assert site_config["db"] == {
        "serverMode": "full",
        "url": "data/evidence.db",
        "requestChunkSize": 4096,
    }
    assert site_config["service"] is None and site_config["orcid"] is None
    assert site_config["schema_version"] == "0.1.0"


def test_service_and_orcid_settings_reach_the_site(tmp_path: Path) -> None:
    build(
        config(
            tmp_path,
            service_url="https://signoff.example.org",
            orcid_client_id="APP-123",
            orcid_base_url="https://sandbox.orcid.org",
        )
    )
    site_config = json.loads((tmp_path / "site" / "config.json").read_text())
    assert site_config["service"] == {"url": "https://signoff.example.org"}
    assert site_config["orcid"] == {"clientId": "APP-123", "baseUrl": "https://sandbox.orcid.org"}


def test_decisions_set_the_review_status(tmp_path: Path, raw_by_id: dict[str, Any]) -> None:
    log = DecisionLog(tmp_path / "decisions.jsonl")
    log.append(
        {
            "decision_id": "dec_1",
            "packet_id": PACKET,
            "packet_version": packet_version(raw_by_id[PACKET]),
            "score": {"rule_version": "0.1.0-uncalibrated"},
            "outcome": "rejected",
            "decided_at": "2026-10-20T12:00:00+00:00",
        }
    )
    build(config(tmp_path))
    record = EvidenceIndex(tmp_path / "var" / "evidence.db").get(PACKET)
    assert record is not None and record["review_status"] == "rejected"


def test_unrecognised_packets_are_skipped_and_reported(
    tmp_path: Path, raw_packets: list[dict[str, Any]]
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    lines = [json.dumps(p) for p in raw_packets] + [json.dumps({"hasEvidenceLines": []})]
    (store / "packets_000.jsonl").write_text("\n".join(lines) + "\n")
    report = build(config(tmp_path, store_dir=store))
    assert (report.records, report.skipped) == (23, 1)
    assert report.skipped_examples == ["packets_000.jsonl:24"]


def test_empty_store_is_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="packets_"):
        build(config(tmp_path, store_dir=empty))


def _store_with(tmp_path: Path, packets: list[dict[str, Any]]) -> Path:
    store = tmp_path / "dupes"
    store.mkdir()
    (store / "packets_000.jsonl").write_text("\n".join(json.dumps(p) for p in packets) + "\n")
    return store


def test_two_statements_with_one_id_fail_the_build(
    tmp_path: Path, raw_packets: list[dict[str, Any]]
) -> None:
    """A stored Mondo link must never silently point at a different statement."""
    import copy

    from mondo_evidence_browser.build import DuplicateRecordError

    clash = copy.deepcopy(raw_packets[0])
    clash["statement"]["object"] = "a different synonym"
    store = _store_with(tmp_path, [*raw_packets, clash])
    with pytest.raises(DuplicateRecordError, match="different statements") as error:
        build(config(tmp_path, store_dir=store))
    assert raw_packets[0]["id"] in str(error.value)
    assert "packets_000.jsonl:1" in str(error.value) and "packets_000.jsonl:24" in str(error.value)


def test_the_same_statement_twice_fails_the_build(
    tmp_path: Path, raw_packets: list[dict[str, Any]]
) -> None:
    """Two packets for one statement means an unmerged store: evidence would be lost."""
    from mondo_evidence_browser.build import DuplicateRecordError

    store = _store_with(tmp_path, [*raw_packets, raw_packets[3]])
    with pytest.raises(DuplicateRecordError, match="appears twice"):
        build(config(tmp_path, store_dir=store))


def test_data_can_be_hosted_somewhere_else(tmp_path: Path) -> None:
    """At 600K+ records the index outgrows GitHub Pages; the site can point elsewhere."""
    data = tmp_path / "data-host"
    build(config(tmp_path, data_url="https://data.example.org/meb/", data_dir=data))
    site_config = json.loads((tmp_path / "site" / "config.json").read_text())
    assert site_config["db"]["url"] == "https://data.example.org/meb/evidence.db"
    assert (data / "evidence.db").is_file()
    assert not (tmp_path / "site" / "data").exists()


def test_data_url_needs_a_data_dir(tmp_path: Path) -> None:
    from mondo_evidence_browser.build import BuildError

    with pytest.raises(BuildError, match="data_dir"):
        build(config(tmp_path, data_url="https://data.example.org/meb/"))
