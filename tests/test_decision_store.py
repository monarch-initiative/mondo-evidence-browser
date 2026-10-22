"""Decision stores: a GitHub repository (one file per decision), or a folder of them."""

import json
from pathlib import Path
from typing import Any

import pytest

from mondo_evidence_browser.decision_store import (
    DecisionStoreError,
    DirectoryDecisionStore,
    GitHubDecisionStore,
    decision_path,
)
from tests.github_api import REPO, TOKEN, FakeContentsAPI

PACKET = "pkt_9c18e79339f2"


def _decision(decision_id: str, at: str, outcome: str = "accepted") -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "packet_id": PACKET,
        "packet_version": "v_8bf2631ae315161b",
        "score": {"rule_version": "0.1.0-uncalibrated"},
        "outcome": outcome,
        "curator": {"orcid": "ORCID:0000-0002-1825-0097"},
        "decided_at": at,
    }


def test_decision_paths_are_per_record_and_never_collide() -> None:
    first = decision_path(_decision("dec_1", "2026-10-21T12:00:00+00:00"))
    second = decision_path(_decision("dec_2", "2026-10-21T12:00:00+00:00"))
    assert first.startswith("decisions/") and f"/{PACKET}/" in first
    assert first.endswith("2026-10-21T12-00-00+00-00_dec_1.json")
    assert first != second


@pytest.fixture
def api() -> FakeContentsAPI:
    return FakeContentsAPI()


@pytest.fixture
def store(api: FakeContentsAPI) -> GitHubDecisionStore:
    return GitHubDecisionStore(REPO, TOKEN, branch="main", transport=api.transport())


def test_github_store_commits_one_file_per_decision(
    api: FakeContentsAPI, store: GitHubDecisionStore
) -> None:
    decision = _decision("dec_1", "2026-10-21T12:00:00+00:00")
    store.append(decision)
    (commit,) = api.commits
    assert commit["path"] == decision_path(decision)
    assert commit["branch"] == "main"
    assert "accepted" in commit["message"] and PACKET in commit["message"]
    assert json.loads(api.files[commit["path"]]) == decision


def test_history_comes_back_in_order(api: FakeContentsAPI, store: GitHubDecisionStore) -> None:
    store.append(_decision("dec_b", "2026-10-21T13:00:00+00:00", "accepted"))
    store.append(_decision("dec_a", "2026-10-21T12:00:00+00:00", "rejected"))
    history = store.for_record(PACKET)
    assert [d["decision_id"] for d in history] == ["dec_a", "dec_b"]
    assert store.for_record("pkt_nothing_yet") == []


def test_history_is_cached_and_refreshed_by_own_writes(
    api: FakeContentsAPI, store: GitHubDecisionStore
) -> None:
    store.append(_decision("dec_1", "2026-10-21T12:00:00+00:00"))
    store.for_record(PACKET)
    before = len(api.requests)
    store.for_record(PACKET)
    assert len(api.requests) == before, "second read comes from the cache"
    store.append(_decision("dec_2", "2026-10-21T12:05:00+00:00"))
    assert len(store.for_record(PACKET)) == 2


def test_github_errors_are_reported(api: FakeContentsAPI, store: GitHubDecisionStore) -> None:
    api.fail_next = 500
    with pytest.raises(DecisionStoreError, match="500"):
        store.append(_decision("dec_1", "2026-10-21T12:00:00+00:00"))
    bad = GitHubDecisionStore(REPO, "wrong-token", transport=api.transport())
    with pytest.raises(DecisionStoreError, match="401"):
        bad.append(_decision("dec_2", "2026-10-21T12:00:00+00:00"))


def test_directory_store_reads_a_checkout_of_the_decisions_repo(tmp_path: Path) -> None:
    store = DirectoryDecisionStore(tmp_path)
    store.append(_decision("dec_b", "2026-10-21T13:00:00+00:00", "accepted"))
    store.append(_decision("dec_a", "2026-10-21T12:00:00+00:00", "rejected"))
    assert (tmp_path / decision_path(_decision("dec_a", "2026-10-21T12:00:00+00:00"))).is_file()
    assert [d["decision_id"] for d in store.for_record(PACKET)] == ["dec_a", "dec_b"]
    assert store.latest()[PACKET]["decision_id"] == "dec_b"
    assert DirectoryDecisionStore(tmp_path / "empty").latest() == {}


OTHER = "pkt_e27a7e05d319"


def test_decisions_since_a_time_cost_one_request_when_nothing_is_new(
    api: FakeContentsAPI, store: GitHubDecisionStore
) -> None:
    """What the list asks for: sign-offs made after its build, cheaply, for many curators."""
    store.append(_decision("dec_1", "2026-10-21T12:00:00+00:00"))
    store.append({**_decision("dec_2", "2026-10-21T12:05:00+00:00"), "packet_id": OTHER})
    store.append(_decision("dec_3", "2026-10-21T12:10:00+00:00", outcome="rejected"))
    recent = store.since("2026-10-21T12:00:00+00:00")
    assert sorted(d["decision_id"] for d in recent) == ["dec_2", "dec_3"]
    assert sorted(d["decision_id"] for d in store.since(None)) == ["dec_1", "dec_2", "dec_3"]
    # Nothing new: one listing of commits, no decision files read again.
    store.ttl_recent = 0
    api.requests.clear()
    store.since(None)
    assert [r.url.path for r in api.requests] == [f"/repos/{REPO}/commits"]
    # A new sign-off: only that record's history is read.
    store.append({**_decision("dec_4", "2026-10-21T12:20:00+00:00"), "packet_id": OTHER})
    api.requests.clear()
    assert "dec_4" in [d["decision_id"] for d in store.since("2026-10-21T12:15:00+00:00")]
    paths = [r.url.path for r in api.requests]
    assert paths[0] == f"/repos/{REPO}/commits"
    assert all(OTHER in p for p in paths[1:]) and len(paths) > 1


def test_recent_decisions_are_cached_briefly(
    api: FakeContentsAPI, store: GitHubDecisionStore
) -> None:
    store.append(_decision("dec_1", "2026-10-21T12:00:00+00:00"))
    store.since(None)
    api.requests.clear()
    store.since(None)
    assert api.requests == []


def test_fresh_history_skips_the_cache(api: FakeContentsAPI, store: GitHubDecisionStore) -> None:
    """A sign-off checks for decisions made since the curator looked, on any instance."""
    other = GitHubDecisionStore(REPO, TOKEN, transport=api.transport())
    assert other.for_record(PACKET) == []
    store.append(_decision("dec_1", "2026-10-21T12:00:00+00:00"))
    assert other.for_record(PACKET) == []  # cached
    assert [d["decision_id"] for d in other.for_record(PACKET, fresh=True)] == ["dec_1"]


def test_a_github_app_signs_in_with_short_lived_installation_tokens() -> None:
    """The service holds GitHub App credentials, not a person's token (option b)."""
    from mondo_evidence_browser.decision_store import GitHubAppToken
    from tests.github_api import APP_ID, APP_PRIVATE_KEY

    api = FakeContentsAPI()
    app = GitHubAppToken(APP_ID, APP_PRIVATE_KEY, REPO, transport=api.transport())
    store = GitHubDecisionStore(REPO, app, transport=api.transport())
    store.append(_decision("dec_1", "2026-10-21T12:00:00+00:00"))
    assert [d["decision_id"] for d in store.for_record(PACKET, fresh=True)] == ["dec_1"]
    assert api.issued == ["ghs_installation_1"]  # one token, reused while it is valid
    assert all(
        r.headers["authorization"] == "Bearer ghs_installation_1"
        for r in api.requests
        if "/contents/" in r.url.path
    )


def test_installation_tokens_are_renewed_before_they_expire() -> None:
    from datetime import timedelta

    from mondo_evidence_browser.decision_store import GitHubAppToken
    from tests.github_api import APP_ID, APP_PRIVATE_KEY

    api = FakeContentsAPI(token_lifetime=timedelta(minutes=2))  # inside the renewal margin
    app = GitHubAppToken(APP_ID, APP_PRIVATE_KEY, REPO, transport=api.transport())
    store = GitHubDecisionStore(REPO, app, transport=api.transport())
    store.for_record(PACKET, fresh=True)
    store.for_record(PACKET, fresh=True)
    assert api.issued == ["ghs_installation_1", "ghs_installation_2"]


def test_a_wrong_app_key_is_reported() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from mondo_evidence_browser.decision_store import GitHubAppToken
    from tests.github_api import APP_ID

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    api = FakeContentsAPI()
    app = GitHubAppToken(APP_ID, other.decode(), REPO, transport=api.transport())
    store = GitHubDecisionStore(REPO, app, transport=api.transport())
    with pytest.raises(DecisionStoreError, match="401"):
        store.for_record(PACKET, fresh=True)
