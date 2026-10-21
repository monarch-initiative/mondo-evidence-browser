"""A schema change means regenerating, never editing UI code.

These tests swap in a modified copy of the schema, rebuild, and check that the index,
the API and the rendered app all follow the new schema with no code changes.
"""

import re
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from playwright.sync_api import Page, expect

from mondo_evidence_browser.service import ServiceSettings, create_service
from mondo_evidence_browser.viewspec import SCHEMA_PATH, SpecError, build_view_spec
from tests.browser.conftest import CONFIG, Site, _serve
from tests.orcid import CLIENT_ID, ORCID_BASE, id_token, jwks_endpoint

PACKET = "pkt_9c18e79339f2"


def _swapped_schema(root: Path) -> Path:
    """The browser schema with display hints changed the way schema work changes them."""
    target = root / "schema"
    shutil.copytree(SCHEMA_PATH.parent, target)
    path = target / "browser.yaml"
    schema: dict[str, Any] = yaml.safe_load(path.read_text())
    statement = schema["classes"]["BrowserStatement"]["slot_usage"]
    # A new column from a slot of the upstream evidence model, sortable and searchable.
    statement["statement_text"] = {
        "title": "Statement",
        "annotations": {"browser_column": 8, "browser_sortable": True, "browser_search": True},
    }
    # Preview changes: a new first item, and evidence lines no longer previewed.
    statement["statement_text"]["annotations"]["browser_preview"] = -1
    record_usage = schema["classes"]["BrowserRecord"]["slot_usage"]
    del record_usage["has_evidence_lines"]["annotations"]["browser_preview"]
    # A retitled column.
    statement["subject_label"]["title"] = "Disease"
    # A removed column.
    del schema["classes"]["BrowserRecord"]["attributes"]["sources"]["annotations"]["browser_column"]
    # Renamed tabs.
    tabs = schema["enums"]["MondoMembership"]["permissible_values"]
    tabs["IN_MONDO"]["title"] = "Already in Mondo"
    # A new sign-off form field.
    schema["classes"]["SignOff"]["attributes"]["certainty"] = {
        "title": "Certainty",
        "range": "Certainty",
        "annotations": {"browser_widget": "radio"},
    }
    schema["enums"]["Certainty"] = {
        "permissible_values": {"high": {"title": "High"}, "low": {"title": "Low"}}
    }
    schema["version"] = "0.2.0-swapped"
    path.write_text(yaml.safe_dump(schema, sort_keys=False))
    return path


@pytest.fixture(scope="module")
def swapped(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Site, Path]]:
    root = tmp_path_factory.mktemp("swapped")
    schema = _swapped_schema(root)
    for site in _serve(root, schema_path=schema):
        yield site, schema


def test_index_follows_the_swapped_schema(swapped: tuple[Site, Path]) -> None:
    site, _ = swapped
    conn = sqlite3.connect(site.root / "evidence.db")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(records)")}
    assert "statement__statement_text" in columns
    assert "sources" not in columns
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(records)")}
    assert "ix___rank__statement__statement_text" in indexes
    assert site.meta["schema_version"] == "0.2.0-swapped"


def test_service_accepts_the_new_form_field(swapped: tuple[Site, Path]) -> None:
    site, schema = swapped
    settings = ServiceSettings(
        index_path=site.root / "evidence.db",
        decisions_path=site.root / "api-decisions.jsonl",
        config_dir=CONFIG,
        schema_path=schema,
        orcid_client_id=CLIENT_ID,
        orcid_base_url=ORCID_BASE,
    )
    client = TestClient(create_service(settings, orcid_transport=jwks_endpoint()))
    record = client.get(f"/api/records/{PACKET}").json()["record"]
    body = {
        "seen_version": record["record_version"],
        "seen_rule_version": record["inclusion_score"]["rule_version"],
        "values": {"outcome": "deferred", "certainty": "low"},
    }
    headers = {"Authorization": f"Bearer {id_token()}"}
    response = client.post(f"/api/records/{PACKET}/sign-off", json=body, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["decision"]["certainty"] == "low"
    body["values"]["certainty"] = "certain"
    record = client.get(f"/api/records/{PACKET}").json()["record"]
    body["seen_version"] = record["record_version"]
    response = client.post(f"/api/records/{PACKET}/sign-off", json=body, headers=headers)
    assert response.status_code == 422


@pytest.mark.browser
def test_app_renders_the_swapped_schema(page: Page, swapped: tuple[Site, Path]) -> None:
    site, schema = swapped
    spec = build_view_spec(schema)
    page.goto(f"{site.url}/")
    expect(page.locator("table.records tbody tr").first).to_be_visible()
    headers = [
        h.replace("▾", "").replace("▴", "").strip()
        for h in page.locator("table.records thead th[data-column]").all_text_contents()
    ]
    assert headers == [c["title"] for c in spec["columns"]]
    assert "Statement" in headers and "Disease" in headers and "Sources" not in headers
    expect(page.get_by_role("tab", name=re.compile("Already in Mondo"))).to_be_visible()
    page.locator("th[data-column=statement__statement_text] button").click()
    expect(page.locator("th[data-column=statement__statement_text]")).to_have_attribute(
        "aria-sort", "ascending"
    )


@pytest.mark.browser
def test_record_form_shows_the_new_field(page: Page, swapped: tuple[Site, Path]) -> None:
    site, _ = swapped
    page.add_init_script(f"sessionStorage.setItem('meb.id_token', '{id_token()}')")
    page.goto(f"{site.url}/record.html?id={PACKET}")
    field = page.locator("form.signoff-form [data-field=certainty]")
    expect(field).to_contain_text("Certainty")
    expect(field.get_by_label("High", exact=True)).to_have_count(1)


def test_an_upstream_rename_fails_the_build_loudly(tmp_path: Path) -> None:
    """If the evidence model renames a slot the browser annotates, say so."""
    target = tmp_path / "schema"
    shutil.copytree(SCHEMA_PATH.parent, target)
    sieve = target / "vendor" / "sieve.yaml"
    sieve.write_text(sieve.read_text().replace("      subject_label:", "      subject_name:"))
    with pytest.raises(SpecError, match="subject_label"):
        build_view_spec(target / "browser.yaml")


@pytest.mark.browser
def test_expanded_rows_follow_the_swapped_preview(page: Page, swapped: tuple[Site, Path]) -> None:
    site, schema = swapped
    spec = build_view_spec(schema)
    assert [p["name"] for p in spec["preview"]] == [
        "statement__statement_text",
        "inclusion_score__components",
    ]
    page.goto(f"{site.url}/")
    page.locator(f"tr[data-key='{PACKET}'] button.expand").click()
    preview = page.locator(f"tr.preview[data-key='{PACKET}']")
    items = preview.locator("[data-preview]")
    expect(items).to_have_count(2)
    expect(items.first).to_contain_text("Statement")
    expect(items.first).to_contain_text("has exact synonym")
    expect(preview.locator("[data-preview=has_evidence_lines]")).to_have_count(0)
