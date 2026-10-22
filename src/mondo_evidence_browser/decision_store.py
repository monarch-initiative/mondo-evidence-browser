"""Where sign-offs are kept: one JSON file per decision, in git.

Each decision is its own file at ``decisions/<shard>/<record id>/<time>_<decision id>.json``.
Two curators signing off at the same moment never touch the same file, nothing is
ever rewritten, and a record's history is one directory listing. The same layout
works in two places:

- :class:`GitHubDecisionStore`: the sign-off service commits each decision to a GitHub
  repository through the REST contents API. It signs in as a GitHub App installed
  only on that repository (:class:`GitHubAppToken`: hour-long tokens, commits by the
  app's bot), or with a fine-grained personal token. Free, durable and versioned.
- :class:`DirectoryDecisionStore`: a checkout of that repository, which the build reads
  to set review statuses (and which local runs and tests can write to).

The append-only JSONL log in :mod:`mondo_evidence_browser.decisions` stays for local use.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Generator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt

from mondo_evidence_browser.lookup import shard_of

API = "https://api.github.com"


# Commit dates and decision times can differ by a little (clocks, retries), so the
# commits listed for "since T" start this much before T; extra ones are filtered out.
SINCE_MARGIN = timedelta(minutes=10)
COMMIT_PAGES = 10  # at most 1,000 commits since a build, 100 per request


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


def commit_message(decision: Mapping[str, Any]) -> str:
    """The commit message of a decision; it names the record, for :func:`record_of`.

    >>> commit_message({"outcome": "accepted", "packet_id": "pkt_1",
    ...                 "curator": {"orcid": "ORCID:0000-0002-1825-0097"}})
    'Sign-off: accepted on pkt_1 by ORCID:0000-0002-1825-0097'
    """
    curator = (decision.get("curator") or {}).get("orcid")
    return f"Sign-off: {decision.get('outcome')} on {decision['packet_id']} by {curator}"


def record_of(message: str) -> str | None:
    """The record a decision commit is about, from its message (None for other commits).

    >>> record_of("Sign-off: accepted on pkt_1 by ORCID:0000-0002-1825-0097")
    'pkt_1'
    >>> record_of("Add README") is None
    True
    """
    found = re.match(r"Sign-off: \S+ on (\S+) by ", message)
    return found.group(1) if found else None


class GitHubAppToken:
    """Installation tokens for a GitHub App, renewed before they expire.

    The app's private key signs a ten-minute JWT; GitHub exchanges it for an
    installation token valid for an hour, limited to the repositories (and
    permissions) the app is installed with. Nothing here belongs to a person.
    """

    RENEW_BEFORE = timedelta(minutes=5)

    def __init__(
        self,
        app_id: str,
        private_key: str,
        repo: str,
        installation_id: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Sign in as app ``app_id`` on the installation that covers ``repo``."""
        self.app_id = app_id
        self.repo = repo
        self._key = private_key
        self._installation = installation_id
        self._token: str | None = None
        self._expires = datetime.min.replace(tzinfo=UTC)
        self._client = httpx.Client(
            base_url=API,
            transport=transport,
            timeout=20,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )

    def _app_jwt(self) -> str:
        now = int(time.time())
        claims = {"iat": now - 60, "exp": now + 540, "iss": self.app_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    def _call(self, method: str, url: str) -> dict[str, Any]:
        response = self._client.request(
            method, url, headers={"Authorization": f"Bearer {self._app_jwt()}"}
        )
        if response.status_code >= 400:
            message = response.json().get("message", "") if response.content else ""
            raise DecisionStoreError(
                f"GitHub refused the app sign-in (HTTP {response.status_code}): {message}"
            )
        body: dict[str, Any] = response.json()
        return body

    def token(self) -> str:
        """A valid installation token, renewing it when it is about to expire."""
        if self._token is None or self._expires - datetime.now(UTC) < self.RENEW_BEFORE:
            if self._installation is None:
                self._installation = int(
                    self._call("GET", f"/repos/{self.repo}/installation")["id"]
                )
            issued = self._call("POST", f"/app/installations/{self._installation}/access_tokens")
            self._token = str(issued["token"])
            self._expires = datetime.fromisoformat(issued["expires_at"].replace("Z", "+00:00"))
        return self._token


class _Bearer(httpx.Auth):
    """A fixed token, or a GitHub App's current installation token, on every request."""

    def __init__(self, credentials: str | GitHubAppToken) -> None:
        self.credentials = credentials

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        token = self.credentials if isinstance(self.credentials, str) else self.credentials.token()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


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

    def for_record(self, record_id: str, fresh: bool = False) -> list[dict[str, Any]]:
        """A record's decisions, oldest first (files are always read fresh)."""
        return [json.loads(p.read_text()) for p in self._files(self.root / _record_dir(record_id))]

    def since(self, when: str | None) -> list[dict[str, Any]]:
        """Decisions made after ``when`` (an ISO time; None for all)."""
        decisions = (json.loads(p.read_text()) for p in self._files(self.root / "decisions"))
        return [d for d in decisions if when is None or d["decided_at"] > when]

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
        token: str | GitHubAppToken,
        branch: str = "main",
        transport: httpx.BaseTransport | None = None,
        ttl: float = 60.0,
        ttl_recent: float = 30.0,
    ) -> None:
        """Write to ``repo`` ("owner/name") on ``branch`` as a GitHub App or with a token.

        Histories are cached for ``ttl`` seconds and the list of recent sign-offs for
        ``ttl_recent``, which bounds GitHub API use whatever the number of curators.
        """
        self.repo = repo
        self.branch = branch
        self.ttl = ttl
        self.ttl_recent = ttl_recent
        self._client = httpx.Client(
            base_url=API,
            transport=transport,
            timeout=20,
            auth=_Bearer(token),
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        self._history: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._files: dict[str, dict[str, Any]] = {}  # decision files never change
        self._seen_commits: set[str] = set()
        self._recent: dict[str, list[dict[str, Any]]] = {}  # history of recently decided
        self._recent_from: str | None = None  # the earliest time _recent covers
        self._recent_at: float | None = None  # when the commits were last listed

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
        response = self._client.put(
            self._url(decision_path(decision)),
            json={
                "message": commit_message(decision),
                "content": base64.b64encode(content.encode()).decode(),
                "branch": self.branch,
            },
        )
        self._check(response, "store the decision")
        self._history.pop(str(decision["packet_id"]), None)

    def for_record(self, record_id: str, fresh: bool = False) -> list[dict[str, Any]]:
        """A record's decisions, oldest first (cached for ``ttl`` seconds unless ``fresh``)."""
        cached = self._history.get(record_id)
        if not fresh and cached is not None and time.monotonic() - cached[0] < self.ttl:
            return cached[1]
        listing = self._client.get(self._url(_record_dir(record_id)), params={"ref": self.branch})
        if listing.status_code == 404:
            history: list[dict[str, Any]] = []
        else:
            self._check(listing, "list the decisions")
            history = [
                self._read(e["path"]) for e in sorted(listing.json(), key=lambda e: e["name"])
            ]
        self._history[record_id] = (time.monotonic(), history)
        return history

    def _read(self, path: str) -> dict[str, Any]:
        if path not in self._files:
            response = self._client.get(self._url(path), params={"ref": self.branch})
            self._check(response, "read a decision")
            self._files[path] = json.loads(base64.b64decode(response.json()["content"]))
        return self._files[path]

    def since(self, when: str | None) -> list[dict[str, Any]]:
        """Decisions made after ``when`` (an ISO time; None for all).

        One request lists the commits since then; only records with commits not seen
        before are read again. The result is cached for ``ttl_recent`` seconds.
        """
        covered = self._recent_at is not None and (
            self._recent_from is None or (when is not None and when >= self._recent_from)
        )
        stale = self._recent_at is None or time.monotonic() - self._recent_at >= self.ttl_recent
        if stale or not covered:
            self._poll(when, exhaustive=not covered)
            if not covered:
                self._recent_from = when
        return [
            d
            for history in self._recent.values()
            for d in history
            if when is None or d["decided_at"] > when
        ]

    def latest(self) -> dict[str, dict[str, Any]]:
        """The current (latest) decision per record."""
        latest: dict[str, dict[str, Any]] = {}
        for decision in sorted(self.since(None), key=lambda d: d["decided_at"]):
            latest[decision["packet_id"]] = decision
        return latest

    def _poll(self, when: str | None, exhaustive: bool) -> None:
        params: dict[str, Any] = {"sha": self.branch, "path": "decisions", "per_page": 100}
        if when is not None:
            start = datetime.fromisoformat(when).astimezone(UTC) - SINCE_MARGIN
            params["since"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        new: dict[str, str] = {}  # commit -> record, for commits not seen before
        for page in range(1, COMMIT_PAGES + 1):  # newest first
            response = self._client.get(
                f"/repos/{self.repo}/commits", params={**params, "page": page}
            )
            self._check(response, "list recent sign-offs")
            commits = response.json()
            unseen = [c for c in commits if c["sha"] not in self._seen_commits]
            for commit in unseen:
                record = record_of(commit["commit"]["message"])
                if record is not None:
                    new[commit["sha"]] = record
            if len(commits) < params["per_page"]:
                break  # the last page
            if not exhaustive and len(unseen) < len(commits):
                break  # the rest was seen by an earlier poll
        for record in set(new.values()):
            self._recent[record] = self.for_record(record, fresh=True)
        self._seen_commits |= set(new)
        self._recent_at = time.monotonic()
