"""Adapters from source packet schemas to the normalised record model.

Each adapter recognises one source shape. :func:`adapt` tries them in order and
returns None for a shape nobody recognises, so ingest can report it instead of
guessing. To support a new schema version, add an adapter here; nothing else in the
browser should need to change.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from mondo_evidence_browser.model import (
    EvidenceItem,
    EvidenceLine,
    EvidenceRecord,
    ItemKind,
    claim_type_for,
)

SIMPLE_KINDS: dict[str, ItemKind] = {
    "ConcordanceItem": ItemKind.CONCORDANCE,
    "ComputationalResult": ItemKind.COMPUTATIONAL,
    "TextMiningResult": ItemKind.TEXT_MINING,
    "SieveDataItem": ItemKind.DATA_ITEM,
    "DataItem": ItemKind.DATA_ITEM,
    "SieveStudyResult": ItemKind.STUDY_RESULT,
    "StudyResult": ItemKind.STUDY_RESULT,
}


def _sieve_item(raw: Mapping[str, Any]) -> EvidenceItem:
    """Normalise one mondo-ai SIEVE evidence item.

    >>> _sieve_item({"id": "ev_1", "type": "ConcordanceItem", "source_id": "omim"}).source
    'omim'
    """
    item_type = str(raw.get("type", ""))
    kind = SIMPLE_KINDS.get(item_type, ItemKind.UNKNOWN)
    source: str | None = None
    if kind is ItemKind.CONCORDANCE:
        source = raw.get("source_id")
    elif item_type in ("SieveDocument", "Document"):
        subtype = raw.get("subtype") or {}
        is_candidate = subtype.get("code") == "candidate_reference"
        kind = ItemKind.CANDIDATE_REFERENCE if is_candidate else ItemKind.DOCUMENT
        urls = raw.get("urls") or ()
        if raw.get("pmid"):
            source = "pubmed"
        elif urls:
            # Web pages (EI.CUSTOM.001) are attributed to their host; the resource
            # registry maps hosts such as en.wikipedia.org to a resource.
            source = urlparse(urls[0]).hostname
    elif item_type == "AgentContribution":
        contributor = raw.get("contributor") or {}
        is_org = contributor.get("subtype") == "organization"
        kind = ItemKind.ORGANISATIONAL_DECISION if is_org else ItemKind.EXPERT_JUDGEMENT
        source = contributor.get("id")
    return EvidenceItem(id=raw.get("id"), kind=kind, type=item_type, source=source, raw=raw)


def _sieve_line(raw: Mapping[str, Any]) -> EvidenceLine:
    """Normalise one mondo-ai SIEVE evidence line."""
    score = raw.get("score_of_evidence_provided")
    confidence = raw.get("confidence")
    return EvidenceLine(
        id=raw.get("id"),
        direction=raw.get("direction_of_evidence_provided"),
        strength_label=raw.get("strength_of_evidence_provided"),
        stored_score=float(score) if score is not None else None,
        confidence=float(confidence) if confidence is not None else None,
        items=tuple(_sieve_item(i) for i in raw.get("has_evidence_items") or ()),
        raw=raw,
    )


def _matches_sieve_v1(raw: Mapping[str, Any]) -> bool:
    statement = raw.get("statement")
    return (
        isinstance(raw.get("id"), str)
        and isinstance(statement, Mapping)
        and isinstance(statement.get("predicate"), Mapping)
        and "has_evidence_lines" in raw
    )


def _adapt_sieve_v1(raw: Mapping[str, Any]) -> EvidenceRecord:
    """mondo-ai snake_case SIEVE packets (``packet_store*/packets_*.jsonl``).

    Those packets do not say which channel proposed the statement. All of them come
    from mondo-ai's concordance pipeline, so the adapter records that channel.
    """
    statement = raw["statement"]
    predicate = statement["predicate"]
    code = predicate.get("code") or ""
    return EvidenceRecord(
        packet_id=raw["id"],
        statement_id=statement.get("id"),
        subject=statement.get("subject") or "",
        subject_label=statement.get("subject_label"),
        predicate=code,
        predicate_label=predicate.get("label"),
        object=statement.get("object") or "",
        object_label=statement.get("object_label"),
        statement_text=statement.get("statement_text"),
        claim_type=claim_type_for(code),
        proposing_channel="mondo-ai-concordance",
        stored_status=raw.get("status"),
        lines=tuple(_sieve_line(line) for line in raw.get("has_evidence_lines") or ()),
        adapter="mondo-ai-sieve-v1",
        raw=raw,
    )


@dataclass(frozen=True)
class Adapter:
    """A recogniser plus a converter for one source schema."""

    name: str
    matches: Callable[[Mapping[str, Any]], bool]
    convert: Callable[[Mapping[str, Any]], EvidenceRecord]


ADAPTERS: tuple[Adapter, ...] = (Adapter("mondo-ai-sieve-v1", _matches_sieve_v1, _adapt_sieve_v1),)


def adapt(raw: Mapping[str, Any]) -> EvidenceRecord | None:
    """Normalise a raw packet with the first adapter that recognises it.

    >>> adapt({"not": "a packet"}) is None
    True
    """
    for adapter in ADAPTERS:
        if adapter.matches(raw):
            return adapter.convert(raw)
    return None
