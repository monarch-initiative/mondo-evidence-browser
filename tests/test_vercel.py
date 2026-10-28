"""The Vercel entrypoint: the sign-off service, configured only from the environment.

Vercel loads the top-level ``app`` from ``app.py`` at the repository root and runs it
as one serverless function. That function keeps nothing on disk, so it must read
records from the lookup shards and write decisions to GitHub, never fall back to a
local index or log, and never serve the static site (GitHub Pages does that).
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

ROOT = Path(__file__).parent.parent

STATELESS = {
    "MEB_LOOKUP_URL": "https://data.example.org/current/lookup/",
    "MEB_DECISIONS_REPO": "monarch-initiative/mondo-evidence-decisions",
    "MEB_GITHUB_TOKEN": "not-a-real-token",
    "ORCID_CLIENT_ID": "APP-TEST",
    "MEB_ALLOWED_ORIGINS": "https://monarch-initiative.github.io",
}


def _load(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> ModuleType:
    """Import ``app.py`` afresh, as Vercel does, from the repository root."""
    for name in [*STATELESS, "MEB_SITE", "MEB_GITHUB_APP_ID", "MEB_GITHUB_APP_PRIVATE_KEY"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(ROOT)  # Vercel runs from the project root
    spec = importlib.util.spec_from_file_location("vercel_entrypoint", ROOT / "app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "vercel_entrypoint", module)
    spec.loader.exec_module(module)
    return module


def test_the_entrypoint_is_the_sign_off_service(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _load(monkeypatch, STATELESS).app
    assert isinstance(app, FastAPI)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/api/health", "/api/records/{record_id}/sign-off"} <= paths


def test_it_never_serves_the_static_site(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even where a built ``site/`` sits next to it, as in a local checkout."""
    app = _load(monkeypatch, {**STATELESS, "MEB_SITE": str(ROOT / "frontend")}).app
    assert not [route for route in app.routes if isinstance(route, Mount)]


def test_it_allows_the_pages_site_to_call_it(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(_load(monkeypatch, STATELESS).app)
    response = client.options(
        "/api/records/pkt_x/sign-off",
        headers={
            "Origin": "https://monarch-initiative.github.io",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert response.headers["access-control-allow-origin"] == "https://monarch-initiative.github.io"


@pytest.mark.parametrize("missing", ["MEB_LOOKUP_URL", "MEB_DECISIONS_REPO"])
def test_it_refuses_to_start_with_local_state(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """A serverless function's disk is thrown away, so local files would lose sign-offs."""
    env = {k: v for k, v in STATELESS.items() if k != missing}
    with pytest.raises(RuntimeError, match=missing):
        _load(monkeypatch, env)
