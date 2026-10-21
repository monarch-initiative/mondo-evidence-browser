"""A stand-in for the parts of GitHub's REST API the decision store uses.

GitHub is an external system, so tests replace it at the HTTP transport. The stand-in
keeps files and commits in memory and checks what the store actually sends (auth,
branch, base64 content, no overwrites), like the contents and commits endpoints.
"""

import base64
import json
from datetime import UTC, datetime
from typing import Any

import httpx

REPO = "yaseminbridges/mondo-evidence-decisions"
TOKEN = "github_pat_test"


class FakeContentsAPI:
    """In-memory repository files behind ``/repos/{REPO}/contents/{path}``."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.commits: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.fail_next: int | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if self.fail_next is not None:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, json={"message": "Server Error"})
        if request.url.path == f"/repos/{REPO}/commits" and request.method == "GET":
            return self._list_commits(request)
        prefix = f"/repos/{REPO}/contents/"
        if not request.url.path.startswith(prefix):
            return httpx.Response(404, json={"message": "Not Found"})
        path = request.url.path[len(prefix) :]
        if request.method == "PUT":
            body = json.loads(request.content)
            if path in self.files:
                return httpx.Response(422, json={"message": "sha wasn't supplied"})
            self.files[path] = base64.b64decode(body["content"])
            date = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            sha = f"{len(self.commits) + 1:040x}"
            self.commits.append({"path": path, "sha": sha, "date": date, **body})
            return httpx.Response(201, json={"content": {"path": path}})
        if request.method == "GET":
            if path in self.files:
                content = base64.b64encode(self.files[path]).decode()
                return httpx.Response(200, json={"type": "file", "path": path, "content": content})
            children = [p for p in self.files if p.startswith(path.rstrip("/") + "/")]
            if not children:
                return httpx.Response(404, json={"message": "Not Found"})
            listing = [{"type": "file", "path": p, "name": p.rsplit("/", 1)[-1]} for p in children]
            return httpx.Response(200, json=listing)
        return httpx.Response(405)

    def _list_commits(self, request: httpx.Request) -> httpx.Response:
        """Commits touching ``path``, newest first, since ``since``, paged by ``per_page``."""
        params = request.url.params
        found = [
            c
            for c in reversed(self.commits)
            if c["path"].startswith(params.get("path", ""))
            and c["date"] >= params.get("since", "")
            and c["branch"] == params.get("sha", "main")
        ]
        per_page, page = int(params.get("per_page", 30)), int(params.get("page", 1))
        chunk = found[(page - 1) * per_page : page * per_page]
        return httpx.Response(
            200,
            json=[
                {
                    "sha": c["sha"],
                    "commit": {"message": c["message"], "committer": {"date": c["date"]}},
                }
                for c in chunk
            ],
        )
