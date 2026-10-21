"""Adapters turn source-schema packets into the normalised record model."""

import copy
from typing import Any

from mondo_evidence_browser.adapters import adapt
from mondo_evidence_browser.model import ItemKind, packet_version


def test_every_fixture_packet_adapts(raw_packets: list[dict[str, Any]]) -> None:
    records = [adapt(p) for p in raw_packets]
    assert all(r is not None for r in records)
    assert {r.adapter for r in records if r} == {"mondo-ai-sieve-v1"}


def test_multi_source_packet_is_normalised(raw_by_id: dict[str, dict[str, Any]]) -> None:
    record = adapt(raw_by_id["pkt_9c18e79339f2"])
    assert record is not None
    assert record.packet_id == "pkt_9c18e79339f2"
    assert record.statement_id == "stmt_1a0584b0faa6"
    assert record.subject == "MONDO:0008814"
    assert record.subject_label == "arginase deficiency"
    assert record.predicate == "oio:hasExactSynonym"
    assert record.predicate_label == "exact synonym"
    assert record.object == "Arginase deficiency"
    assert record.claim_type == "C003"
    assert [line.kind for line in record.lines] == [ItemKind.CONCORDANCE, ItemKind.CONCORDANCE]
    assert [line.source for line in record.lines] == ["doid", "ordo"]
    assert [line.direction for line in record.lines] == ["supports", "supports"]
    assert [line.stored_score for line in record.lines] == [0.85, 0.9]
    item = record.lines[0].items[0]
    assert item.raw["source_subject"] == "DOID:9278"
    assert record.raw is raw_by_id["pkt_9c18e79339f2"]


def test_proposing_channel_is_marked_as_inferred(raw_by_id: dict[str, dict[str, Any]]) -> None:
    record = adapt(raw_by_id["pkt_9c18e79339f2"])
    assert record is not None
    assert record.proposing_channel == "mondo-ai-concordance"


def test_unrecognised_shapes_are_not_adapted() -> None:
    assert adapt({"foo": 1}) is None
    # Legacy camelCase packets predate the snake_case model.
    assert adapt({"id": "pkt_1", "statement": {}, "hasEvidenceLines": []}) is None


def _with_items(packet: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    p = copy.deepcopy(packet)
    p["has_evidence_lines"] = [
        {
            "id": f"el_{i}",
            "type": "SieveEvidenceLine",
            "has_evidence_items": [item],
            "direction_of_evidence_provided": "supports",
        }
        for i, item in enumerate(items)
    ]
    return p


def test_item_kinds_follow_the_mondo_ai_schema(raw_by_id: dict[str, dict[str, Any]]) -> None:
    """Shapes as written by mondo-ai's PacketBuilder for Mondo/ORCID/PMID evidence."""
    packet = _with_items(
        raw_by_id["pkt_9c18e79339f2"],
        [
            {
                "id": "ev_a",
                "type": "AgentContribution",
                "contributor": {"id": "ORCID:0000-0002-1825-0097", "subtype": "person"},
                "trust_level": "curator",
                "contribution_type": "decision",
            },
            {
                "id": "ev_b",
                "type": "AgentContribution",
                "contributor": {"id": "mondo:consortium", "subtype": "organization"},
                "trust_level": "authority",
            },
            {
                "id": "ev_c",
                "type": "SieveDocument",
                "pmid": "12345",
                "subtype": {"code": "candidate_reference"},
            },
            {"id": "ev_d", "type": "SieveDocument", "urls": ["https://en.wikipedia.org/wiki/X"]},
            {"id": "ev_e", "type": "ComputationalResult", "value": "0.9"},
            {"id": "ev_f", "type": "SomethingNew"},
        ],
    )
    record = adapt(packet)
    assert record is not None
    assert [line.kind for line in record.lines] == [
        ItemKind.EXPERT_JUDGEMENT,
        ItemKind.ORGANISATIONAL_DECISION,
        ItemKind.CANDIDATE_REFERENCE,
        ItemKind.DOCUMENT,
        ItemKind.COMPUTATIONAL,
        ItemKind.UNKNOWN,
    ]
    assert [line.source for line in record.lines] == [
        "ORCID:0000-0002-1825-0097",
        "mondo:consortium",
        "pubmed",
        "en.wikipedia.org",
        None,
        None,
    ]


def test_line_with_mixed_items_has_mixed_kind(raw_by_id: dict[str, dict[str, Any]]) -> None:
    packet = copy.deepcopy(raw_by_id["pkt_9c18e79339f2"])
    line = packet["has_evidence_lines"][0]
    line["has_evidence_items"].append({"id": "ev_x", "type": "SieveDocument", "pmid": "1"})
    record = adapt(packet)
    assert record is not None
    assert record.lines[0].kind == ItemKind.MIXED


def test_packet_version_ignores_dates_but_tracks_evidence(
    raw_by_id: dict[str, dict[str, Any]],
) -> None:
    packet = raw_by_id["pkt_9c18e79339f2"]
    version = packet_version(packet)
    assert version.startswith("v_") and len(version) == 18

    redated = copy.deepcopy(packet)
    redated["created"] = "2030-01-01"
    redated["updated"] = "2030-01-02"
    assert packet_version(redated) == version

    changed = copy.deepcopy(packet)
    changed["has_evidence_lines"][1]["direction_of_evidence_provided"] = "disputes"
    assert packet_version(changed) != version
