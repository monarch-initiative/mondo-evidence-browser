"""The sign-off service: ORCID id_token verification, schema-checked sign-offs."""

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mondo_evidence_browser.build import BuildConfig, build
from mondo_evidence_browser.index import EvidenceIndex
from mondo_evidence_browser.service import ServiceSettings, create_service
from tests.orcid import CARBERRY, CLIENT_ID, ORCID_BASE, STRANGER, id_token, jwks_endpoint

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = Path(__file__).parent.parent / "config"
PACKET = "pkt_9c18e79339f2"


@pytest.fixture
def settings(tmp_path: Path) -> ServiceSettings:
    build(
        BuildConfig(
            store_dir=FIXTURES,
            mondo_obo=FIXTURES / "mondo_subset.obo",
            config_dir=CONFIG,
            index_path=tmp_path / "evidence.db",
            site_dir=tmp_path / "site",
            decisions_path=tmp_path / "decisions.jsonl",
            as_of=date(2026, 10, 20),
            service_url="",
        )
    )
    return ServiceSettings(
        index_path=tmp_path / "evidence.db",
        decisions_path=tmp_path / "decisions.jsonl",
        config_dir=CONFIG,
        site_dir=tmp_path / "site",
        orcid_client_id=CLIENT_ID,
        orcid_base_url=ORCID_BASE,
        allowed_origins=("https://monarch-initiative.github.io",),
    )


@pytest.fixture
def client(settings: ServiceSettings) -> TestClient:
    return TestClient(create_service(settings, orcid_transport=jwks_endpoint()))


def _body(client: TestClient, **values: Any) -> dict[str, Any]:
    record = client.get(f"/api/records/{PACKET}").json()["record"]
    return {
        "seen_version": record["record_version"],
        "seen_rule_version": record["inclusion_score"]["rule_version"],
        "values": {"outcome": "accepted", "rationale": "Two sources list it.", **values},
    }


def _post(client: TestClient, body: dict[str, Any], token: str | None = None) -> Any:
    headers = {"Authorization": f"Bearer {token or id_token()}"}
    return client.post(f"/api/records/{PACKET}/sign-off", json=body, headers=headers)


def test_health(client: TestClient) -> None:
    data = client.get("/api/health").json()
    assert data["status"] == "ok"
    assert data["records"] == 23
    assert data["schema_version"] == "0.1.0"


def test_curator_signs_off(client: TestClient, settings: ServiceSettings) -> None:
    response = _post(client, _body(client))
    assert response.status_code == 201, response.text
    decision = response.json()["decision"]
    assert decision["outcome"] == "accepted"
    assert decision["rationale"] == "Two sources list it."
    assert decision["curator"]["orcid"] == f"ORCID:{CARBERRY}"
    assert decision["curator"]["name"] == "Josiah Carberry"
    assert decision["curator"]["trust"] == 0.85
    logged = [json.loads(x) for x in settings.decisions_path.read_text().splitlines()]
    assert logged == [decision]
    history = client.get(f"/api/records/{PACKET}/decisions").json()["decisions"]
    assert history == [decision]
    record = EvidenceIndex(settings.index_path).get(PACKET)
    assert record is not None and record["review_status"] == "accepted"


def test_decisions_are_valid_against_the_schema(client: TestClient) -> None:
    from linkml.validator import validate

    from mondo_evidence_browser.viewspec import SCHEMA_PATH

    decision = _post(client, _body(client)).json()["decision"]
    report = validate(decision, str(SCHEMA_PATH), "Decision")
    assert not report.results, [r.message for r in report.results]


@pytest.mark.parametrize(
    ("token", "fragment"),
    [
        (None, "missing"),
        ("not-a-jwt", "token"),
        ("forged", "signature"),
        ("expired", "expired"),
        ("wrong-audience", "audience"),
        ("wrong-issuer", "issuer"),
    ],
)
def test_invalid_tokens_are_rejected(
    client: TestClient, settings: ServiceSettings, token: str | None, fragment: str
) -> None:
    tokens = {
        "not-a-jwt": "not-a-jwt",
        "forged": id_token(forged=True),
        "expired": id_token(expires_in=-60),
        "wrong-audience": id_token(aud="APP-SOMEONE-ELSE"),
        "wrong-issuer": id_token(iss="https://orcid.example.org"),
    }
    headers = {"Authorization": f"Bearer {tokens[token]}"} if token else {}
    response = client.post(f"/api/records/{PACKET}/sign-off", json=_body(client), headers=headers)
    assert response.status_code == 401
    assert fragment in response.json()["detail"].lower()
    assert not settings.decisions_path.exists()


def test_people_outside_the_curator_registry_cannot_sign_off(client: TestClient) -> None:
    response = _post(client, _body(client), token=id_token(sub=STRANGER))
    assert response.status_code == 403
    assert "not a curator" in response.json()["detail"]


def test_form_values_are_checked_against_the_schema(client: TestClient) -> None:
    assert _post(client, _body(client, outcome="maybe")).status_code == 422
    assert _post(client, _body(client, colour="red")).status_code == 422
    body = _body(client)
    del body["values"]["outcome"]
    response = _post(client, body)
    assert response.status_code == 422 and "outcome is required" in response.json()["detail"]


def test_outdated_view_is_refused(client: TestClient) -> None:
    body = {**_body(client), "seen_version": "v_0000000000000000"}
    assert _post(client, body).status_code == 409


def test_unknown_record(client: TestClient) -> None:
    response = client.post(
        "/api/records/pkt_nope/sign-off",
        json=_body(client),
        headers={"Authorization": f"Bearer {id_token()}"},
    )
    assert response.status_code == 404
    assert client.get("/api/records/pkt_nope/decisions").status_code == 404


def test_cors_allows_the_configured_site(client: TestClient) -> None:
    response = client.options(
        f"/api/records/{PACKET}/sign-off",
        headers={
            "Origin": "https://monarch-initiative.github.io",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.headers["access-control-allow-origin"] == "https://monarch-initiative.github.io"
    other = client.options(
        f"/api/records/{PACKET}/sign-off",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in other.headers


def test_the_service_serves_the_static_site(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert re.search(r'"assets/[0-9a-f]{12}/list\.js"', page.text)
    record_page = client.get("/record.html", params={"id": PACKET})
    assert record_page.status_code == 200
    chunk = client.get("/data/evidence.db", headers={"Range": "bytes=0-99"})
    assert chunk.status_code == 206 and chunk.content.startswith(b"SQLite format 3")
    config = client.get("/config.json").json()
    assert config["service"] == {"url": ""}
