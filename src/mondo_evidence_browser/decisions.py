"""Sign-offs: the human assertion decisions that put a statement into Mondo.

A sign-off (D-048) records who decided (ORCID, role and trust as of signing, D-049,
D-051), the outcome and the other form fields (D-053), and exactly what was signed:
the record version and the score with its rule version. Recorded sign-offs are
``Decision`` instances (``schema/browser.yaml``); the form's fields and their rules
come from the view spec, so a field added to the schema flows through unchanged.

The decision log is an append-only JSONL file: decisions are never edited or
deleted, rejected claims stay on record, and the latest decision for a record is its
current one.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from mondo_evidence_browser.records import review_status
from mondo_evidence_browser.registry import Registry

DECISION_FORMAT = "mondo-evidence-browser/decision/v1"


class RecordSource(Protocol):
    """Where sign-offs look records up: a local index, or per-record lookup shards."""

    def get(self, key: str) -> dict[str, Any] | None:
        """The record (or its lookup entry), or None."""
        ...

    def refresh(self, key: str) -> None:
        """Drop any cached copy of ``key``'s record."""
        ...

    def note_status(self, record: Mapping[str, Any], status: str) -> None:
        """Record a new review status, where the source can."""
        ...


@dataclass(frozen=True)
class Refusal:
    """Why a sign-off was not recorded, with the matching HTTP status."""

    status_code: int
    message: str


class DecisionLog:
    """Append-only JSONL log of sign-offs."""

    def __init__(self, path: Path) -> None:
        """Use (and create on first write) the log at ``path``."""
        self.path = path
        self._lock = threading.Lock()

    def append(self, decision: Mapping[str, Any]) -> None:
        """Durably append one decision."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(decision, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock, open(self.path, "a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """Decisions in the order they were made."""
        if not self.path.exists():
            return
        with open(self.path) as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def all(self) -> list[dict[str, Any]]:
        """Every decision, oldest first."""
        return list(self)

    def for_record(self, record_id: str) -> list[dict[str, Any]]:
        """The decision history of one record, oldest first."""
        return [d for d in self if d["packet_id"] == record_id]

    def latest(self) -> dict[str, dict[str, Any]]:
        """The current (latest) decision per record."""
        return {d["packet_id"]: d for d in self}


def check_form(form: Mapping[str, Any], values: Mapping[str, Any]) -> str | None:
    """Check submitted values against the schema-derived form; None if they are valid.

    >>> form = {"fields": [{"name": "outcome", "required": True, "kind": "enum",
    ...     "options": [{"value": "accepted"}]}, {"name": "rationale", "required": False,
    ...     "kind": "string", "options": None}]}
    >>> check_form(form, {"outcome": "accepted"}) is None
    True
    >>> check_form(form, {"outcome": "maybe"})
    "outcome must be one of: accepted (got 'maybe')"
    >>> check_form(form, {"outcome": "accepted", "colour": "red"})
    'unknown field(s): colour'
    """
    fields = {f["name"]: f for f in form["fields"]}
    unknown = sorted(set(values) - set(fields))
    if unknown:
        return f"unknown field(s): {', '.join(unknown)}"
    for name, field in fields.items():
        value = values.get(name)
        if value in (None, ""):
            if field["required"]:
                return f"{name} is required"
            continue
        if not isinstance(value, str):
            return f"{name} must be text"
        if field["kind"] == "enum":
            allowed = [o["value"] for o in field["options"]]
            if value not in allowed:
                return f"{name} must be one of: {', '.join(allowed)} (got {value!r})"
    return None


class DecisionSink(Protocol):
    """Where a new decision is written: the local log, or the GitHub decision store."""

    def append(self, decision: Mapping[str, Any]) -> None:
        """Store one decision; never overwrite."""
        ...


def sign_off(
    records: RecordSource,
    log: DecisionSink,
    registry: Registry,
    form: Mapping[str, Any],
    *,
    record_id: str,
    seen_version: str,
    seen_rule_version: str | None,
    values: Mapping[str, Any],
    orcid: str,
    name: str | None,
    now: datetime,
) -> dict[str, Any] | Refusal:
    """Record a curator's decision on a record, or say why it cannot be recorded.

    ``seen_version`` and ``seen_rule_version`` are what the curator was looking at. If
    the record or its score changed since, the sign-off is refused: a curator must
    never sign off evidence they have not seen (D-051).
    """
    record = records.get(record_id)
    if record is None:
        return Refusal(404, f"No evidence record {record_id}.")
    agent = registry.agent(orcid)
    if agent is None or agent.kind != "person" or not agent.can_sign_off:
        return Refusal(
            403,
            f"{orcid} is not a curator in the agent registry, so it cannot sign off. "
            "Ask a Mondo administrator to add you to config/agents.yaml.",
        )

    def changed(found: Mapping[str, Any]) -> bool:
        rule = found["inclusion_score"].get("rule_version")
        return bool(seen_version != found["record_version"] or seen_rule_version != rule)

    if changed(record):
        # A cached lookup may predate a rebuild the curator already sees: check afresh.
        records.refresh(record_id)
        record = records.get(record_id) or record
    score = record["inclusion_score"]
    if changed(record):
        return Refusal(
            409,
            "This record or its score changed after you opened it. Review it again "
            "before signing off.",
        )
    problem = check_form(form, values)
    if problem:
        return Refusal(422, problem)
    statement = record.get("statement") or {}
    decision: dict[str, Any] = {
        "format": DECISION_FORMAT,
        "decision_id": f"dec_{secrets.token_hex(8)}",
        "packet_id": record_id,
        "statement_id": statement.get("id"),
        "packet_version": record["record_version"],
        "statement": {
            "subject": statement.get("subject"),
            "predicate": (statement.get("predicate") or {}).get("code"),
            "object": statement.get("object"),
        },
        "score": {
            k: score.get(k) for k in ("value", "band", "rule_id", "rule_version") if k in score
        },
        **{k: v.strip() for k, v in values.items() if isinstance(v, str) and v.strip()},
        "curator": {
            "orcid": orcid,
            "name": name or agent.name,
            "role": agent.role,
            "trust": registry.agent_trust(orcid, now.date()),
            "trust_as_of": now.date().isoformat(),
        },
        "decided_at": now.isoformat(),
    }
    decision["curator"] = {k: v for k, v in decision["curator"].items() if v is not None}
    log.append(decision)
    status = review_status(decision, record["record_version"], score.get("rule_version"))
    records.note_status(record, status)
    return decision
