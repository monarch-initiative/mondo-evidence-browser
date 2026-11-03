"""Building BrowserRecords: packet + Mondo membership + inclusion score + review status."""

import copy
import math
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from linkml.validator import validate

from mondo_evidence_browser.model import packet_version
from mondo_evidence_browser.mondo import MondoRelease
from mondo_evidence_browser.records import RecordBuilder
from mondo_evidence_browser.registry import Registry
from mondo_evidence_browser.score import ScoreRule
from mondo_evidence_browser.viewspec import SCHEMA_PATH

AS_OF = date(2026, 10, 20)
PACKET = "pkt_9c18e79339f2"  # arginase deficiency, exact synonym "Arginase deficiency"


@pytest.fixture(scope="module")
def builder(config_dir: Path) -> RecordBuilder:
    return RecordBuilder(
        rule=ScoreRule.from_yaml(config_dir / "rules" / "inclusion-score.yaml"),
        registry=Registry.from_dir(config_dir),
        mondo=MondoRelease.from_obo(Path(__file__).parent / "fixtures" / "mondo_subset.obo"),
        as_of=AS_OF,
    )


@pytest.fixture(scope="module")
def config_dir() -> Path:  # module-scoped copy of the conftest fixture
    return Path(__file__).parent.parent / "config"


def test_record_adds_the_computed_slots(
    builder: RecordBuilder, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    raw = raw_by_id[PACKET]
    record = builder.build(raw)
    assert record is not None
    assert record["id"] == PACKET
    assert record["has_evidence_lines"] == raw["has_evidence_lines"]
    assert record["mondo_membership"] == "IN_MONDO"
    assert record["mondo_release"] == "releases/2026-10-06"
    assert record["claim_type"] == "C003"
    assert record["sources"] == ["doid", "ordo"]
    assert record["review_status"] == "unreviewed"
    assert record["record_version"] == packet_version(raw)


def test_inclusion_score_uses_schema_slot_names(
    builder: RecordBuilder, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    record = builder.build(raw_by_id[PACKET])
    assert record is not None
    score = record["inclusion_score"]
    assert score["value"] == pytest.approx(math.tanh(4 * (0.68 + 0.72) / 2))
    assert score["rule_version"] == "0.1.0-uncalibrated"
    assert (score["claim_prior"], score["scaling"]) == (0.0, 4.0)
    assert score["log_odds"] == pytest.approx(5.6)
    doid, ordo = score["components"]
    assert doid["source"] == "doid" and doid["strength"] == 0.8 and doid["source_prior"] == 0.85
    assert doid["confidence_basis"] == "PRIOR" and doid["counted"] is True
    assert ordo["weight"] == pytest.approx(0.72)


def test_candidates_are_statements_mondo_does_not_have(
    builder: RecordBuilder, raw_packets: list[dict[str, Any]]
) -> None:
    candidate = next(p for p in raw_packets if p["statement"]["object"] == "Arginase 1 deficiency")
    record = builder.build(candidate)
    assert record is not None and record["mondo_membership"] == "CANDIDATE"


def test_review_status_follows_the_latest_decision(
    builder: RecordBuilder, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    raw = raw_by_id[PACKET]
    signed = {
        "outcome": "accepted",
        "packet_version": packet_version(raw),
        "score": {"rule_version": "0.1.0-uncalibrated"},
    }
    record = builder.build(raw, decision=signed)
    assert record is not None and record["review_status"] == "accepted"
    old = {**signed, "packet_version": "v_0000000000000000"}
    record = builder.build(raw, decision=old)
    assert record is not None and record["review_status"] == "stale"


def test_unscored_claim_types_have_no_value(
    builder: RecordBuilder, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    raw = copy.deepcopy(raw_by_id[PACKET])
    raw["statement"]["predicate"]["code"] = "RO:0004003"
    record = builder.build(raw)
    assert record is not None
    assert "value" not in record["inclusion_score"]
    assert record["inclusion_score"]["band"] == "unscored"
    assert record["mondo_membership"] == "NOT_CHECKED"


def test_unrecognised_packets_are_not_records(builder: RecordBuilder) -> None:
    assert builder.build({"hasEvidenceLines": []}) is None


def test_every_record_is_valid_against_the_browser_schema(
    builder: RecordBuilder, raw_packets: list[dict[str, Any]]
) -> None:
    for raw in raw_packets:
        record = builder.build(raw)
        report = validate(record, str(SCHEMA_PATH), "BrowserRecord")
        assert not report.results, (raw["id"], [r.message for r in report.results])


def test_schema_validation_catches_a_missing_required_slot(
    builder: RecordBuilder, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    record = builder.build(raw_by_id[PACKET])
    assert record is not None
    del record["review_status"]
    report = validate(record, str(SCHEMA_PATH), "BrowserRecord")
    assert any("review_status" in r.message for r in report.results)


def test_score_has_no_worked_calculation_slot(
    builder: RecordBuilder, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    """The formula is explained once on the list page; records carry only the numbers."""
    record = builder.build(raw_by_id[PACKET])
    assert record is not None and "calculation" not in record["inclusion_score"]


def test_the_score_says_in_plain_words_what_the_evidence_amounts_to(
    builder: RecordBuilder, raw_by_id: dict[str, Any]
) -> None:
    """The verdict a curator reads first: independent sources by name, score, caveats."""
    record = builder.build(raw_by_id[PACKET])
    assert record is not None
    assert record["inclusion_score"]["summary"] == (
        "Supported by 2 independent sources (Disease Ontology and Orphanet (ORDO)). "
        "Score 0.99, strong support. Rule not yet calibrated."
    )


def test_the_verdict_counts_dependent_sources_once(builder: RecordBuilder) -> None:
    from mondo_evidence_browser.records import verdict

    class Line:
        def __init__(self, source: str, cluster: str, direction: int, included: bool) -> None:
            self.source, self.cluster, self.direction, self.included = (
                source,
                cluster,
                direction,
                included,
            )

    class Result:
        value, band, rule_version = 0.5, "moderate support", "1.0.0"
        lines = [
            Line("omim", "omim", 1, True),
            Line("doid", "omim", 1, False),  # derives from OMIM: same cluster
            Line("wikipedia", "wikipedia", -1, True),
        ]

    assert verdict(Result(), builder.registry) == (
        "Supported by 1 independent source (OMIM); disputed by 1 (Wikipedia). "
        "Score 0.50, moderate support."
    )


def test_claim_types_have_readable_labels() -> None:
    from mondo_evidence_browser.viewspec import build_view_spec

    titles = {e["value"]: e["title"] for e in build_view_spec()["enums"]["ClaimType"]}
    assert titles["C003"] == "Synonym"
