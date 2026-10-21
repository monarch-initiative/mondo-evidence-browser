"""Normalised evidence record model.

Source schemas (mondo-ai SIEVE packets today, the Mondo evidence model later) are a
moving target. Adapters in :mod:`mondo_evidence_browser.adapters` map each source
shape onto these classes; the score, store and web layers only ever see these.
Every object keeps its ``raw`` source dict so nothing is lost when a schema gains
fields an adapter does not know yet.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ItemKind(StrEnum):
    """What kind of evidence an item (or a line made of such items) is.

    The kinds follow the evidence item types of the Mondo requirements (D-043) and
    the human/AI interpretation acts of D-049/D-050.
    """

    CONCORDANCE = "concordance"
    """An external record asserting the same statement (EI.PRIOR_CLAIM.DIRECT_STRUCTURED)."""
    DOCUMENT = "document"
    """A publication or web page cited as supporting evidence."""
    CANDIDATE_REFERENCE = "candidate_reference"
    """A document found in source annotations, not yet verified to support the claim."""
    EXPERT_JUDGEMENT = "expert_judgement"
    """A person's interpretation or decision (ORCID-identified agent)."""
    ORGANISATIONAL_DECISION = "organisational_decision"
    """A decision by an organisation, e.g. the Mondo consortium including the axiom."""
    COMPUTATIONAL = "computational"
    """A computational prediction or score (lexical match, Boomer, AI confidence)."""
    TEXT_MINING = "text_mining"
    """A text span extracted from a document by a text-mining or LLM pipeline."""
    DATA_ITEM = "data_item"
    STUDY_RESULT = "study_result"
    UNKNOWN = "unknown"
    """An item type no adapter recognises; shown raw, never scored."""
    MIXED = "mixed"
    """A line whose items are of different kinds (line granularity is parked, D-046)."""
    EMPTY = "empty"
    """A line with no items."""


CLAIM_TYPES: dict[str, str] = {
    "IAO:0000115": "C002",
    "oio:hasExactSynonym": "C003",
    "oio:hasRelatedSynonym": "C003",
    "oio:hasNarrowSynonym": "C003",
    "oio:hasBroadSynonym": "C003",
    "rdfs:subClassOf": "C004",
    "skos:exactMatch": "C006",
    "skos:broadMatch": "C006",
    "skos:narrowMatch": "C006",
    "skos:relatedMatch": "C006",
}
"""Statement predicate → Mondo claim type (requirements CT form, D-039)."""

CLAIM_TYPE_NAMES: dict[str, str] = {
    "C001": "term existence / obsoletion",
    "C002": "definition",
    "C003": "synonym",
    "C004": "subclass placement",
    "C005": "negative subclass",
    "C006": "mapping",
    "C007": "disease attribute",
    "C008": "subset membership",
}


def claim_type_for(predicate: str | None) -> str | None:
    """Return the Mondo claim type for a predicate CURIE.

    >>> claim_type_for("oio:hasNarrowSynonym")
    'C003'
    >>> claim_type_for("RO:0004003") is None
    True
    """
    if predicate is None:
        return None
    return CLAIM_TYPES.get(predicate)


@dataclass(frozen=True)
class EvidenceItem:
    """One evidence item, e.g. a concordant DOID record or a cited PMID."""

    id: str | None
    kind: ItemKind
    type: str
    source: str | None
    """The resource (``doid``, ``pubmed``) or agent (``ORCID:...``) the item comes from."""
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class EvidenceLine:
    """An interpretation of one or more items as supporting or disputing the statement."""

    id: str | None
    direction: str | None
    strength_label: str | None
    stored_score: float | None
    """The numeric strength stored on the line by whoever produced it (not the D-063 s)."""
    confidence: float | None
    """Data-quality confidence of the line (D-062), if the source records one."""
    items: tuple[EvidenceItem, ...]
    raw: Mapping[str, Any]

    @property
    def kind(self) -> ItemKind:
        """The single kind shared by all items, or MIXED / EMPTY."""
        kinds = {item.kind for item in self.items}
        if not kinds:
            return ItemKind.EMPTY
        if len(kinds) > 1:
            return ItemKind.MIXED
        return kinds.pop()

    @property
    def source(self) -> str | None:
        """The single source shared by all items, else None."""
        sources = {item.source for item in self.items}
        return sources.pop() if len(sources) == 1 else None


@dataclass(frozen=True)
class EvidenceRecord:
    """A statement with its evidence: the unit curators review and sign off."""

    packet_id: str
    statement_id: str | None
    subject: str
    subject_label: str | None
    predicate: str
    predicate_label: str | None
    object: str
    object_label: str | None
    statement_text: str | None
    claim_type: str | None
    proposing_channel: str
    """The channel that proposed the statement; drives the claim prior (D-063 Q3)."""
    stored_status: str | None
    lines: tuple[EvidenceLine, ...]
    adapter: str
    raw: Mapping[str, Any]

    @property
    def version(self) -> str:
        """Content version of the record, see :func:`packet_version`."""
        return packet_version(self.raw)

    @property
    def sources(self) -> tuple[str, ...]:
        """Distinct line sources, in line order."""
        return tuple(dict.fromkeys(line.source for line in self.lines if line.source))


PREFIXES: dict[str, str] = {
    "oio": "http://www.geneontology.org/formats/oboInOwl#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "MONDO": "http://purl.obolibrary.org/obo/MONDO_",
    "IAO": "http://purl.obolibrary.org/obo/IAO_",
}


def to_curie(value: str) -> str:
    """Contract a known IRI to a CURIE; anything else is returned unchanged.

    Links from Mondo may use IRIs or CURIEs; records store CURIEs.

    >>> to_curie("http://purl.obolibrary.org/obo/MONDO_0008814")
    'MONDO:0008814'
    >>> to_curie("oio:hasExactSynonym")
    'oio:hasExactSynonym'
    """
    for prefix, base in PREFIXES.items():
        if value.startswith(base):
            return f"{prefix}:{value[len(base) :]}"
    return value


VOLATILE_FIELDS = ("created", "updated")
"""Top-level fields that change on every pipeline run without changing the evidence."""


def packet_version(raw: Mapping[str, Any]) -> str:
    """Return a content hash identifying this version of a packet.

    A sign-off refers to the packet "as of" a version (D-051), so the version must
    change when evidence changes and stay put when a rerun only re-dates the packet.

    >>> packet_version({"id": "pkt_1", "created": "2026-01-01"})
    'v_4f5430ea7b085d62'
    >>> packet_version({"id": "pkt_1", "created": "2027-12-31"})
    'v_4f5430ea7b085d62'
    """
    content = {k: v for k, v in raw.items() if k not in VOLATILE_FIELDS}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "v_" + hashlib.sha256(canonical.encode()).hexdigest()[:16]
