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


def _static(directory: Path) -> Any:
    """A static data host serving ``directory`` (like Hugging Face or Pages)."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        path = directory / request.url.path.rsplit("/lookup/", 1)[-1]
        if not path.is_file():
            return httpx.Response(404)
        return httpx.Response(200, content=path.read_bytes())

    return httpx.MockTransport(handler)


def test_stateless_service_uses_lookups_and_a_github_decision_store(
    settings: ServiceSettings, tmp_path: Path
) -> None:
    """How it runs on Vercel: no index, no local log, nothing on disk."""
    from tests.github_api import REPO, TOKEN, FakeContentsAPI

    assert settings.site_dir is not None
    lookup_dir = settings.site_dir / "data" / "lookup"
    github = FakeContentsAPI()
    stateless = ServiceSettings(
        index_path=tmp_path / "no-index-here.db",
        decisions_path=tmp_path / "no-log-here.jsonl",
        config_dir=CONFIG,
        orcid_client_id=CLIENT_ID,
        orcid_base_url=ORCID_BASE,
        lookup_url="https://huggingface.co/datasets/x/y/resolve/main/lookup/",
        decisions_repo=REPO,
        github_token=TOKEN,
    )
    client = TestClient(
        create_service(
            stateless,
            orcid_transport=jwks_endpoint(),
            lookup_transport=_static(lookup_dir),
            github_transport=github.transport(),
        )
    )
    assert client.get("/api/health").json()["records"] == 23
    entry = client.get(f"/api/records/{PACKET}").json()["record"]
    body = {
        "seen_version": entry["record_version"],
        "seen_rule_version": entry["inclusion_score"]["rule_version"],
        "values": {"outcome": "accepted", "rationale": "Checked."},
    }
    response = client.post(
        f"/api/records/{PACKET}/sign-off",
        json=body,
        headers={"Authorization": f"Bearer {id_token()}"},
    )
    assert response.status_code == 201, response.text
    (path,) = github.files
    assert f"/{PACKET}/" in path
    history = client.get(f"/api/records/{PACKET}/decisions").json()["decisions"]
    assert [d["rationale"] for d in history] == ["Checked."]
    assert not (tmp_path / "no-log-here.jsonl").exists()


def test_stateless_service_reports_github_failures(
    settings: ServiceSettings, tmp_path: Path
) -> None:
    from tests.github_api import REPO, FakeContentsAPI

    assert settings.site_dir is not None
    github = FakeContentsAPI()
    stateless = ServiceSettings(
        index_path=tmp_path / "none.db",
        decisions_path=tmp_path / "none.jsonl",
        config_dir=CONFIG,
        orcid_client_id=CLIENT_ID,
        orcid_base_url=ORCID_BASE,
        lookup_url="https://data.example.org/lookup/",
        decisions_repo=REPO,
        github_token="expired-token",
    )
    client = TestClient(
        create_service(
            stateless,
            orcid_transport=jwks_endpoint(),
            lookup_transport=_static(settings.site_dir / "data" / "lookup"),
            github_transport=github.transport(),
        )
    )
    entry = client.get(f"/api/records/{PACKET}").json()["record"]
    response = client.post(
        f"/api/records/{PACKET}/sign-off",
        json={
            "seen_version": entry["record_version"],
            "seen_rule_version": entry["inclusion_score"]["rule_version"],
            "values": {"outcome": "accepted"},
        },
        headers={"Authorization": f"Bearer {id_token()}"},
    )
    assert response.status_code == 503
    assert "401" in response.json()["detail"]


def _stateless_client(tmp_path: Path, lookup_url: str, transport: Any) -> TestClient:
    from tests.github_api import REPO, TOKEN, FakeContentsAPI

    stateless = ServiceSettings(
        index_path=tmp_path / "none.db",
        decisions_path=tmp_path / "none.jsonl",
        config_dir=CONFIG,
        orcid_client_id=CLIENT_ID,
        orcid_base_url=ORCID_BASE,
        lookup_url=lookup_url,
        decisions_repo=REPO,
        github_token=TOKEN,
    )
    return TestClient(
        create_service(
            stateless,
            orcid_transport=jwks_endpoint(),
            lookup_transport=transport,
            github_transport=FakeContentsAPI().transport(),
        )
    )


def test_health_says_which_lookup_url_is_missing(tmp_path: Path) -> None:
    """A wrong MEB_LOOKUP_URL must be obvious from /api/health, not a bare 500."""
    url = "https://data.example.org/wrong/lookup/"
    client = _stateless_client(tmp_path, url, _static(tmp_path / "nothing-here"))
    response = client.get("/api/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert f"{url}meta.json" in body["detail"] and "404" in body["detail"]


def test_health_reports_a_failing_data_host(settings: ServiceSettings, tmp_path: Path) -> None:
    import httpx

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    client = _stateless_client(
        tmp_path, "https://data.example.org/lookup/", httpx.MockTransport(refuse)
    )
    response = client.get("/api/health")
    assert response.status_code == 503 and "429" in response.json()["detail"]
    record = client.get(f"/api/records/{PACKET}")
    assert record.status_code == 503 and "429" in record.json()["detail"]


def test_health_names_the_lookup_url_it_reads(settings: ServiceSettings, tmp_path: Path) -> None:
    assert settings.site_dir is not None
    url = "https://data.example.org/lookup/"
    client = _stateless_client(tmp_path, url, _static(settings.site_dir / "data" / "lookup"))
    assert client.get("/api/health").json()["lookups"] == url


def test_settings_ignore_stray_whitespace_from_pasting() -> None:
    """Values pasted into a host's settings page often carry a space or a newline."""
    settings = ServiceSettings.from_env(
        {
            "ORCID_CLIENT_ID": " APP-DDVDTWRWPWISP0BC\n",
            "ORCID_BASE_URL": "https://sandbox.orcid.org/ ",
            "MEB_LOOKUP_URL": "https://data.example.org/lookup/\n",
            "MEB_DECISIONS_REPO": " owner/name ",
            "MEB_GITHUB_TOKEN": "token\n",
        }
    )
    assert settings.orcid_client_id == "APP-DDVDTWRWPWISP0BC"
    assert settings.orcid_base_url == "https://sandbox.orcid.org"
    assert settings.lookup_url == "https://data.example.org/lookup/"
    assert settings.decisions_repo == "owner/name"
    assert settings.github_token == "token"


def test_health_and_audience_errors_name_the_expected_client(client: TestClient) -> None:
    """So a mismatch between the site's and the service's ORCID client is obvious."""
    assert client.get("/api/health").json()["orcid_client_id"] == CLIENT_ID
    response = _post(client, _body(client), token=id_token(aud="APP-SOMEONE-ELSE"))
    assert response.status_code == 401
    assert CLIENT_ID in response.json()["detail"]


def test_a_sign_off_after_someone_elses_is_refused_until_seen(client: TestClient) -> None:
    """Two curators on one record: the second sees the first's decision before adding one."""
    first = _post(client, {**_body(client), "seen_decision": None})
    assert first.status_code == 201
    first_id = first.json()["decision"]["decision_id"]
    late = _post(client, {**_body(client, outcome="rejected"), "seen_decision": None})
    assert late.status_code == 409
    detail = late.json()["detail"]
    assert "Josiah Carberry" in detail and "accepted" in detail
    seen = _post(client, {**_body(client, outcome="rejected"), "seen_decision": first_id})
    assert seen.status_code == 201
    history = client.get(f"/api/records/{PACKET}/decisions").json()
    assert [d["outcome"] for d in history["decisions"]] == ["accepted", "rejected"]
    assert history["status"] == "rejected"


def test_recent_decisions_give_live_statuses_for_the_list(client: TestClient) -> None:
    assert client.get("/api/decisions/recent").json() == {"statuses": {}}
    _post(client, _body(client))
    recent = client.get("/api/decisions/recent").json()["statuses"]
    assert recent[PACKET]["status"] == "accepted"
    assert recent[PACKET]["curator"] == "Josiah Carberry"
    decided_at = recent[PACKET]["decided_at"]
    # Only sign-offs newer than what the site was built with.
    assert client.get("/api/decisions/recent", params={"since": decided_at}).json() == {
        "statuses": {}
    }
