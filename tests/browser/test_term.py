"""The term page: every evidence record about one Mondo term, at a stable URL."""

import sqlite3
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from mondo_evidence_browser.viewspec import build_view_spec
from tests.browser.conftest import Site
from tests.browser.test_ui import PACKET, _sign_via_api

pytestmark = pytest.mark.browser

SPEC = build_view_spec()
TERM_COLUMNS = {
    c["name"]
    for c in SPEC["columns"]
    if c["path"] in (SPEC["roles"]["term"], SPEC["roles"]["term_label"])
}


def _query(site: Site, sql: str, *params: object) -> list[tuple[object, ...]]:
    return list(sqlite3.connect(site.root / "evidence.db").execute(sql, params))


def _busiest_term(site: Site) -> str:
    """The term with the most records in the fixtures, in more than one tab if any."""
    rows = _query(
        site,
        "SELECT statement__subject FROM records GROUP BY 1 "
        "ORDER BY count(DISTINCT tab) DESC, count(*) DESC, 1 LIMIT 1",
    )
    return str(rows[0][0])


def _open_term(page: Page, site: Site, term: str) -> None:
    page.goto(f"{site.url}/term.html?id={quote(term)}")
    expect(page.locator("body")).to_have_attribute("data-loading", "false")


def test_the_term_page_lists_the_terms_records_by_tab(page: Page, site: Site) -> None:
    term = _busiest_term(site)
    label = _query(
        site, "SELECT statement__subject_label FROM records WHERE statement__subject = ?", term
    )[0][0]
    _open_term(page, site, term)
    heading = page.locator("main h1")
    expect(heading).to_contain_text(str(label))
    expect(heading).to_contain_text(term)
    expect(page).to_have_title(f"{label} ({term})")
    by_tab: dict[str, set[str]] = {}
    for key, tab in _query(site, "SELECT key, tab FROM records WHERE statement__subject = ?", term):
        by_tab.setdefault(str(tab), set()).add(str(key))
    for value in SPEC["tabs"]["values"]:
        tab = value["value"]
        if not site.meta["tab_counts"].get(tab):
            expect(page.locator(f'section.term-tab[data-tab="{tab}"]')).to_have_count(0)
            continue
        section = page.locator(f'section.term-tab[data-tab="{tab}"]')
        expect(section.locator("h2")).to_contain_text(value["title"])
        keys = section.locator("tr[data-key]").evaluate_all("rows => rows.map(r => r.dataset.key)")
        assert set(keys) == by_tab.get(tab, set()), tab
        if not by_tab.get(tab):
            expect(section).to_contain_text("None")
    first = next(iter(next(v for v in by_tab.values() if v)))
    link = page.locator(f'tr[data-key="{first}"] a.record-link')
    expect(link).to_have_attribute("href", f"record.html?id={first}")


def test_the_term_page_leaves_out_the_terms_own_columns(page: Page, site: Site) -> None:
    _open_term(page, site, _busiest_term(site))
    shown = set(
        page.locator("th[data-column]").evaluate_all(
            "headers => headers.map(h => h.dataset.column)"
        )
    )
    assert shown and not shown & TERM_COLUMNS
    assert shown == {c["name"] for c in SPEC["columns"]} - TERM_COLUMNS


def test_the_term_page_links_out_to_the_term(page: Page, site: Site) -> None:
    term = _busiest_term(site)
    _open_term(page, site, term)
    out = page.locator("main h1 a")
    expect(out).to_have_text(term)
    expect(out).to_have_attribute("href", f"https://bioregistry.io/{term}")


def test_a_term_without_records(page: Page, site: Site) -> None:
    _open_term(page, site, "MONDO:9999999")
    expect(page.locator("main")).to_contain_text("No evidence records for MONDO:9999999")


def test_record_pages_and_the_list_link_the_mondo_term_to_its_page(page: Page, site: Site) -> None:
    term = str(_query(site, "SELECT statement__subject FROM records WHERE key = ?", PACKET)[0][0])
    page.goto(f"{site.url}/record.html?id={PACKET}")
    link = page.locator("#record h1 a", has_text=term)
    expect(link).to_have_attribute("href", f"term.html?id={quote(term)}")
    page.goto(f"{site.url}/")
    expect(page.locator("body")).to_have_attribute("data-loading", "false")
    cell = page.locator(f'tr[data-key="{PACKET}"] td[data-column="statement__subject"] a')
    expect(cell).to_have_attribute("href", f"term.html?id={quote(term)}")
    cell.click()
    expect(page.locator("main h1")).to_contain_text(term)


def test_the_term_page_shows_sign_offs_made_after_the_build(page: Page, site: Site) -> None:
    term = _busiest_term(site)
    key = str(
        _query(
            site,
            "SELECT key FROM records WHERE statement__subject = ? AND review_status = 'unreviewed' "
            "AND key != ? LIMIT 1",
            term,
            PACKET,
        )[0][0]
    )
    _sign_via_api(site, key, "deferred")
    _open_term(page, site, term)
    status = page.locator(f'tr[data-key="{key}"] td[data-column="review_status"]')
    expect(status).to_contain_text("Deferred")
    expect(status).to_have_attribute("data-live", "true")


def test_the_term_page_reads_lazily_and_caps_long_lists(page: Page, big_site: Site) -> None:
    term = _busiest_term(big_site)
    count_sql = "SELECT count(*) FROM records WHERE statement__subject = ?"
    total = int(str(_query(big_site, count_sql, term)[0][0]))
    assert total > 200
    _open_term(page, big_site, term)
    expect(page.locator("tr[data-key]")).to_have_count(200)
    more = page.locator("a.term-more")
    expect(more).to_contain_text(f"{total:,}")
    expect(more).to_have_attribute("href", f"index.html?tab=CANDIDATE&q={quote(term)}")
    read = page.evaluate("async () => await window.__meb.bytesRead()")
    size = (big_site.root / "evidence.db").stat().st_size
    assert read < size * 0.2, f"read {read} of {size} bytes"
