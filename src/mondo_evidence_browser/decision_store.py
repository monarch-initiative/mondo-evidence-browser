"""Where sign-offs are kept: one JSON file per decision, in git.

Each decision is its own file at ``decisions/<shard>/<record id>/<time>_<decision id>.json``.
Two curators signing off at the same moment never touch the same file, nothing is
ever rewritten, and a record's history is one directory listing. The same layout
works in two places:

- :class:`GitHubDecisionStore`: the sign-off service commits each decision to a GitHub
  repository through the REST contents API (a fine-grained token that can write only
  to that repository). Free, durable and versioned.
- :class:`DirectoryDecisionStore`: a checkout of that repository, which the build reads
  to set review statuses (and which local runs and tests can write to).

The append-only JSONL log in :mod:`mondo_evidence_browser.decisions` stays for local use.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx

from mondo_evidence_browser.lookup import shard_of

API = "https://api.github.com"


class DecisionStoreError(RuntimeError):
    """The decision could not be stored or read."""


def decision_path(decision: Mapping[str, Any]) -> str:
    """Repository path of a decision: per record, ordered by time, never colliding.

    >>> decision_path({"packet_id": "pkt_1", "decision_id": "dec_9",
    ...                "decided_at": "2026-10-21T12:00:00+00:00"})
    'decisions/4b6/pkt_1/2026-10-21T12-00-00+00-00_dec_9.json'
    """
    record = str(decision["packet_id"])
    stamp = str(decision["decided_at"]).replace(":", "-")
    return f"decisions/{shard_of(record)}/{record}/{stamp}_{decision['decision_id']}.json"


def _record_dir(record_id: str) -> str:
    return f"decisions/{shard_of(record_id)}/{record_id}"


class DirectoryDecisionStore:
    """Decisions as files under ``root`` (a checkout of the decisions repository)."""

    def __init__(self, root: Path) -> None:
        """Use ``root/decisions/...``."""
        self.root = root

    def append(self, decision: Mapping[str, Any]) -> None:
        """Write one decision file; an existing file is never overwritten."""
        path = self.root / decision_path(decision)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x") as f:
            json.dump(dict(decision), f, ensure_ascii=False, sort_keys=True, indent=1)

    def _files(self, directory: Path) -> Iterator[Path]:
        return iter(sorted(directory.glob("**/*.json"))) if directory.exists() else iter(())

    def for_record(self, record_id: str) -> list[dict[str, Any]]:
        """A record's decisions, oldest first."""
        return [json.loads(p.read_text()) for p in self._files(self.root / _record_dir(record_id))]

    def latest(self) -> dict[str, dict[str, Any]]:
        """The current (latest) decision per record."""
        latest: dict[str, dict[str, Any]] = {}
        for path in self._files(self.root / "decisions"):
            decision = json.loads(path.read_text())
            latest[decision["packet_id"]] = decision  # files sort by time within a record
        return latest


class GitHubDecisionStore:
    """Decisions committed to a GitHub repository, one file each."""

    def __init__(
        self,
        repo: str,
        token: str,
        branch: str = "main",
        transport: httpx.BaseTransport | None = None,
        ttl: float = 60.0,
    ) -> None:
        """Write to ``repo`` ("owner/name") on ``branch`` with a token that may write there."""
        self.repo = repo
        self.branch = branch
        self.ttl = ttl
        self._client = httpx.Client(
            base_url=API,
            transport=transport,
            timeout=20,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self._history: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def _url(self, path: str) -> str:
        return f"/repos/{self.repo}/contents/{path}"

    def _check(self, response: httpx.Response, action: str) -> None:
        if response.status_code >= 400:
            message = response.json().get("message", "") if response.content else ""
            raise DecisionStoreError(
                f"GitHub refused to {action} (HTTP {response.status_code}): {message}"
            )

    def append(self, decision: Mapping[str, Any]) -> None:
        """Commit one decision as a new file."""
        content = json.dumps(dict(decision), ensure_ascii=False, sort_keys=True, indent=1)
        message = (
            f"Sign-off: {decision.get('outcome')} on {decision['packet_id']} "
            f"by {(decision.get('curator') or {}).get('orcid')}"
        )
        response = self._client.put(
            self._url(decision_path(decision)),
            json={
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "branch": self.branch,
            },
        )
        self._check(response, "store the decision")
        self._history.pop(str(decision["packet_id"]), None)

    def for_record(self, record_id: str) -> list[dict[str, Any]]:
        """A record's decisions, oldest first (cached for ``ttl`` seconds)."""
        cached = self._history.get(record_id)
        if cached is not None and time.monotonic() - cached[0] < self.ttl:
            return cached[1]
        listing = self._client.get(self._url(_record_dir(record_id)), params={"ref": self.branch})
        if listing.status_code == 404:
            history: list[dict[str, Any]] = []
        else:
            self._check(listing, "list the decisions")
            history = []
            for entry in sorted(listing.json(), key=lambda e: e["name"]):
                response = self._client.get(self._url(entry["path"]), params={"ref": self.branch})
                self._check(response, "read a decision")
                history.append(json.loads(base64.b64decode(response.json()["content"])))
        self._history[record_id] = (time.monotonic(), history)
        return history
