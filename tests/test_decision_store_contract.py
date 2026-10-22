"""What every decision store must do, whatever it keeps decisions in.

Sign-offs are append-only records pointing at (record id, record version). Today they
are kept in a JSONL file (local runs), a folder of files (a checkout of the decisions
repository) or a GitHub repository (the deployed service). At hundreds of thousands
of records and several reviewers a database is the likely end state; a store for it
only has to pass these tests to drop in.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mondo_evidence_browser.decision_store import DirectoryDecisionStore, GitHubDecisionStore
from mondo_evidence_browser.decisions import DecisionLog, DecisionStore
from tests.github_api import REPO, TOKEN, FakeContentsAPI

A, B = "pkt_9c18e79339f2", "pkt_e27a7e05d319"


def _decision(decision_id: str, record: str, at: str, outcome: str = "accepted") -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "packet_id": record,
        "packet_version": "v_8bf2631ae315161b",
        "score": {"rule_version": "0.1.0-uncalibrated"},
        "outcome": outcome,
        "curator": {"orcid": "ORCID:0000-0002-1825-0097", "name": "Josiah Carberry"},
        "decided_at": at,
    }


def _jsonl(tmp_path: Path) -> DecisionStore:
    return DecisionLog(tmp_path / "decisions.jsonl")


def _directory(tmp_path: Path) -> DecisionStore:
    return DirectoryDecisionStore(tmp_path / "checkout")


def _github(tmp_path: Path) -> DecisionStore:
    return GitHubDecisionStore(REPO, TOKEN, transport=FakeContentsAPI().transport(), ttl_recent=0)


STORES: dict[str, Callable[[Path], DecisionStore]] = {
    "jsonl": _jsonl,
    "directory": _directory,
    "github": _github,
}


@pytest.fixture(params=sorted(STORES))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> DecisionStore:
    return STORES[request.param](tmp_path)


def test_a_new_store_is_empty(store: DecisionStore) -> None:
    assert store.for_record(A) == []
    assert store.since(None) == []
    assert store.latest() == {}


def test_history_is_per_record_and_oldest_first(store: DecisionStore) -> None:
    store.append(_decision("dec_1", A, "2026-10-21T12:00:00+00:00"))
    store.append(_decision("dec_2", B, "2026-10-21T12:05:00+00:00"))
    store.append(_decision("dec_3", A, "2026-10-21T12:10:00+00:00", outcome="rejected"))
    assert [d["decision_id"] for d in store.for_record(A, fresh=True)] == ["dec_1", "dec_3"]
    assert [d["decision_id"] for d in store.for_record(B, fresh=True)] == ["dec_2"]


def test_decisions_come_back_as_they_were_stored(store: DecisionStore) -> None:
    decision = _decision("dec_1", A, "2026-10-21T12:00:00+00:00")
    store.append(decision)
    assert store.for_record(A, fresh=True) == [decision]


def test_the_latest_decision_per_record_is_its_current_one(store: DecisionStore) -> None:
    store.append(_decision("dec_1", A, "2026-10-21T12:00:00+00:00"))
    store.append(_decision("dec_2", A, "2026-10-21T12:10:00+00:00", outcome="rejected"))
    store.append(_decision("dec_3", B, "2026-10-21T12:05:00+00:00"))
    latest = store.latest()
    assert {k: v["decision_id"] for k, v in latest.items()} == {A: "dec_2", B: "dec_3"}


def test_since_returns_only_later_decisions(store: DecisionStore) -> None:
    store.append(_decision("dec_1", A, "2026-10-21T12:00:00+00:00"))
    store.append(_decision("dec_2", B, "2026-10-21T12:05:00+00:00"))
    store.append(_decision("dec_3", A, "2026-10-21T12:10:00+00:00"))
    later = store.since("2026-10-21T12:00:00+00:00")
    assert sorted(d["decision_id"] for d in later) == ["dec_2", "dec_3"]
    assert sorted(d["decision_id"] for d in store.since(None)) == ["dec_1", "dec_2", "dec_3"]
    assert store.since("2026-10-21T12:10:00+00:00") == []
