import pytest

from core.config import load_config
from scrapers import westmetall_lme as wm

OVERVIEW_HTML = """
<table><tr><th>Official LME-Prices in US Dollar</th><th>20. October 2026</th></tr>
<tr><td>in US Dollar per ton</td><td>Settlement Kasse</td><td>3 months</td></tr>
<tr><td>Copper</td><td>14,540.00</td><td>14,357.00</td></tr></table>"""


def table_html(commodity, field, rows, years=(2025, 2026)):
    links = "".join(
        f'<a href="https://www.westmetall.com/en/markdaten.php?action=table&amp;field={field}&amp;year={y}">{y}</a>'
        for y in sorted(years, reverse=True))
    header = (f'<tr class="shaded"><th class="text">date</th><th class="text">{commodity} Cash-Settlement</th>'
              f'<th class="text">{commodity} 3-month</th><th class="text last">{commodity} stock</th></tr>')
    body = "".join(f"<tr><td >{d}</td><td >{c}</td><td >{t}</td><td class=\"last\">1,000</td></tr>"
                   for d, c, t in rows)
    return (f"<div><p>{links}</p></div><table><thead>{header}</thead><tbody>"
            f"{body}{header}</tbody></table>")


class FakeFetcher:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        if url in self.fail:
            raise RuntimeError("boom")
        if url == wm._OVERVIEW_URL:
            return OVERVIEW_HTML
        field = url.split("field=")[1].split("&")[0]
        commodity = next(c for f, c in wm.METALS.values() if f == field)
        if url.endswith("year=2025"):
            return table_html(commodity, field, [("31. December 2025", "12,000.50", "12,100.00"),
                                                 ("02. January 2025", "-", "9,000.00")])
        return table_html(commodity, field, [("20. October 2026", "14,540.00", "14,357.00")])


@pytest.fixture
def fake(monkeypatch):
    f = FakeFetcher()
    monkeypatch.setattr(wm, "_Fetcher", lambda pacer: f)
    return f


def test_parses_dates_and_prices():
    assert wm.parse_date("02. January 2008") == "2008-01-02"
    assert wm.parse_date("20. October 2026") == "2026-10-20"
    with pytest.raises(ValueError):
        wm.parse_date("2026-10-20")
    assert wm.parse_price("14,540.00") == 14540.0
    assert wm.parse_price("-") is None


def test_reads_both_price_columns_and_skips_dashes():
    html = table_html("LME Tin", "LME_Sn_cash", [("31. December 2025", "12,000.50", "12,100.00"),
                                                 ("02. January 2025", "-", "9,000.00")])
    assert wm.parse_table(html, "LME Tin") == [
        ("2025-12-31", "Cash-Settlement", 12000.5),
        ("2025-12-31", "3-month", 12100.0),
        ("2025-01-02", "3-month", 9000.0),
    ]


def test_reworded_headers_fail_instead_of_starting_new_series():
    html = table_html("LME Tin", "LME_Sn_cash", [("31. December 2025", "1", "2")]).replace(
        "Cash-Settlement", "Cash Settlement")
    with pytest.raises(ValueError):
        wm.parse_table(html, "LME Tin")


def test_scrapes_every_year_of_every_metal(fake):
    rows = wm.run({})
    assert all("status" not in r for r in rows)
    assert {r["metal"] for r in rows} == set(wm.METALS)
    assert {"metal": "aluminium", "commodity": "LME Aluminium", "market": "Cash-Settlement",
            "unit": "USD/MT", "date": "2025-12-31", "price": 12000.5} in rows
    # The current year comes from the page without a year, and is not fetched twice.
    assert not any(u.endswith("year=2026") for u in fake.requested)
    assert len(fake.requested) == 1 + 2 * len(wm.METALS)


def test_metals_and_from_year_filters(fake):
    rows = wm.run({"metals": ["nickel"], "from_year": 2026})
    assert {(r["metal"], r["date"]) for r in rows} == {("nickel", "2026-10-20")}
    assert not any("year=" in u for u in fake.requested)
    with pytest.raises(ValueError):
        wm.run({"metals": ["iron"]})


def test_failed_year_becomes_marker_row(monkeypatch):
    f = FakeFetcher(fail={wm.table_url("LME_Pb_cash", 2025)})
    monkeypatch.setattr(wm, "_Fetcher", lambda pacer: f)
    rows = wm.run({"metals": ["lead", "zinc"]})
    assert any(r.get("status") == "error" and r["metal"] == "lead" for r in rows)
    assert any(r["metal"] == "zinc" and r["date"] == "2025-12-31" for r in rows)


def test_unit_change_on_overview_fails_the_run(monkeypatch):
    f = FakeFetcher()
    f.get = lambda url: OVERVIEW_HTML.replace("US Dollar per ton", "Euro per ton")
    monkeypatch.setattr(wm, "_Fetcher", lambda pacer: f)
    with pytest.raises(ValueError):
        wm.run({})


def test_job_yaml_is_valid():
    config = load_config("jobs/westmetall_base_metals.yaml")
    assert config is not None
    assert config.script == "scrapers.westmetall_lme"
    assert config.output.key == ["commodity", "market", "date"]
    assert config.parameters["metals"] == list(wm.METALS)
