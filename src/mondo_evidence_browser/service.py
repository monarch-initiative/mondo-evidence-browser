"""The sign-off service: the one part of the browser that cannot be static.

The static site logs curators in with ORCID (OpenID Connect implicit flow) and posts
sign-offs here with the ORCID id_token as a bearer token. The service verifies the
token's signature against ORCID's published keys, its audience (this deployment's
ORCID client id), issuer and expiry; checks the curator registry; checks the form
values against the schema-derived form; and appends the decision to the log.

It can also serve the static site itself, which is how ``just serve`` runs
everything locally. In a free, stateless deployment (a Vercel function, ``app.py``) it keeps
nothing on disk: records come from the build's lookup shards (``MEB_LOOKUP_URL``) and
decisions are committed to a GitHub repository (``MEB_DECISIONS_REPO``).

=========================  =====================================================
``MEB_INDEX``              SQLite index (default ``var/evidence.db``)
``MEB_DECISIONS``          decision log (default ``var/decisions.jsonl``)
``MEB_CONFIG``             score rule + registries (default ``config``)
``MEB_SITE``               static site to serve, if any (default ``site``)
``MEB_ALLOWED_ORIGINS``    comma-separated origins allowed to call the API (CORS)
``MEB_LOOKUP_URL``         lookup shards URL (``…/lookup/``); replaces the local index
``MEB_DECISIONS_REPO``     GitHub repository ("owner/name") to commit decisions to;
                           replaces the local log
``MEB_DECISIONS_BRANCH``   its branch (default ``main``)
``MEB_GITHUB_TOKEN``       fine-grained token that can write only to that repository
``ORCID_CLIENT_ID``        ORCID API client id (the id_token audience)
``ORCID_BASE_URL``         ``https://sandbox.orcid.org`` (default) or ``https://orcid.org``
=========================  =====================================================
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mondo_evidence_browser.decision_store import DecisionStoreError, GitHubDecisionStore
from mondo_evidence_browser.decisions import DecisionLog, Refusal, sign_off
from mondo_evidence_browser.index import EvidenceIndex
from mondo_evidence_browser.lookup import IndexSource, LookupUnavailable, ShardLookup
from mondo_evidence_browser.registry import ORCID_PATTERN, Registry, normalise_orcid
from mondo_evidence_browser.viewspec import SCHEMA_PATH, build_view_spec


@dataclass(frozen=True)
class ServiceSettings:
    """Everything the service needs."""

    index_path: Path
    decisions_path: Path
    config_dir: Path
    schema_path: Path = SCHEMA_PATH
    site_dir: Path | None = None
    orcid_client_id: str | None = None
    orcid_base_url: str = "https://sandbox.orcid.org"
    allowed_origins: tuple[str, ...] = ()
    lookup_url: str | None = None
    decisions_repo: str | None = None
    decisions_branch: str = "main"
    github_token: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServiceSettings:
        """Read settings from environment variables (see the module docstring).

        >>> s = ServiceSettings.from_env({"MEB_ALLOWED_ORIGINS": "https://a.org, https://b.org"})
        >>> s.allowed_origins, s.orcid_client_id
        (('https://a.org', 'https://b.org'), None)
        """
        # Values pasted into a host's settings page often carry a space or a newline.
        env = {k: v.strip() for k, v in (os.environ if env is None else env).items()}
        site = Path(env.get("MEB_SITE", "site"))
        origins = env.get("MEB_ALLOWED_ORIGINS", "")
        return cls(
            index_path=Path(env.get("MEB_INDEX", "var/evidence.db")),
            decisions_path=Path(env.get("MEB_DECISIONS", "var/decisions.jsonl")),
            config_dir=Path(env.get("MEB_CONFIG", "config")),
            site_dir=site if site.exists() else None,
            orcid_client_id=env.get("ORCID_CLIENT_ID") or None,
            orcid_base_url=env.get("ORCID_BASE_URL", "https://sandbox.orcid.org").rstrip("/"),
            allowed_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
            lookup_url=env.get("MEB_LOOKUP_URL") or None,
            decisions_repo=env.get("MEB_DECISIONS_REPO") or None,
            decisions_branch=env.get("MEB_DECISIONS_BRANCH", "main"),
            github_token=env.get("MEB_GITHUB_TOKEN") or None,
        )


@dataclass(frozen=True)
class Identity:
    """The curator an id_token identifies."""

    orcid: str
    name: str | None


class OrcidVerifier:
    """Verify ORCID id_tokens against ORCID's published signing keys."""

    def __init__(
        self, base_url: str, client_id: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Verify tokens issued by ``base_url`` for ``client_id``."""
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.transport = transport
        self._keys: dict[str, Any] | None = None

    async def _fetch_keys(self) -> dict[str, Any]:
        async with httpx.AsyncClient(transport=self.transport, timeout=15) as client:
            response = await client.get(f"{self.base_url}/oauth/jwks")
        response.raise_for_status()
        return {k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(k) for k in response.json()["keys"]}

    async def verify(self, token: str) -> Identity | str:
        """The identity in a valid token, or why the token is not valid."""
        try:  # the token is untrusted input from the network
            kid = str(jwt.get_unverified_header(token).get("kid"))
        except jwt.PyJWTError:
            return "The ORCID token is not a valid token."
        if self._keys is None or kid not in self._keys:
            try:  # ORCID is an external system; it may be unreachable
                self._keys = await self._fetch_keys()
            except httpx.HTTPError:
                return "Could not fetch ORCID's signing keys to check the token."
        key = self._keys.get(kid)
        if key is None:
            return "The ORCID token signature uses an unknown key."
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=self.base_url,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError:
            return "The ORCID login has expired; log in again."
        except jwt.InvalidAudienceError:
            return (
                "The ORCID token was issued for another application (audience); "
                f"this service expects ORCID client {self.client_id}."
            )
        except jwt.InvalidIssuerError:
            return "The ORCID token was not issued by the configured ORCID (issuer)."
        except jwt.InvalidSignatureError:
            return "The ORCID token signature is not valid."
        except jwt.PyJWTError as exc:
            return f"The ORCID token is not valid ({type(exc).__name__})."
        if not ORCID_PATTERN.search(str(claims["sub"])):
            return "The ORCID token does not identify an ORCID iD."
        name = " ".join(p for p in (claims.get("given_name"), claims.get("family_name")) if p)
        return Identity(orcid=normalise_orcid(claims["sub"]), name=name or None)


class SignOffRequest(BaseModel):
    """What the record page sends: the version it showed, and the form values."""

    seen_version: str
    seen_rule_version: str | None = None
    values: dict[str, Any]


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def create_service(
    settings: ServiceSettings,
    orcid_transport: httpx.AsyncBaseTransport | None = None,
    lookup_transport: httpx.BaseTransport | None = None,
    github_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    """Build the service app. The transports stand in for ORCID, the data host and
    GitHub in tests."""
    spec = build_view_spec(settings.schema_path)
    records: ShardLookup | IndexSource = (
        ShardLookup(settings.lookup_url, transport=lookup_transport)
        if settings.lookup_url
        else IndexSource(EvidenceIndex(settings.index_path, spec))
    )
    if settings.decisions_repo and not settings.github_token:
        raise ValueError("MEB_DECISIONS_REPO is set but MEB_GITHUB_TOKEN is not")
    log: GitHubDecisionStore | DecisionLog = (
        GitHubDecisionStore(
            settings.decisions_repo,
            settings.github_token or "",
            branch=settings.decisions_branch,
            transport=github_transport,
        )
        if settings.decisions_repo
        else DecisionLog(settings.decisions_path)
    )
    registry = Registry.from_dir(settings.config_dir)
    verifier = (
        OrcidVerifier(settings.orcid_base_url, settings.orcid_client_id, orcid_transport)
        if settings.orcid_client_id
        else None
    )

    app = FastAPI(title="Mondo evidence browser: sign-off service", docs_url="/api/docs")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(LookupUnavailable)
    def lookups_unavailable(request: Any, error: LookupUnavailable) -> JSONResponse:
        detail = f"The record lookups cannot be read ({error}); check MEB_LOOKUP_URL."
        return JSONResponse(
            {"status": "unavailable", "detail": detail, "lookups": settings.lookup_url},
            status_code=503,
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        meta = records.meta()
        return {
            "status": "ok",
            "lookups": settings.lookup_url,
            "records": meta["record_count"],
            "schema_version": meta["schema_version"],
            "mondo_release": meta.get("mondo_release"),
            "rule_version": meta.get("rule_version"),
            "orcid": bool(verifier),
            "orcid_client_id": settings.orcid_client_id,
        }

    @app.get("/api/records/{record_id}", response_model=None)
    def record(record_id: str) -> JSONResponse | dict[str, Any]:
        found = records.get(record_id)
        if found is None:
            return _error(404, f"No evidence record {record_id}.")
        return {"record": found}

    @app.get("/api/records/{record_id}/decisions", response_model=None)
    def decisions(record_id: str) -> JSONResponse | dict[str, Any]:
        if records.get(record_id) is None:
            return _error(404, f"No evidence record {record_id}.")
        try:  # GitHub is an external system
            return {"decisions": log.for_record(record_id)}
        except DecisionStoreError as error:
            return _error(503, str(error))

    @app.post("/api/records/{record_id}/sign-off", status_code=201, response_model=None)
    async def record_sign_off(
        record_id: str,
        body: SignOffRequest,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse | dict[str, Any]:
        if verifier is None or spec["form"] is None:
            return _error(503, "Sign-off is not configured on this service (ORCID_CLIENT_ID).")
        if not authorization or not authorization.lower().startswith("bearer "):
            return _error(401, "The ORCID token is missing; log in with ORCID.")
        identity = await verifier.verify(authorization.split(" ", 1)[1].strip())
        if isinstance(identity, str):
            return _error(401, identity)
        try:  # the decision store may be GitHub, an external system
            result = sign_off(
                records,
                log,
                registry,
                spec["form"],
                record_id=record_id,
                seen_version=body.seen_version,
                seen_rule_version=body.seen_rule_version,
                values=body.values,
                orcid=identity.orcid,
                name=identity.name,
                now=datetime.now(UTC).replace(microsecond=0),
            )
        except DecisionStoreError as error:
            return _error(503, f"The sign-off could not be saved: {error}")
        if isinstance(result, Refusal):
            return _error(result.status_code, result.message)
        return {"decision": result}

    if settings.site_dir is not None:
        app.mount("/", StaticFiles(directory=settings.site_dir, html=True), name="site")
    return app
