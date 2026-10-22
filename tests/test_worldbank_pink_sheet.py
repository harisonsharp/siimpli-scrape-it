import io

import pytest
from openpyxl import Workbook

from core.config import load_config
from scrapers import worldbank_pink_sheet as wb

XLSX_URL = ("https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/"
            "related/CMO-Historical-Data-Monthly.xlsx")
LANDING_HTML = f"""
<a href="{XLSX_URL}">Monthly prices
    , October 2026 (XLS)</a>
<a href="{XLSX_URL.replace('Monthly', 'Annual')}">Annual prices, October 2026 (XLS)</a>"""

HEADERS = [None, "Copper", "Gold", "Platinum", "Silver"]
UNITS = [None, "($/mt)", "($/troy oz)", "($/troy oz)", "($/troy oz)"]
MONTHS = [
    ["1960M01", 715, 35, 84, 0.9],
    ["1960M02", 728, 35.27, "…", None],
    ["2026M09", 10200, 4319, 1784, 64.6],
]


def workbook_bytes(headers=HEADERS, units=UNITS, months=MONTHS, sheet=wb.SHEET):
    book = Workbook()
    ws = book.active
    ws.title = "Mismatch Details"
    ws = book.create_sheet(sheet)
    ws.append(["World Bank Commodity Price Data (The Pink Sheet)"])
    ws.append(["monthly prices in nominal US dollars, 1960 to present"])
    ws.append([])
    ws.append(["Updated on October 02, 2026"])
    ws.append(headers)
    ws.append(units)
    for row in months:
        ws.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


class FakeFetcher:
    def __init__(self, workbook=None):
        self.workbook = workbook or workbook_bytes()
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        if url == wb.LANDING_URL:
            return LANDING_HTML.encode()
        if url == XLSX_URL:
            return self.workbook
        raise RuntimeError(f"unexpected {url}")


def test_finds_the_monthly_workbook_link():
    assert wb.find_workbook_url(LANDING_HTML) == XLSX_URL
    with pytest.raises(ValueError):
        wb.find_workbook_url("<a href='/x/CMO-Historical-Data-Annual.xlsx'>Annual</a>")


def test_parses_periods_and_prices():
    assert wb.parse_period("1960M01") == "1960-01-01"
    assert wb.parse_period("2026M09") == "2026-09-01"
    for bad in ("2026-09", "2026M13", "Average"):
        with pytest.raises(ValueError):
            wb.parse_period(bad)
    assert wb.parse_price(4319) == 4319.0
    assert wb.parse_price(None) is None
    assert wb.parse_price("…") is None
    with pytest.raises(ValueError):
        wb.parse_price("n/a (see note)")


def test_reads_each_metal_and_skips_blank_months():
    rows = wb.parse_workbook(workbook_bytes(), list(wb.METALS))
    assert {"metal": "silver", "commodity": "Silver", "market": "Monthly average",
            "unit": "USD/oz", "date": "2026-09-01", "price": 64.6} in rows
    assert sorted((r["metal"], r["date"]) for r in rows if r["date"] == "1960-02-01") == [
        ("gold", "1960-02-01")]
    assert len(rows) == 3 + 1 + 3
    assert not any(r["commodity"] == "Copper" for r in rows)


def test_unit_change_fails_instead_of_starting_new_series():
    units = [None, "($/mt)", "($/troy oz)", "($/troy oz)", "(cents/troy oz)"]
    with pytest.raises(ValueError, match="Silver"):
        wb.parse_workbook(workbook_bytes(units=units), list(wb.METALS))
    # Only the wanted metals are checked.
    assert wb.parse_workbook(workbook_bytes(units=units), ["gold"])


def test_renamed_column_or_sheet_fails():
    headers = [None, "Copper", "Gold", "Platinum", "Silver, London"]
    with pytest.raises(ValueError, match="Silver"):
        wb.parse_workbook(workbook_bytes(headers=headers), list(wb.METALS))
    with pytest.raises(ValueError, match="sheet"):
        wb.parse_workbook(workbook_bytes(sheet="Prices"), list(wb.METALS))


def test_run_fetches_landing_page_then_workbook(monkeypatch):
    f = FakeFetcher()
    monkeypatch.setattr(wb, "_Fetcher", lambda pacer: f)
    rows = wb.run({"metals": ["platinum"]})
    assert f.requested == [wb.LANDING_URL, XLSX_URL]
    assert [(r["date"], r["price"]) for r in rows] == [("1960-01-01", 84.0), ("2026-09-01", 1784.0)]
    with pytest.raises(ValueError):
        wb.run({"metals": ["palladium"]})


def test_metal_without_prices_fails_the_run(monkeypatch):
    months = [["1960M01", 715, 35, None, 0.9]]
    f = FakeFetcher(workbook_bytes(months=months))
    monkeypatch.setattr(wb, "_Fetcher", lambda pacer: f)
    with pytest.raises(ValueError, match="platinum"):
        wb.run({})


def test_job_yaml_is_valid():
    config = load_config("jobs/worldbank_precious_metals.yaml")
    assert config is not None
    assert config.script == "scrapers.worldbank_pink_sheet"
    assert config.output.key == ["commodity", "market", "date"]
    assert sorted(config.parameters["metals"]) == sorted(wb.METALS)
