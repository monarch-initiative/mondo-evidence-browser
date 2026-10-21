"""The Mondo inclusion score (requirement D-063).

One number in (-1, 1) per statement that ranks the curation queue (D-047) and that a
human signs off (D-048)::

    w_i = d_i * s_i * q_i * c_i              per evidence line
    L   = L0 + k * sum of w over independence clusters (strongest line per cluster)
    S   = tanh(L / 2)                         (= 2 * P(true) - 1)

The rule's parameters live in ``config/rules/*.yaml`` with an id and version, and every
:class:`ScoreResult` names the version that produced it, so a sign-off can be checked
against the score it was given on. The result keeps a per-line breakdown so the
browser can show curators why a statement scores what it does.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from mondo_evidence_browser.model import EvidenceLine, EvidenceRecord
from mondo_evidence_browser.registry import Registry

DIRECTIONS = {"supports": 1, "disputes": -1}


@dataclass(frozen=True)
class LineScore:
    """How one evidence line enters the score, or why it does not."""

    line_id: str | None
    kind: str
    source: str | None
    direction: int | None
    s: float | None
    q: float | None
    q_basis: str | None
    c: float | None
    c_basis: str | None
    w: float | None
    cluster: str | None
    included: bool
    capped: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class ScoreResult:
    """The score of one statement under one rule version, with its breakdown."""

    value: float | None
    band: str
    rule_id: str
    rule_version: str
    claim_type: str | None
    channel: str
    as_of: str
    L0: float | None
    k: float
    L: float | None
    lines: tuple[LineScore, ...]
    notes: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON form, as stored in the index and served by the API."""
        return asdict(self)


@dataclass(frozen=True)
class ClaimTypeRule:
    """Strengths per evidence kind and priors per proposing channel for one claim type."""

    strengths: Mapping[str, float]
    priors: Mapping[str, float]

    def prior(self, channel: str) -> float:
        """Claim prior L0 for a channel, falling back to the ``default`` prior."""
        return self.priors.get(channel, self.priors.get("default", 0.0))


@dataclass(frozen=True)
class ScoreRule:
    """A declared, versioned aggregation rule (D-048, D-054)."""

    id: str
    version: str
    k: float
    confidence_floor: float
    default_confidence: float
    default_resource_quality: float
    trust_levels: Mapping[str, float]
    judgement_kinds: frozenset[str]
    claim_types: Mapping[str, ClaimTypeRule]
    bands: tuple[tuple[float, str], ...]

    @classmethod
    def from_dict(cls, spec: Mapping[str, Any]) -> ScoreRule:
        """Build a rule from its parsed YAML."""
        return cls(
            id=spec["id"],
            version=str(spec["version"]),
            k=float(spec["k"]),
            confidence_floor=float(spec["confidence_floor"]),
            default_confidence=float(spec["default_confidence"]),
            default_resource_quality=float(spec["default_resource_quality"]),
            trust_levels=dict(spec.get("trust_levels", {})),
            judgement_kinds=frozenset(spec.get("judgement_kinds", ())),
            claim_types={
                ct: ClaimTypeRule(dict(r.get("strengths", {})), dict(r.get("priors", {})))
                for ct, r in spec["claim_types"].items()
            },
            bands=tuple(
                sorted(((float(b["min"]), b["label"]) for b in spec["bands"]), reverse=True)
            ),
        )

    @classmethod
    def from_yaml(cls, path: Path) -> ScoreRule:
        """Load a rule file such as ``config/rules/inclusion-score.yaml``."""
        return cls.from_dict(yaml.safe_load(path.read_text()))

    def band(self, value: float | None) -> str:
        """Categorical label for a score (D-047: labels derived from score bands)."""
        if value is None:
            return "unscored"
        for minimum, label in self.bands:
            if value >= minimum:
                return label
        return self.bands[-1][1]

    def score(self, record: EvidenceRecord, registry: Registry, as_of: date) -> ScoreResult:
        """Score a record with registry levels as of ``as_of``."""
        ct_rule = self.claim_types.get(record.claim_type or "")
        if ct_rule is None:
            return ScoreResult(
                value=None,
                band=self.band(None),
                rule_id=self.id,
                rule_version=self.version,
                claim_type=record.claim_type,
                channel=record.proposing_channel,
                as_of=as_of.isoformat(),
                L0=None,
                k=self.k,
                L=None,
                lines=tuple(
                    _excluded(line, f"no rule for claim type {record.claim_type}")
                    for line in record.lines
                ),
                notes=(f"Rule {self.id} {self.version} has no rule for this claim type.",),
            )
        lines = [self._line(line, ct_rule, registry, as_of) for line in record.lines]
        lines = self._cap_judgements(lines)
        lines = self._keep_strongest_per_cluster(lines, registry)
        L0 = ct_rule.prior(record.proposing_channel)
        L = L0 + self.k * sum(line.w or 0.0 for line in lines if line.included)
        value = math.tanh(L / 2)
        return ScoreResult(
            value=value,
            band=self.band(value),
            rule_id=self.id,
            rule_version=self.version,
            claim_type=record.claim_type,
            channel=record.proposing_channel,
            as_of=as_of.isoformat(),
            L0=L0,
            k=self.k,
            L=L,
            lines=tuple(lines),
        )

    def _line(
        self, line: EvidenceLine, ct_rule: ClaimTypeRule, registry: Registry, as_of: date
    ) -> LineScore:
        """Direction, strength, source prior and confidence of one line."""
        kind = line.kind
        direction = DIRECTIONS.get((line.direction or "").lower())
        if direction is None:
            return _excluded(line, f"direction {line.direction!r} carries no support or dispute")
        s = ct_rule.strengths.get(kind.value)
        if s is None:
            return _excluded(line, f"no strength for {kind.value} evidence under this rule")
        q, q_basis = self._source_prior(line, registry, as_of)
        if line.confidence is not None:
            c, c_basis = line.confidence, "RECORDED"
        else:
            c, c_basis = self.default_confidence, "PRIOR"
        base = LineScore(
            line_id=line.id,
            kind=kind.value,
            source=line.source,
            direction=direction,
            s=s,
            q=q,
            q_basis=q_basis,
            c=c,
            c_basis=c_basis,
            w=None,
            cluster=None,
            included=False,
        )
        if c < self.confidence_floor:
            return replace(base, reason=f"confidence {c} below floor {self.confidence_floor}")
        return replace(base, w=direction * s * q * c, included=True)

    def _source_prior(
        self, line: EvidenceLine, registry: Registry, as_of: date
    ) -> tuple[float, str]:
        """Agent trust for judgement lines, resource quality for everything else (Q2)."""
        source = line.source
        if line.kind.value in self.judgement_kinds:
            trust = registry.agent_trust(source, as_of) if source else None
            if trust is not None:
                return trust, "agent registry"
            level = line.items[0].raw.get("trust_level") if line.items else None
            if isinstance(level, str) and level in self.trust_levels:
                return self.trust_levels[level], f"trust_level {level}"
            return self.default_resource_quality, "default (agent not registered)"
        quality = registry.resource_quality(source, as_of) if source else None
        if quality is not None:
            return quality, "resource registry"
        return self.default_resource_quality, "default (resource not registered)"

    def _cap_judgements(self, lines: list[LineScore]) -> list[LineScore]:
        """Cap judgement lines at trust x strength of the evidence they rest on (Q5).

        No source records what an agent read yet (D-059), so the agent is assumed to
        have read every other line on the statement.
        """
        read = [
            line.s or 0.0
            for line in lines
            if line.included and line.kind not in self.judgement_kinds
        ]
        read_strength = max(read, default=0.0)
        capped = []
        for line in lines:
            if line.included and line.kind in self.judgement_kinds and line.w is not None:
                cap = (line.q or 0.0) * read_strength
                if abs(line.w) > cap:
                    line = replace(
                        line,
                        w=math.copysign(cap, line.w) if cap else 0.0,
                        capped=True,
                        reason=f"capped at trust x strength read = {cap:.2f} (D-063 Q5)",
                    )
            capped.append(line)
        return capped

    def _keep_strongest_per_cluster(
        self, lines: list[LineScore], registry: Registry
    ) -> list[LineScore]:
        """Group included lines into independence clusters; only the strongest counts."""
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        keys: list[str | None] = []
        for index, line in enumerate(lines):
            if not line.included:
                keys.append(None)
            elif line.kind in self.judgement_kinds:
                keys.append(f"agent:{line.source or index}")
            elif line.source is None:
                keys.append(f"line:{line.line_id or index}")
            else:
                resource = registry.resource_id(line.source)
                for ancestor in registry.derived_from(resource):
                    union(resource, ancestor)
                keys.append(resource)
        for key in keys:
            if key is not None:
                find(key)

        strongest: dict[str, int] = {}
        for index, key in enumerate(keys):
            if key is None:
                continue
            root = find(key)
            best = strongest.get(root)
            if best is None or abs(lines[index].w or 0.0) > abs(lines[best].w or 0.0):
                strongest[root] = index

        result = []
        for index, (line, key) in enumerate(zip(lines, keys, strict=True)):
            if key is None:
                result.append(line)
                continue
            root = find(key)
            line = replace(line, cluster=root)
            winner = strongest[root]
            if winner != index:
                line = replace(
                    line,
                    included=False,
                    reason=(
                        f"dependent: same independence cluster as {lines[winner].source} "
                        "(D-046), only the strongest line counts"
                    ),
                )
            result.append(line)
        return result


def _excluded(line: EvidenceLine, reason: str) -> LineScore:
    return LineScore(
        line_id=line.id,
        kind=line.kind.value,
        source=line.source,
        direction=DIRECTIONS.get((line.direction or "").lower()),
        s=None,
        q=None,
        q_basis=None,
        c=None,
        c_basis=None,
        w=None,
        cluster=None,
        included=False,
        reason=reason,
    )
