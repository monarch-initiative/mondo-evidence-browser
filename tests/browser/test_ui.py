"""Browser smoke tests: the static app renders from the spec, sorts lazily, signs off."""

import json
import re
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from mondo_evidence_browser.viewspec import build_view_spec
from tests.browser.conftest import Site
from tests.orcid import CLIENT_ID, id_token

pytestmark = pytest.mark.browser

PACKET = "pkt_9c18e79339f2"
SPEC = build_view_spec()


def _keys(site: Site, sql: str, *params: object) -> list[str]:
    conn = sqlite3.connect(site.root / "evidence.db")
    return [row[0] for row in conn.execute(sql, params)]


def _row_keys(page: Page) -> list[str]:
    keys: list[str] = page.locator("table.records tbody tr").evaluate_all(
        "rows => rows.map(r => r.dataset.key)"
    )
    return keys


def _open_list(page: Page, site: Site, query: str = "") -> None:
    page.goto(f"{site.url}/{query}")
    expect(page.locator("table.records tbody tr").first).to_be_visible()
    expect(page.locator("body")).to_have_attribute("data-loading", "false")


def test_tabs_and_columns_come_from_the_schema(page: Page, site: Site) -> None:
    _open_list(page, site)
    counts = site.meta["tab_counts"]
    tabs = page.locator("[role=tab]")
    expect(tabs).to_have_count(2)
    expect(tabs.nth(0)).to_contain_text("In Mondo")
    expect(tabs.nth(0)).to_contain_text(str(counts["IN_MONDO"]))
    expect(tabs.nth(1)).to_contain_text("Candidates")
    expect(tabs.nth(0)).to_have_attribute("aria-selected", "true")
    headers = page.locator("table.records thead th[data-column]").all_text_contents()
    assert [h.replace("▾", "").replace("▴", "").strip() for h in headers] == [
        c["title"] for c in SPEC["columns"]
    ]


def test_default_order_is_by_score_descending(page: Page, site: Site) -> None:
    _open_list(page, site)
    expected = _keys(
        site,
        "SELECT key FROM records WHERE tab = 'IN_MONDO' "
        "ORDER BY inclusion_score__value DESC, rowid DESC LIMIT 50",
    )
    assert _row_keys(page) == expected
    score_header = page.locator("th[data-column=inclusion_score__value]")
    expect(score_header).to_have_attribute("aria-sort", "descending")


def test_switching_tabs(page: Page, site: Site) -> None:
    _open_list(page, site)
    page.get_by_role("tab", name=re.compile("Candidates")).click()
    expect(page).to_have_url(re.compile(r"tab=CANDIDATE"))
    expect(page.locator("body")).to_have_attribute("data-loading", "false")
    candidates = set(_keys(site, "SELECT key FROM records WHERE tab = 'CANDIDATE'"))
    shown = _row_keys(page)
    assert shown and set(shown) <= candidates


def test_sorting_by_a_column_header(page: Page, site: Site) -> None:
    _open_list(page, site)
    page.locator("th[data-column=statement__subject_label] button").click()
    header = page.locator("th[data-column=statement__subject_label]")
    expect(header).to_have_attribute("aria-sort", "ascending")
    expect(page).to_have_url(re.compile(r"sort=statement__subject_label"))
    expected = _keys(
        site,
        "SELECT key FROM records WHERE tab = 'IN_MONDO' "
        "ORDER BY statement__subject_label ASC, rowid ASC LIMIT 50",
    )
    expect(page.locator("table.records tbody tr").first).to_have_attribute("data-key", expected[0])
    assert _row_keys(page) == expected
    page.locator("th[data-column=statement__subject_label] button").click()
    expect(header).to_have_attribute("aria-sort", "descending")


def test_unsortable_columns_have_no_sort_control(page: Page, site: Site) -> None:
    _open_list(page, site)
    expect(page.locator("th[data-column=sources] button")).to_have_count(0)


def test_search(page: Page, site: Site) -> None:
    _open_list(page, site, "?tab=CANDIDATE")
    page.get_by_label("Search records").fill("arginase")
    page.get_by_role("button", name="Search", exact=True).click()
    expect(page).to_have_url(re.compile(r"q=arginase"))
    expect(page.locator("body")).to_have_attribute("data-loading", "false")
    labels = page.locator("td[data-column=statement__subject_label]").all_inner_texts()
    assert labels and all("arginase" in label.lower() for label in labels)


def test_filters_use_the_counts_in_meta(page: Page, site: Site) -> None:
    _open_list(page, site)
    select = page.locator("select[data-filter=review_status]")
    expect(select.locator("option", has_text="Unreviewed")).to_have_count(1)
    select.select_option("unreviewed")  # a filter applies as soon as it is picked
    expect(page).to_have_url(re.compile(r"review_status=unreviewed"))
    n = site.meta["filter_values"]["IN_MONDO"]["review_status"]["unreviewed"]
    expect(page.locator("#count")).to_contain_text(f"of {n} records")


def test_clicking_a_row_opens_the_record_at_its_stable_url(page: Page, site: Site) -> None:
    _open_list(page, site)
    page.locator(f"tr[data-key='{PACKET}'] td[data-column=claim_type]").click()
    expect(page).to_have_url(f"{site.url}/record.html?id={PACKET}")
    expect(page.locator("h1")).to_have_text(
        "arginase deficiency (MONDO:0008814) has exact synonym “Arginase deficiency”"
    )


def test_record_page_reads_as_an_account_of_the_evidence(page: Page, site: Site) -> None:
    """Statement as a sentence, a plain verdict, then one card per piece of evidence."""
    page.goto(f"{site.url}/record.html?id={PACKET}")
    heading = page.locator("h1")
    expect(heading.get_by_role("link", name="MONDO:0008814")).to_have_attribute(
        "href", "https://bioregistry.io/MONDO:0008814"
    )
    expect(page.locator(".lede")).to_have_text(
        "Supported by 2 independent sources (Disease Ontology and Orphanet (ORDO)). "
        "Score 0.99, strong support. Rule not yet calibrated."
    )
    facts = page.locator("dl.summary")
    expect(facts.locator("[data-column=claim_type]")).to_have_text("Synonym")
    expect(facts.locator("[data-column=sources]")).to_contain_text("Disease Ontology")
    cards = page.locator("#record .evidence .mapping")
    expect(cards).to_have_count(2)
    doid = cards.filter(has_text="Disease Ontology")
    expect(doid).to_contain_text("arginase deficiency")  # the Mondo side
    expect(doid.get_by_role("link", name="DOID:9278")).to_have_attribute(
        "href", "https://bioregistry.io/DOID:9278"
    )
    expect(doid).to_contain_text("hyperargininemia")  # the source's own label
    expect(doid).to_contain_text("mondo.sssom.tsv")  # where the mapping comes from


def test_technical_fields_stay_out_of_the_main_view(page: Page, site: Site) -> None:
    page.goto(f"{site.url}/record.html?id={PACKET}")
    expect(page.locator("#record .evidence .mapping")).to_have_count(2)
    record = site.index.get(PACKET)
    assert record is not None
    # What shows without opening anything: the record minus its folded <details>.
    text = page.evaluate(
        """() => { const copy = document.querySelector('#record').cloneNode(true);
        copy.querySelectorAll('details').forEach(d => d.remove());
        document.body.append(copy); const t = copy.innerText; copy.remove(); return t; }"""
    )
    for hidden in (record["record_version"], "SieveEvidenceLine", "ConcordanceItem", "el_", "ev_"):
        assert hidden not in text, hidden
    assert "Status in source" not in text
    # Still there for anyone who needs it, collapsed.
    expect(page.locator("#record details", has_text="Record details")).to_contain_text(
        record["record_version"]
    )


def test_the_score_maths_is_a_collapsed_table(page: Page, site: Site) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{site.url}/record.html?id={PACKET}")
    maths = page.locator("#record details", has_text="How the score was computed")
    expect(maths).not_to_have_attribute("open", "")
    maths.locator("summary").click()
    components = maths.locator("table.detail-table[data-class=ScoreComponent]")
    expect(components).to_be_visible()
    headers = components.locator("thead th").all_text_contents()
    assert {"Direction (d)", "Strength (s)", "Source prior (q)", "Confidence (c)"} <= set(headers)
    expect(components.locator("tbody tr")).to_have_count(2)
    expect(maths.locator(".detail-wrap.stacked")).to_have_count(0)  # a real table


def test_unknown_record(page: Page, site: Site) -> None:
    page.goto(f"{site.url}/record.html?id=pkt_doesnotexist")
    expect(page.locator("h1")).to_have_text("No evidence record")


def test_read_only_deployment_says_sign_off_is_unavailable(page: Page, static_site: Site) -> None:
    page.goto(f"{static_site.url}/record.html?id={PACKET}")
    expect(page.locator("#signoff")).to_contain_text("Sign-off is not available")
    expect(page.get_by_role("button", name="Log in with ORCID")).to_have_count(0)


def test_login_goes_to_orcid_with_an_openid_request(page: Page, site: Site) -> None:
    captured: list[str] = []

    def orcid(route: Route) -> None:
        captured.append(route.request.url)
        route.fulfill(status=200, body="ORCID")

    page.route("https://sandbox.orcid.org/**", orcid)
    page.goto(f"{site.url}/record.html?id={PACKET}")
    page.get_by_role("button", name="Log in with ORCID to sign off").click()
    page.wait_for_url("https://sandbox.orcid.org/**")
    query = parse_qs(urlparse(captured[0]).query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["response_type"] == ["token"]
    assert query["scope"] == ["openid"]
    assert query["redirect_uri"] == [f"{site.url}/auth.html"]
    assert query["state"][0] and query["nonce"][0]


def test_login_callback_keeps_the_token_and_returns(page: Page, site: Site) -> None:
    page.goto(f"{site.url}/index.html")
    pending = {"state": "s1", "nonce": "n1", "back": f"/record.html?id={PACKET}"}
    page.evaluate("p => sessionStorage.setItem('meb.login', JSON.stringify(p))", pending)
    page.goto(f"{site.url}/auth.html#id_token={id_token(nonce='n1')}&state=s1")
    expect(page).to_have_url(f"{site.url}/record.html?id={PACKET}")
    expect(page.locator("#who")).to_contain_text("Josiah Carberry")


def test_login_callback_rejects_a_wrong_state(page: Page, site: Site) -> None:
    page.goto(f"{site.url}/index.html")
    pending = {"state": "s1", "nonce": "n1", "back": "/"}
    page.evaluate("p => sessionStorage.setItem('meb.login', JSON.stringify(p))", pending)
    page.goto(f"{site.url}/auth.html#id_token={id_token(nonce='n1')}&state=forged")
    expect(page.locator("#status")).to_contain_text("could not be verified")


def test_sign_off_form_comes_from_the_schema_and_records_a_decision(page: Page, site: Site) -> None:
    token = id_token()
    page.add_init_script(f"sessionStorage.setItem('meb.id_token', {json.dumps(token)})")
    page.goto(f"{site.url}/record.html?id={PACKET}")
    form = page.locator("form.signoff-form")
    expect(form).to_be_visible()
    options = SPEC["form"]["fields"][0]["options"]
    for option in options:
        expect(form.get_by_label(option["title"], exact=True)).to_have_count(1)
    expect(form.locator("[data-field=rationale] textarea")).to_have_count(1)
    form.get_by_label("Accept", exact=True).check()
    form.locator("[data-field=rationale] textarea").fill("Two independent sources list it.")
    form.get_by_role("button", name="Record sign-off").click()
    expect(page.locator(".form-message")).to_have_text("Sign-off recorded.")
    history = page.locator("#history")
    expect(history).to_contain_text("Josiah Carberry")
    expect(history).to_contain_text("Two independent sources list it.")
    record = site.index.get(PACKET)
    assert record is not None and record["review_status"] == "accepted"


def test_reads_stay_lazy_on_a_large_index(page: Page, big_site: Site) -> None:
    # The replicated values are not in Mondo, so all 6,900 records are candidates.
    _open_list(page, big_site, "?tab=CANDIDATE")
    page.locator("th[data-column=statement__subject_label] button").click()
    expect(page.locator("th[data-column=statement__subject_label]")).to_have_attribute(
        "aria-sort", "ascending"
    )
    page.get_by_role("button", name="Next →").click()
    expect(page).to_have_url(re.compile(r"page=2"))
    expect(page.locator("body")).to_have_attribute("data-loading", "false")
    read = page.evaluate("async () => await window.__meb.bytesRead()")
    size = (big_site.root / "evidence.db").stat().st_size
    assert big_site.meta["record_count"] == 6900
    assert read < size * 0.2, f"read {read} of {size} bytes"


def test_rows_expand_to_preview_the_record(page: Page, site: Site) -> None:
    _open_list(page, site)
    row = page.locator(f"tr[data-key='{PACKET}']")
    toggle = row.locator("button.expand")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    expect(page).to_have_url(f"{site.url}/")  # expanding does not navigate
    expect(toggle).to_have_attribute("aria-expanded", "true")
    preview = page.locator(f"tr.preview[data-key='{PACKET}']")
    expect(preview).to_be_visible()
    for item in SPEC["preview"]:
        expect(preview.locator(f"[data-preview={item['name']}]")).to_contain_text(item["title"])
    components = preview.locator("table.detail-table[data-class=ScoreComponent] tbody tr")
    expect(components).to_have_count(2)
    expect(preview.locator("article.mapping")).to_have_count(2)  # same cards as the record page
    expect(preview.get_by_role("link", name="Open the full record")).to_have_attribute(
        "href", f"{site.url}/record.html?id={PACKET}"
    )
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(preview).to_have_count(0)


def test_row_click_still_opens_the_record(page: Page, site: Site) -> None:
    _open_list(page, site)
    page.locator(f"tr[data-key='{PACKET}'] td[data-column=sources]").click()
    expect(page).to_have_url(f"{site.url}/record.html?id={PACKET}")


def test_list_page_explains_the_score(page: Page, site: Site) -> None:
    _open_list(page, site)
    explainer = page.locator("details.explainer")
    expect(explainer.locator("summary")).to_have_text("How the score is calculated")
    explainer.locator("summary").click()
    expect(explainer).to_contain_text("tanh")
    expect(explainer).to_contain_text(site.meta["rule_version"])
    for slot in SPEC["classes"]["ScoreComponent"]["slots"]:
        expect(explainer.locator(f"dt[data-slot={slot['name']}]")).to_have_text(slot["title"])
        expect(explainer.locator(f"dd[data-slot={slot['name']}]")).to_have_text(slot["description"])


def test_expanded_row_explains_components_on_hover(page: Page, site: Site) -> None:
    _open_list(page, site)
    page.locator(f"tr[data-key='{PACKET}'] button.expand").click()
    preview = page.locator(f"tr.preview[data-key='{PACKET}']")
    expect(preview.locator("[data-preview]").first).to_contain_text("Score components")
    expect(preview.locator("[data-preview*=calculation]")).to_have_count(0)
    slots = {s["name"]: s for s in SPEC["classes"]["ScoreComponent"]["slots"]}
    header = preview.locator("table.detail-table th", has_text="Source prior (q)")
    expect(header).to_have_attribute("title", slots["source_prior"]["description"])
    expect(header).to_have_class(re.compile(r"has-help"))
    assert header.evaluate("th => getComputedStyle(th).cursor") != "help"


def test_tabs_without_records_are_hidden(page: Page, site: Site) -> None:
    assert "NOT_CHECKED" not in site.meta["tab_counts"]
    _open_list(page, site)
    expect(page.locator("[role=tab][data-tab=NOT_CHECKED]")).to_have_count(0)
    page.goto(f"{site.url}/?tab=NOT_CHECKED")
    expect(page.locator("[role=tab][data-tab=NOT_CHECKED]")).to_have_count(1)
    expect(page.locator(".empty")).to_have_text("No records match.")


def test_data_served_from_another_host(page: Page, external_data_site: Site) -> None:
    site = external_data_site
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    _open_list(page, site)
    page.goto(f"{site.url}/record.html?id={PACKET}")
    expect(page.locator("h1")).to_have_text(
        "arginase deficiency (MONDO:0008814) has exact synonym “Arginase deficiency”"
    )
    data_requests = [url for url in requests if "/evidence." in url]
    assert any(url.endswith("evidence.sqlite.png") for url in data_requests), data_requests
    assert data_requests
    assert all(not url.startswith(site.url) for url in data_requests), data_requests


def _expected(site: Site, order: str, offset: int, where: str = "") -> list[str]:
    return _keys(
        site,
        f"SELECT key FROM records WHERE tab = 'CANDIDATE' {where} ORDER BY {order} "
        f"LIMIT 50 OFFSET {offset}",
    )


def test_deep_pages_load_directly_and_cheaply(page: Page, big_site: Site) -> None:
    _open_list(page, big_site, "?tab=CANDIDATE&page=100")
    order = "inclusion_score__value DESC, rowid DESC"
    assert _row_keys(page) == _expected(big_site, order, 99 * 50)
    expect(page.locator("#count")).to_contain_text("4,951–5,000 of 6,900")
    read = page.evaluate("async () => await window.__meb.bytesRead()")
    size = (big_site.root / "evidence.db").stat().st_size
    assert read < size * 0.1, f"page 100 read {read} of {size} bytes"


def test_next_and_previous_continue_from_the_page_shown(page: Page, big_site: Site) -> None:
    where = "AND review_status = 'unreviewed'"
    order = "statement__subject_label ASC, rowid ASC"
    query = "?tab=CANDIDATE&review_status=unreviewed&sort=statement__subject_label&dir=asc"
    _open_list(page, big_site, query)
    next_page = page.get_by_role("button", name="Next →")
    previous = page.get_by_role("button", name="← Previous")
    for number in (2, 3):
        next_page.click()
        expect(page).to_have_url(re.compile(rf"page={number}"))
        expect(page.locator("body")).to_have_attribute("data-loading", "false")
        assert _row_keys(page) == _expected(big_site, order, (number - 1) * 50, where)
    previous.click()
    expect(page).to_have_url(re.compile(r"page=2"))
    expect(page.locator("body")).to_have_attribute("data-loading", "false")
    assert _row_keys(page) == _expected(big_site, order, 50, where)


def _fts_keys(site: Site, match: str, order: str = "rowid") -> list[str]:
    return _keys(
        site,
        "SELECT key FROM records WHERE rowid IN "
        f"(SELECT rowid FROM search WHERE search MATCH ?) AND tab = 'CANDIDATE' ORDER BY {order}",
        match,
    )


def test_search_with_a_filter_is_counted_and_paged_from_the_search_index(
    page: Page, big_site: Site
) -> None:
    _open_list(page, big_site, "?tab=CANDIDATE&q=deficiency&review_status=unreviewed")
    expected = _fts_keys(big_site, '"deficiency"*')
    expect(page.locator("#count")).to_contain_text(f"of {len(expected):,} records")
    assert _row_keys(page) == expected[:50]
    page.get_by_role("button", name="Next →").click()
    expect(page).to_have_url(re.compile(r"page=2"))
    expect(page.locator("body")).to_have_attribute("data-loading", "false")
    assert _row_keys(page) == expected[50:100]


def test_two_filters_are_counted_without_reading_rows(page: Page, big_site: Site) -> None:
    _open_list(page, big_site, "?tab=CANDIDATE&review_status=unreviewed&claim_type=C003")
    n = len(
        _keys(
            big_site,
            "SELECT key FROM records WHERE tab = 'CANDIDATE' AND "
            "review_status = 'unreviewed' AND claim_type = 'C003'",
        )
    )
    expect(page.locator("#count")).to_contain_text(f"of {n:,} records")
    read = page.evaluate("async () => await window.__meb.bytesRead()")
    assert read < (big_site.root / "evidence.db").stat().st_size * 0.2


def test_a_broad_search_stays_in_the_default_order_and_says_so(page: Page, big_site: Site) -> None:
    _open_list(page, big_site, "?tab=CANDIDATE&q=deficiency&sort=statement__subject_label&dir=asc")
    expect(page.locator("#notice")).to_contain_text("can't be sorted by another column")
    assert _row_keys(page) == _fts_keys(big_site, '"deficiency"*')[:50]
    expect(page.locator("th[data-column=inclusion_score__value]")).to_have_attribute(
        "aria-sort", "descending"
    )


def test_a_narrow_search_can_be_sorted_by_any_column(page: Page, big_site: Site) -> None:
    conn = sqlite3.connect(big_site.root / "evidence.db")
    word = next(
        w
        for w in ("myasthenic", "argininemia", "peripartum", "seminoma", "lyase", "werner")
        if 0
        < conn.execute(
            "SELECT count(*) FROM records WHERE tab = 'CANDIDATE' AND rowid IN "
            "(SELECT rowid FROM search WHERE search MATCH ?)",
            (f'"{w}"*',),
        ).fetchone()[0]
        <= 500
    )
    _open_list(page, big_site, f"?tab=CANDIDATE&q={word}&sort=statement__subject_label&dir=asc")
    expect(page.locator("#notice")).to_be_hidden()
    expected = _fts_keys(big_site, f'"{word}"*', "statement__subject_label ASC, rowid ASC")
    assert _row_keys(page) == expected[:50]


def _sign_via_api(site: Site, record_id: str, outcome: str) -> dict[str, object]:
    """Another curator's sign-off, made through the service while this page is open."""
    import httpx

    record = httpx.get(f"{site.url}/api/records/{record_id}").json()["record"]
    history = httpx.get(f"{site.url}/api/records/{record_id}/decisions").json()["decisions"]
    response = httpx.post(
        f"{site.url}/api/records/{record_id}/sign-off",
        json={
            "seen_version": record["record_version"],
            "seen_rule_version": record["inclusion_score"]["rule_version"],
            "seen_decision": history[-1]["decision_id"] if history else None,
            "values": {"outcome": outcome},
        },
        headers={"Authorization": f"Bearer {id_token()}"},
    )
    assert response.status_code == 201, response.text
    decision: dict[str, object] = response.json()["decision"]
    return decision


def _unreviewed_key(page: Page, site: Site, skip: set[str]) -> str:
    _open_list(page, site)
    for key in _row_keys(page):
        status = page.locator(f'tr[data-key="{key}"] td[data-column="review_status"]')
        if key not in skip and status.inner_text().strip().lower() == "unreviewed":
            return key
    raise AssertionError("no unreviewed record on the first page")


def test_the_list_shows_sign_offs_made_after_the_build(page: Page, site: Site) -> None:
    """Curators in parallel see each other's decisions without waiting for a rebuild."""
    key = _unreviewed_key(page, site, skip={PACKET})
    _sign_via_api(site, key, "rejected")
    _open_list(page, site)
    status = page.locator(f'tr[data-key="{key}"] td[data-column="review_status"]')
    expect(status).to_contain_text("Rejected")
    expect(status).to_contain_text("new")
    expect(status).to_have_attribute("data-live", "true")
    expect(status).to_have_attribute("title", re.compile("Josiah Carberry"))


def test_a_decision_made_while_the_record_is_open_is_shown_before_signing(
    page: Page, site: Site
) -> None:
    key = _unreviewed_key(page, site, skip={PACKET})
    page.add_init_script(f"sessionStorage.setItem('meb.id_token', {json.dumps(id_token())})")
    page.goto(f"{site.url}/record.html?id={key}")
    form = page.locator("form.signoff-form")
    expect(form).to_be_visible()
    expect(page.locator("#history")).to_contain_text("No sign-offs yet.")
    _sign_via_api(site, key, "deferred")  # someone else, meanwhile
    form.get_by_label("Accept", exact=True).check()
    form.get_by_role("button", name="Record sign-off").click()
    message = page.locator(".form-message")
    expect(message).to_contain_text("deferred this record")
    expect(message).to_have_class(re.compile("error"))
    expect(page.locator("#history li")).to_have_count(1)
    expect(page.locator('dd[data-column="review_status"]')).to_contain_text("Deferred")
    # Having seen it, the curator can add their own decision on top.
    form.get_by_role("button", name="Record sign-off").click()
    expect(message).to_have_text("Sign-off recorded.")
    expect(page.locator("#history li")).to_have_count(2)
    expect(page.locator('dd[data-column="review_status"]')).to_contain_text("Accepted")


def test_the_first_page_shows_without_reading_the_database(page: Page, site: Site) -> None:
    """The default view comes from first-page.json, so it is instant on any data host."""
    # What the database gives for the default view: the default tab by score rank.
    expected = _keys(
        site,
        "SELECT key FROM records WHERE tab = 'IN_MONDO' "
        "ORDER BY __rank__inclusion_score__value DESC LIMIT 50",
    )
    count = f"1–{len(expected)} of {site.meta['tab_counts']['IN_MONDO']:,} records"
    page.route(re.compile(r".*/data/evidence\.(db|sqlite\.png).*"), lambda route: route.abort())
    page.goto(f"{site.url}/")
    expect(page.locator("table.records tbody tr").first).to_be_visible()
    assert _row_keys(page) == expected
    expect(page.locator("#count")).to_have_text(count)


def test_other_views_still_come_from_the_database(page: Page, site: Site) -> None:
    _open_list(page, site)
    default = _row_keys(page)
    _open_list(page, site, "?q=syndrome")
    keys = _row_keys(page)
    assert keys and keys != default
    expected = _keys(
        site,
        "SELECT key FROM records WHERE tab = ? AND rowid IN "
        "(SELECT rowid FROM search WHERE search MATCH ?) ORDER BY rowid LIMIT 50",
        "IN_MONDO",  # the default tab
        '"syndrome"*',
    )
    assert keys == expected


def test_rows_do_not_wait_for_the_sign_off_service(page: Page, site: Site) -> None:
    """A sleeping service (a free host waking up) must not hold back the table."""
    import time

    held: list[Route] = []
    page.route("**/api/decisions/recent*", lambda route: held.append(route))
    started = time.monotonic()
    page.goto(f"{site.url}/")
    expect(page.locator("table.records tbody tr").first).to_be_visible(timeout=3000)
    assert time.monotonic() - started < 3
    assert held, "the list should still ask the service for recent sign-offs"
    for route in held:
        route.fulfill(json={"statuses": {}})


def test_active_filters_stand_out_and_clear_in_one_click(page: Page, site: Site) -> None:
    _open_list(page, site, "?review_status=unreviewed&q=deficiency")
    control = page.locator("label.control:has(select[data-filter=review_status])")
    expect(control).to_have_class(re.compile(r"\bactive\b"))
    page.get_by_role("button", name="Clear all").click()
    expect(page).not_to_have_url(re.compile(r"review_status|q="))
    expect(page.locator("body")).to_have_attribute("data-loading", "false")
    expect(page.get_by_label("Search records")).to_have_value("")
    expect(page.get_by_role("button", name="Clear all")).to_have_count(0)


def test_a_filter_with_no_matches_still_shows_what_was_chosen(page: Page, site: Site) -> None:
    """A link may filter on a value this tab doesn't have; say so rather than show "Any"."""
    page.goto(f"{site.url}/?review_status=controversial")
    expect(page.locator("#count")).to_contain_text("of 0 records")
    select = page.locator("select[data-filter=review_status]")
    expect(select).to_have_value("controversial")
    expect(select.locator("option:checked")).to_contain_text("(0)")
    expect(page.locator("#count")).to_contain_text("of 0 records")


def _logged_in(page: Page) -> None:
    page.add_init_script(f"sessionStorage.setItem('meb.id_token', {json.dumps(id_token())})")


def test_quick_sign_off_buttons_only_for_logged_in_curators(page: Page, site: Site) -> None:
    _open_list(page, site)
    expect(page.locator("table.records th.actions-col")).to_have_count(0)
    _logged_in(page)
    _open_list(page, site)
    row = page.locator(f"tr[data-key='{PACKET}']")
    expect(row.get_by_role("button", name="Accept")).to_be_visible()
    expect(row.get_by_role("button", name="Reject")).to_be_visible()
    expect(row.get_by_role("link", name="More sign-off options")).to_have_attribute(
        "href", f"{site.url}/record.html?id={PACKET}#signoff"
    )


def test_quick_sign_off_from_the_table(page: Page, site: Site) -> None:
    """Accept a record from the list: confirm, add a reason, and the row updates."""
    import httpx

    key = _unreviewed_key(page, site, skip={PACKET})
    _logged_in(page)
    _open_list(page, site)
    row = page.locator(f"tr[data-key='{key}']")
    row.get_by_role("button", name="Accept").click()
    confirm = page.locator(f"tr.quick[data-key='{key}']")
    expect(confirm).to_contain_text("Accept")
    expect(row.locator("td[data-column=review_status]")).to_contain_text("Unreviewed")  # not yet
    confirm.get_by_label("Rationale").fill("Listed in both sources.")
    confirm.get_by_role("button", name="Confirm").click()
    expect(confirm).to_have_count(0)
    status = row.locator("td[data-column=review_status]")
    expect(status).to_contain_text("Accepted")
    expect(status).to_have_attribute("data-live", "true")
    history = httpx.get(f"{site.url}/api/records/{key}/decisions").json()["decisions"]
    assert [(d["outcome"], d.get("rationale")) for d in history] == [
        ("accepted", "Listed in both sources.")
    ]


def test_quick_sign_off_can_be_cancelled(page: Page, site: Site) -> None:
    import httpx

    key = _unreviewed_key(page, site, skip={PACKET})
    _logged_in(page)
    _open_list(page, site)
    row = page.locator(f"tr[data-key='{key}']")
    row.get_by_role("button", name="Reject").click()
    page.locator(f"tr.quick[data-key='{key}']").get_by_role("button", name="Cancel").click()
    expect(page.locator(f"tr.quick[data-key='{key}']")).to_have_count(0)
    assert httpx.get(f"{site.url}/api/records/{key}/decisions").json()["decisions"] == []


def test_quick_sign_off_shows_a_decision_made_meanwhile(page: Page, site: Site) -> None:
    """Like the full form: another curator's decision is shown before yours is added."""
    key = _unreviewed_key(page, site, skip={PACKET})
    _logged_in(page)
    _open_list(page, site)
    row = page.locator(f"tr[data-key='{key}']")
    row.get_by_role("button", name="Reject").click()
    confirm = page.locator(f"tr.quick[data-key='{key}']")
    expect(confirm.get_by_role("button", name="Confirm")).to_be_enabled()
    _sign_via_api(site, key, "deferred")  # another curator, while the strip is open
    confirm.get_by_role("button", name="Confirm").click()
    expect(confirm).to_contain_text("deferred this record")
    expect(row.locator("td[data-column=review_status]")).to_contain_text("Deferred")
    confirm.get_by_role("button", name="Confirm").click()  # knowingly, on top
    expect(confirm).to_have_count(0)
    expect(row.locator("td[data-column=review_status]")).to_contain_text("Rejected")


def test_the_strip_says_what_was_decided_before(page: Page, site: Site) -> None:
    key = _unreviewed_key(page, site, skip={PACKET})
    _sign_via_api(site, key, "deferred")
    _logged_in(page)
    _open_list(page, site)
    page.locator(f"tr[data-key='{key}']").get_by_role("button", name="Accept").click()
    expect(page.locator(f"tr.quick[data-key='{key}']")).to_contain_text(
        "Latest: Defer by Josiah Carberry"
    )


# Draws a score with the site's own modules, outside any record: for the cases the
# fixture packets do not have yet (disputing lines, lines that do not count).
DRAW = """async ([module, call, args]) => {
  const base = document.querySelector('script[type=module]').src.replace(/[^/]+$/, '');
  const m = await import(base + module);
  const spec = await (await fetch('view-spec.json')).json();
  const node = call === 'renderValue'
    ? m.renderValue(spec, m.slotAtPath(spec, ['inclusion_score']), args, 0, null)
    : m.evidenceMap(spec, args, { term: 'MONDO:0000001', termLabel: 'test disease' });
  node.id = 'drawn';
  document.querySelector('#record').prepend(node);
}"""


def _component(line: str, source: str, weight: float, **extra: object) -> dict[str, object]:
    return {
        "line_id": line,
        "kind": "concordance",
        "source": source,
        "direction": 1 if weight >= 0 else -1,
        "weight": weight,
        "cluster": source,
        "counted": True,
        **extra,
    }


SYNTHETIC_SCORE = {
    "value": 0.46,
    "band": "moderate support",
    "claim_prior": 0.0,
    "scaling": 2.0,
    "log_odds": 1.0,
    "components": [
        _component("el_doid", "doid", 0.8),
        _component("el_ordo", "ordo", -0.3),
        _component(
            "el_omim",
            "omim",
            0.5,
            cluster="doid",
            counted=False,
            reason="Same independence cluster as Disease Ontology",
        ),
    ],
}


def test_the_verdict_shows_the_score_on_a_gauge(page: Page, site: Site) -> None:
    """Where the score sits from -1 (disputed) to +1 (supported), on the rule's bands."""
    record = site.index.get(PACKET)
    assert record is not None
    score = record["inclusion_score"]
    page.goto(f"{site.url}/record.html?id={PACKET}")
    gauge = page.locator(".verdict figure.gauge")
    expect(gauge).to_have_attribute("role", "img")
    label = gauge.get_attribute("aria-label") or ""
    assert f"{score['value']:.2f}" in label and score["band"] in label, label
    expect(gauge.locator(".gauge-band")).to_have_count(5)
    expect(gauge.locator(".gauge-band.current")).to_have_attribute("data-band", score["band"])
    left = gauge.locator(".gauge-marker").evaluate("m => parseFloat(m.style.left)")
    assert abs(left - (score["value"] + 1) * 50) < 0.5
    expect(page.locator(".verdict .lede")).to_contain_text("Supported by 2 independent sources")
    # Shown once: not again as a fact tile.
    expect(page.locator("dl.summary [data-column=inclusion_score__value]")).to_have_count(0)


def test_the_score_maths_draws_each_lines_contribution(page: Page, site: Site) -> None:
    record = site.index.get(PACKET)
    assert record is not None
    score = record["inclusion_score"]
    page.goto(f"{site.url}/record.html?id={PACKET}")
    maths = page.locator("#record details", has_text="How the score was computed")
    maths.locator("summary").click()
    chart = maths.locator("figure.contributions")
    expect(chart).to_be_visible()
    # The starting point, one bar per line, and the total.
    expect(chart.locator(".contribution")).to_have_count(len(score["components"]) + 2)
    expect(chart.locator(".contribution.prior")).to_contain_text(f"{score['claim_prior']:+.2f}")
    k = score["scaling"]
    widths = {}
    for component in score["components"]:
        row = chart.locator(f".contribution[data-line='{component['line_id']}']")
        expect(row).to_have_class(re.compile(r"\bpos\b"))
        expect(row).to_contain_text(
            {"doid": "Disease Ontology", "ordo": "Orphanet (ORDO)"}[component["source"]]
        )
        expect(row).to_contain_text(f"{k * component['weight']:+.2f}")
        widths[component["line_id"]] = row.locator(".bar").evaluate(
            "b => b.getBoundingClientRect().width"
        )
    total = chart.locator(".contribution.total")
    expect(total).to_contain_text(f"{score['log_odds']:+.2f}")
    total_width = total.locator(".bar").evaluate("b => b.getBoundingClientRect().width")
    for component in score["components"]:  # bars to scale
        expected = k * component["weight"] / score["log_odds"]
        assert abs(widths[component["line_id"]] / total_width - expected) < 0.02
    expect(chart.locator("figcaption")).to_contain_text(f"{score['value']:.2f}")
    # The exact numbers are still there, as a table.
    expect(maths.locator("table.detail-table[data-class=ScoreComponent]")).to_be_visible()


def test_lines_that_dispute_or_do_not_count_are_drawn_apart(page: Page, site: Site) -> None:
    page.goto(f"{site.url}/record.html?id={PACKET}")
    expect(page.locator("h1")).to_be_visible()
    page.evaluate(DRAW, ["render.js", "renderValue", SYNTHETIC_SCORE])
    chart = page.locator("#drawn figure.contributions")
    expect(chart.locator(".contribution[data-line=el_doid]")).to_have_class(re.compile(r"\bpos\b"))
    disputing = chart.locator(".contribution[data-line=el_ordo]")
    expect(disputing).to_have_class(re.compile(r"\bneg\b"))
    expect(disputing).to_contain_text("-0.60")
    left = chart.locator(".contribution[data-line=el_omim]")
    expect(left).to_have_class(re.compile(r"\bnot-counted\b"))
    expect(left).to_contain_text("Same independence cluster as Disease Ontology")
    expect(chart.locator(".contribution.total")).to_contain_text("+1.00")
    # A disputing bar runs left of zero, a supporting one right of it.
    zero = chart.locator(".contribution[data-line=el_doid] .bar").evaluate(
        "b => b.getBoundingClientRect().left"
    )
    right = disputing.locator(".bar").evaluate("b => b.getBoundingClientRect().right")
    assert abs(right - zero) < 1.5


def test_the_evidence_starts_with_a_map_of_its_sources(page: Page, site: Site) -> None:
    record = site.index.get(PACKET)
    assert record is not None
    page.goto(f"{site.url}/record.html?id={PACKET}")
    evidence = page.locator("#record .evidence")
    graph = evidence.locator("figure.evidence-map")
    expect(graph).to_be_visible()
    assert evidence.evaluate(
        "s => s.querySelector('h2').nextElementSibling.matches('figure.evidence-map')"
    )
    hub = graph.locator(".map-hub")
    expect(hub).to_contain_text("arginase deficiency")
    expect(hub).to_contain_text("MONDO:0008814")
    expect(graph.locator(".map-branch")).to_have_count(2)
    expect(graph.locator(".map-branch.dir-pos")).to_have_count(2)
    expect(graph.locator("figcaption")).to_have_text(
        "2 independent sources support this statement; none dispute it."
    )
    # Each source links to its evidence below.
    for component in record["inclusion_score"]["components"]:
        link = graph.locator(f"a[href='#line-{component['line_id']}']")
        expect(link).to_have_count(1)
        link.click()
        expect(page.locator(f"#line-{component['line_id']}")).to_be_in_viewport()


def test_the_map_groups_dependent_sources_and_shows_disputes(page: Page, site: Site) -> None:
    page.goto(f"{site.url}/record.html?id={PACKET}")
    expect(page.locator("h1")).to_be_visible()
    page.evaluate(DRAW, ["charts.js", "evidenceMap", SYNTHETIC_SCORE["components"]])
    graph = page.locator("#drawn")
    expect(graph.locator(".map-hub")).to_contain_text("test disease")
    cluster = graph.locator(".map-cluster")
    expect(cluster).to_have_count(1)  # Disease Ontology and OMIM count once
    expect(cluster.locator(".map-node")).to_have_count(2)
    expect(cluster).to_contain_text("count once")
    expect(graph.locator(".map-node.not-counted")).to_contain_text("OMIM")
    expect(graph.locator(".map-branch.dir-neg")).to_contain_text("Orphanet (ORDO)")
    expect(graph.locator(".map-branch.dir-neg")).to_contain_text("disputes")
    expect(graph.locator("figcaption")).to_have_text(
        "1 independent source supports this statement; 1 disputes it."
    )
