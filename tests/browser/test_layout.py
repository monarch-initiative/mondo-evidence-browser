"""Every table column is visible at once, at any width, without horizontal scrolling."""

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from mondo_evidence_browser.viewspec import build_view_spec
from tests.browser.conftest import Site

pytestmark = pytest.mark.browser

TITLES = [c["title"] for c in build_view_spec()["columns"]]

MEASURE = """() => {
  const table = document.querySelector('table.records');
  const vw = document.documentElement.clientWidth;
  const outside = el => {
    const box = el.getBoundingClientRect();
    return box.right > vw + 1 || box.left < -1;
  };
  const cells = [...table.querySelectorAll('td, th')];
  const spill = cells.filter(c => c.scrollWidth > c.clientWidth + 1);
  const headers = [...table.querySelectorAll('thead th[data-column]')];
  return {
    vw,
    pageWidth: document.documentElement.scrollWidth,
    tableRight: table.getBoundingClientRect().right,
    headersOutside: headers.filter(outside).map(th => th.textContent),
    cellsOutside: [...table.querySelectorAll('tbody td')].filter(outside).length,
    previewSpill: [...table.querySelectorAll('tr.preview *')].filter(outside).length,
    clipped: [...document.querySelectorAll('.detail-wrap, .table-scroll')]
      .filter(w => w.scrollWidth > w.clientWidth + 1).length,
    spilling: spill.map(c => `${c.dataset.column}: ${c.textContent.slice(0, 30)}`),
  };
}"""


WIDE_FONT = (
    "body, button, input, select, textarea "
    "{ font-family: Verdana, 'DejaVu Sans', sans-serif !important; }"
)
"""A wide font, so layouts that only fit with one platform's fonts fail everywhere."""


@pytest.fixture(params=["default font", "wide font"])
def font(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _open(
    page: Page, site: Site, width: int, query: str = "", font: str = "default font"
) -> dict[str, Any]:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{site.url}/{query}")
    if font == "wide font":
        page.add_style_tag(content=WIDE_FONT)
    expect(page.locator("table.records tbody tr").first).to_be_visible()
    expect(page.locator("body")).to_have_attribute("data-loading", "false")
    result: dict[str, Any] = page.evaluate(MEASURE)
    return result


@pytest.mark.parametrize("width", [1440, 1280, 1024])
@pytest.mark.parametrize("tab", ["IN_MONDO", "CANDIDATE"])
def test_all_columns_fit_without_scrolling(
    page: Page, site: Site, width: int, tab: str, font: str
) -> None:
    m = _open(page, site, width, f"?tab={tab}", font)
    assert m["pageWidth"] <= m["vw"], m
    assert m["tableRight"] <= m["vw"] + 1, m
    assert m["headersOutside"] == [], m
    assert m["cellsOutside"] == 0, m
    assert m["spilling"] == [], m
    headers = page.locator("table.records thead th[data-column]")
    expect(headers).to_have_count(len(TITLES))
    for i in range(len(TITLES)):
        expect(headers.nth(i)).to_be_visible()


@pytest.mark.parametrize("width", [800, 375])
def test_narrow_screens_show_every_column_of_a_row(
    page: Page, site: Site, width: int, font: str
) -> None:
    """Below 900 px each row is a card of label-value pairs, so nothing can overflow."""
    m = _open(page, site, width, font=font)
    assert m["pageWidth"] <= m["vw"], m
    assert m["cellsOutside"] == 0, m
    assert m["spilling"] == [], m
    first = page.locator("table.records tbody tr").first
    cells = first.locator("td[data-column]")
    expect(cells).to_have_count(len(TITLES))
    labels = cells.evaluate_all("cells => cells.map(c => c.dataset.label)")
    assert labels == TITLES
    for i in range(len(TITLES)):
        expect(cells.nth(i)).to_be_visible()


@pytest.mark.parametrize("width", [1440, 1280, 1024, 800, 375])
def test_expanded_preview_fits_too(page: Page, site: Site, width: int, font: str) -> None:
    _open(page, site, width, font=font)
    page.locator("table.records tbody tr button.expand").first.click()
    expect(page.locator("tr.preview table.detail-table")).to_be_visible()
    m: dict[str, Any] = page.evaluate(MEASURE)
    assert m["pageWidth"] <= m["vw"], m
    assert m["previewSpill"] == 0, m
    assert m["clipped"] == 0, m


SQUEEZED = """() => {
  for (const d of document.querySelectorAll('details')) d.open = true;
  const values = [...document.querySelectorAll('#record dl.object > dd')]
    .filter(dd => dd.textContent.trim() && dd.getClientRects().length);
  return {
    pageWidth: document.documentElement.scrollWidth,
    vw: document.documentElement.clientWidth,
    squeezed: values
      .filter(dd => dd.getBoundingClientRect().width < 80)
      .map(dd => `${dd.previousElementSibling?.textContent}: ${dd.textContent.slice(0, 20)}`),
  };
}"""


@pytest.mark.parametrize("width", [1440, 1280, 1100, 1024, 800, 390])
def test_record_details_never_squeeze_values(page: Page, site: Site, width: int, font: str) -> None:
    """Nested details stay readable in narrow places, e.g. a decision in the side column."""
    import httpx

    from tests.orcid import id_token

    record_id = "pkt_9c18e79339f2"
    history = httpx.get(f"{site.url}/api/records/{record_id}/decisions").json()["decisions"]
    if not history:
        record = httpx.get(f"{site.url}/api/records/{record_id}").json()["record"]
        signed = httpx.post(
            f"{site.url}/api/records/{record_id}/sign-off",
            json={
                "seen_version": record["record_version"],
                "seen_rule_version": record["inclusion_score"]["rule_version"],
                "seen_decision": None,
                "values": {"outcome": "accepted", "rationale": "Layout check."},
            },
            headers={"Authorization": f"Bearer {id_token()}"},
        )
        assert signed.status_code == 201, signed.text
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{site.url}/record.html?id={record_id}")
    if font == "wide font":
        page.add_style_tag(content=WIDE_FONT)
    expect(page.locator("#history li.decision")).to_have_count(1)
    m = page.evaluate(SQUEEZED)
    assert m["pageWidth"] <= m["vw"], m
    assert m["squeezed"] == [], m


@pytest.mark.parametrize("width", [1440, 1280, 1024, 800, 375])
def test_quick_sign_off_column_fits_too(page: Page, site: Site, width: int, font: str) -> None:
    """Logged in, each row gets sign-off buttons; every column must still fit."""
    import json

    from tests.orcid import id_token

    page.add_init_script(f"sessionStorage.setItem('meb.id_token', {json.dumps(id_token())})")
    m = _open(page, site, width, "?tab=CANDIDATE", font)
    expect(page.locator("table.records th.actions-col")).to_have_count(1)
    assert m["pageWidth"] <= m["vw"], m
    assert m["cellsOutside"] == 0, m
    assert m["spilling"] == [], m
    if width >= 1024:
        assert m["headersOutside"] == [], m


@pytest.mark.parametrize("logged_in", [False, True])
@pytest.mark.parametrize("width", [1440, 1280, 1024])
def test_a_very_long_word_cannot_widen_the_table(
    page: Page, long_word_site: Site, width: int, font: str, logged_in: bool
) -> None:
    """Real labels include 28-letter chemical names; they must wrap, not overflow."""
    if logged_in:
        import json

        from tests.orcid import id_token

        page.add_init_script(f"sessionStorage.setItem('meb.id_token', {json.dumps(id_token())})")
    m = _open(page, long_word_site, width, "", font)
    assert m["pageWidth"] <= m["vw"], m
    assert m["tableRight"] <= m["vw"] + 1, m
    assert m["cellsOutside"] == 0, m


VISUALS_OUTSIDE = """() => {
  const vw = document.documentElement.clientWidth;
  const drawn = 'figure.gauge *, figure.evidence-map *, figure.contributions *';
  const outside = e => {
    const b = e.getBoundingClientRect();
    return b.width && (b.right > vw + 1 || b.left < -1);
  };
  const clips = e =>
    getComputedStyle(e).overflow !== 'visible' && e.scrollWidth > e.clientWidth + 1;
  const name = e => e.className || e.tagName;
  return {
    vw,
    pageWidth: document.documentElement.scrollWidth,
    out: [...document.querySelectorAll(drawn)].filter(outside).map(name),
    clipped: [...document.querySelectorAll(drawn)].filter(clips).map(name),
  };
}"""


@pytest.mark.parametrize("width", [1440, 1024, 800, 375])
def test_the_record_page_visuals_fit(page: Page, site: Site, width: int, font: str) -> None:
    """The gauge, the evidence map and the contribution chart never widen the page."""
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{site.url}/record.html?id=pkt_9c18e79339f2")
    if font == "wide font":
        page.add_style_tag(content=WIDE_FONT)
    expect(page.locator("figure.evidence-map")).to_be_visible()
    page.locator("#record details", has_text="How the score was computed").locator(
        "summary"
    ).click()
    expect(page.locator("figure.contributions")).to_be_visible()
    m = page.evaluate(VISUALS_OUTSIDE)
    assert m["pageWidth"] <= m["vw"], m
    assert m["out"] == [], m
    assert m["clipped"] == [], m
