"""Build BrowserRecords: a source packet plus everything the browser computes.

A BrowserRecord (see ``schema/browser.yaml``) is the packet itself, unchanged, plus:
its Mondo membership (the tab), claim type, sources, D-063 inclusion score with its
components, review status from the latest sign-off, and record version. Slot names
here are the schema's; tests validate every record against the schema.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from mondo_evidence_browser.adapters import adapt
from mondo_evidence_browser.mondo import MondoRelease
from mondo_evidence_browser.registry import Registry
from mondo_evidence_browser.score import ScoreResult, ScoreRule

OPEN_STATUSES = ("unreviewed", "stale", "deferred")
"""Review statuses that put a record in the curation queue (deferred: D-053)."""


def review_status(
    decision: Mapping[str, Any] | None, version: str, rule_version: str | None
) -> str:
    """Workflow status given the latest sign-off on a record.

    A sign-off applies to one record version and one score rule version (D-048,
    D-051); if either changed since, the sign-off is stale.

    >>> review_status(None, "v_1", "r1")
    'unreviewed'
    >>> d = {"outcome": "accepted", "packet_version": "v_1", "score": {"rule_version": "r1"}}
    >>> review_status(d, "v_1", "r1"), review_status(d, "v_2", "r1")
    ('accepted', 'stale')
    """
    if decision is None:
        return "unreviewed"
    if decision["packet_version"] != version or decision["score"]["rule_version"] != rule_version:
        return "stale"
    return str(decision["outcome"])


def _drop_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def _names(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'.

    >>> _names(["OMIM"]), _names(["A", "B", "C"])
    ('OMIM', 'A, B and C')
    """
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def verdict(result: Any, registry: Registry) -> str:
    """What the evidence amounts to, in a line a curator reads first.

    Counts independent sources (clusters, D-046) for and against, names them, and
    gives the score with its band and whether the rule is calibrated yet.
    """

    def side(direction: int) -> tuple[int, list[str]]:
        lines = [x for x in result.lines if x.included and x.direction * direction > 0]
        clusters = {x.cluster for x in lines}
        sources = {registry.resource_id(x.source) for x in lines}
        names = sorted(
            registry.resources[r].name if r in registry.resources else r for r in sources
        )
        return len(clusters), names

    supported, for_names = side(1)
    disputed, against_names = side(-1)
    plural = "s" if supported != 1 else ""
    if supported and disputed:
        evidence = (
            f"Supported by {supported} independent source{plural} ({_names(for_names)}); "
            f"disputed by {disputed} ({_names(against_names)})."
        )
    elif supported:
        evidence = f"Supported by {supported} independent source{plural} ({_names(for_names)})."
    elif disputed:
        evidence = (
            f"Disputed by {disputed} independent source{'s' if disputed != 1 else ''} "
            f"({_names(against_names)})."
        )
    else:
        evidence = "No evidence counts towards the score yet."
    if result.value is None:
        score = "Not scored: no rule covers this claim type."
    else:
        score = f"Score {result.value:.2f}, {result.band}."
    caveat = " Rule not yet calibrated." if "uncalibrated" in str(result.rule_version) else ""
    return f"{evidence} {score}{caveat}"


def inclusion_score(result: ScoreResult, summary: str | None = None) -> dict[str, Any]:
    """A :class:`ScoreResult` as an ``InclusionScore`` instance."""
    return _drop_none(
        {
            "value": result.value,
            "summary": summary,
            "band": result.band,
            "rule_id": result.rule_id,
            "rule_version": result.rule_version,
            "as_of": result.as_of,
            "claim_prior": result.L0,
            "scaling": result.k,
            "log_odds": result.L,
            "proposing_channel": result.channel,
            "components": [
                _drop_none(
                    {
                        "line_id": line.line_id,
                        "kind": line.kind,
                        "source": line.source,
                        "direction": line.direction,
                        "strength": line.s,
                        "source_prior": line.q,
                        "source_prior_basis": line.q_basis,
                        "confidence": line.c,
                        "confidence_basis": line.c_basis,
                        "weight": line.w,
                        "cluster": line.cluster,
                        "counted": line.included,
                        "capped": line.capped,
                        "reason": line.reason,
                    }
                )
                for line in result.lines
            ],
            "notes": list(result.notes) or None,
        }
    )


@dataclass(frozen=True)
class RecordBuilder:
    """Everything needed to turn packets into records."""

    rule: ScoreRule
    registry: Registry
    mondo: MondoRelease
    as_of: date

    def build(
        self, raw: Mapping[str, Any], decision: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """The BrowserRecord for a packet, or None if no adapter recognises it."""
        record = adapt(raw)
        if record is None:
            return None
        result = self.rule.score(record, self.registry, self.as_of)
        version = record.version
        return _drop_none(
            {
                **raw,
                "mondo_membership": self.mondo.membership(
                    record.subject, record.predicate, record.object
                ),
                "mondo_release": self.mondo.version,
                "claim_type": record.claim_type,
                "sources": list(record.sources),
                "inclusion_score": inclusion_score(result, verdict(result, self.registry)),
                "review_status": review_status(decision, version, result.rule_version),
                "record_version": version,
            }
        )
