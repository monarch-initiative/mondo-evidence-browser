"""The D-063 inclusion score: S = tanh((L0 + k * sum over independence clusters of w) / 2)."""

import copy
import math
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from mondo_evidence_browser.adapters import adapt
from mondo_evidence_browser.model import EvidenceRecord
from mondo_evidence_browser.registry import Registry
from mondo_evidence_browser.score import ScoreRule

AS_OF = date(2026, 10, 20)


@pytest.fixture
def rule(config_dir: Path) -> ScoreRule:
    return ScoreRule.from_yaml(config_dir / "rules" / "inclusion-score.yaml")


@pytest.fixture
def registry(config_dir: Path) -> Registry:
    return Registry.from_dir(config_dir)


def _record(raw: dict[str, Any]) -> EvidenceRecord:
    record = adapt(raw)
    assert record is not None
    return record


def test_single_concordance_line(
    rule: ScoreRule, registry: Registry, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    """One DOID line: w = d*s*q*c = 1 * 0.8 * 0.85 * 1.0 = 0.68, L = 4 * 0.68."""
    result = rule.score(_record(raw_by_id["pkt_d519400a27d1"]), registry, AS_OF)
    assert result.value == pytest.approx(math.tanh(4 * 0.68 / 2))
    (line,) = result.lines
    assert (line.s, line.q, line.c, line.c_basis) == (0.8, 0.85, 1.0, "PRIOR")
    assert line.w == pytest.approx(0.68)
    assert line.included
    assert result.rule_id == "mondo-inclusion-score"
    assert result.rule_version == rule.version


def test_independent_sources_accumulate(
    rule: ScoreRule, registry: Registry, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    """DOID (0.68) and Orphanet (0.8 * 0.9 = 0.72) are independent: both count."""
    result = rule.score(_record(raw_by_id["pkt_9c18e79339f2"]), registry, AS_OF)
    assert result.value == pytest.approx(math.tanh(4 * (0.68 + 0.72) / 2))
    assert all(line.included for line in result.lines)


def test_dependent_sources_count_once(
    rule: ScoreRule, registry: Registry, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    """DOID derives from OMIM (D-046): one cluster, only the strongest line counts."""
    raw = copy.deepcopy(raw_by_id["pkt_9c18e79339f2"])
    raw["has_evidence_lines"][1]["has_evidence_items"][0]["source_id"] = "omim"
    result = rule.score(_record(raw), registry, AS_OF)
    doid, omim = result.lines
    assert omim.included and omim.w == pytest.approx(0.8 * 0.95)
    assert not doid.included
    assert doid.reason is not None and "omim" in doid.reason
    assert doid.cluster == omim.cluster
    assert result.value == pytest.approx(math.tanh(4 * 0.76 / 2))


def test_disputing_line_subtracts(
    rule: ScoreRule, registry: Registry, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    raw = copy.deepcopy(raw_by_id["pkt_9c18e79339f2"])
    raw["has_evidence_lines"][1]["direction_of_evidence_provided"] = "disputes"
    result = rule.score(_record(raw), registry, AS_OF)
    assert result.lines[1].w == pytest.approx(-0.72)
    assert result.value == pytest.approx(math.tanh(4 * (0.68 - 0.72) / 2))
    assert result.value is not None and result.value < 0


def test_neutral_and_low_confidence_lines_are_excluded(
    rule: ScoreRule, registry: Registry, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    raw = copy.deepcopy(raw_by_id["pkt_9c18e79339f2"])
    raw["has_evidence_lines"][0]["direction_of_evidence_provided"] = "neutral"
    raw["has_evidence_lines"][1]["confidence"] = rule.confidence_floor - 0.01
    result = rule.score(_record(raw), registry, AS_OF)
    assert [line.included for line in result.lines] == [False, False]
    assert result.value == pytest.approx(0.0)


def test_confidence_above_floor_discounts_multiplicatively(
    rule: ScoreRule, registry: Registry, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    raw = copy.deepcopy(raw_by_id["pkt_d519400a27d1"])
    raw["has_evidence_lines"][0]["confidence"] = 0.75
    (line,) = rule.score(_record(raw), registry, AS_OF).lines
    assert (line.c, line.c_basis) == (0.75, "RECORDED")
    assert line.w == pytest.approx(0.68 * 0.75)
    assert line.s == 0.8, "confidence never rewrites the line's strength (D-062)"


def test_unknown_kinds_and_unruled_claim_types(
    rule: ScoreRule, registry: Registry, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    raw = copy.deepcopy(raw_by_id["pkt_d519400a27d1"])
    raw["has_evidence_lines"][0]["has_evidence_items"][0]["type"] = "SomethingNew"
    (line,) = rule.score(_record(raw), registry, AS_OF).lines
    assert not line.included and line.reason is not None and "unknown" in line.reason

    raw = copy.deepcopy(raw_by_id["pkt_d519400a27d1"])
    raw["statement"]["predicate"]["code"] = "RO:0004003"
    result = rule.score(_record(raw), registry, AS_OF)
    assert result.value is None
    assert result.band == "unscored"


def _spoan(trust: float) -> tuple[ScoreRule, Registry, EvidenceRecord]:
    """D-063 Q2 worked example: "SPOAN syndrome" cited from a Wikipedia page only."""
    rule = ScoreRule.from_dict(
        {
            "id": "test",
            "version": "0",
            "k": 1.0,
            "confidence_floor": 0.5,
            "default_confidence": 1.0,
            "default_resource_quality": 0.5,
            "judgement_kinds": ["expert_judgement"],
            "claim_types": {
                "C003": {
                    "strengths": {"document": 0.8, "expert_judgement": 0.6},
                    "priors": {"default": 0.0},
                }
            },
            "bands": [{"min": -1.0, "label": "any"}],
        }
    )
    registry = Registry.from_dicts(
        resources={"wikipedia": {"quality": 0.4, "aliases": ["en.wikipedia.org"]}},
        agents={"ORCID:0000-0002-1825-0097": {"kind": "person", "trust": trust}},
    )
    raw = {
        "id": "pkt_spoan",
        "statement": {
            "subject": "MONDO:0012297",
            "predicate": {"code": "oio:hasExactSynonym"},
            "object": "SPOAN syndrome",
        },
        "has_evidence_lines": [
            {
                "id": "el_page",
                "direction_of_evidence_provided": "supports",
                "has_evidence_items": [
                    {
                        "type": "SieveDocument",
                        "id": "ev_page",
                        "urls": ["https://en.wikipedia.org/wiki/SPOAN_syndrome"],
                    }
                ],
            },
            {
                "id": "el_expert",
                "direction_of_evidence_provided": "supports",
                "has_evidence_items": [
                    {
                        "type": "AgentContribution",
                        "contributor": {"id": "ORCID:0000-0002-1825-0097", "subtype": "person"},
                    }
                ],
            },
        ],
    }
    return rule, registry, _record(raw)


@pytest.mark.parametrize(("trust", "total"), [(0.5, 0.62), (1.0, 0.92)])
def test_d063_q2_worked_example(trust: float, total: float) -> None:
    """Community contributor: 0.32 + 0.30 = 0.62. ClinGen panel: 0.32 + 0.60 = 0.92."""
    rule, registry, record = _spoan(trust)
    result = rule.score(record, registry, AS_OF)
    page, expert = result.lines
    assert page.w == pytest.approx(0.32)
    assert expert.w == pytest.approx(0.6 * trust)
    assert not expert.capped
    assert pytest.approx(total) == result.L


def test_judgement_with_nothing_read_is_capped_to_zero(
    rule: ScoreRule, registry: Registry, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    """D-063 Q5: a judgement line cannot exceed the evidence it rests on."""
    raw = copy.deepcopy(raw_by_id["pkt_d519400a27d1"])
    raw["has_evidence_lines"][0]["has_evidence_items"] = [
        {
            "type": "AgentContribution",
            "contributor": {"id": "ORCID:0000-0002-1825-0097", "subtype": "person"},
            "trust_level": "curator",
        }
    ]
    (line,) = rule.score(_record(raw), registry, AS_OF).lines
    assert line.capped and line.w == 0.0


def test_score_bands(rule: ScoreRule) -> None:
    assert rule.band(0.95) == "strong support"
    assert rule.band(0.0) == "uncertain"
    assert rule.band(-0.95) == "strong dispute"
    assert rule.band(None) == "unscored"


def test_result_round_trips_through_a_dict(
    rule: ScoreRule, registry: Registry, raw_by_id: dict[str, dict[str, Any]]
) -> None:
    result = rule.score(_record(raw_by_id["pkt_9c18e79339f2"]), registry, AS_OF)
    data = result.to_dict()
    assert data["value"] == result.value
    assert data["lines"][0]["source"] == "doid"
