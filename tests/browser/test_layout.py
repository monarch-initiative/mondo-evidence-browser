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
