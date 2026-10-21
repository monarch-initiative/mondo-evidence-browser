"""A stand-in for the parts of GitHub's REST contents API the decision store uses.

GitHub is an external system, so tests replace it at the HTTP transport. The stand-in
keeps files in memory and checks what the store actually sends (auth, branch,
base64 content, no overwrites).
"""

import base64
import json
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
        prefix = f"/repos/{REPO}/contents/"
        if not request.url.path.startswith(prefix):
            return httpx.Response(404, json={"message": "Not Found"})
        path = request.url.path[len(prefix) :]
        if request.method == "PUT":
            body = json.loads(request.content)
            if path in self.files:
                return httpx.Response(422, json={"message": "sha wasn't supplied"})
            self.files[path] = base64.b64decode(body["content"])
            self.commits.append({"path": path, **body})
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
