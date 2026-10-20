import pytest

from core.config import load_config
from scrapers import scrapmonster_all_metals as sm

START = "https://www.scrapmonster.com/metal-prices/start-page"

START_HTML = """
<div class="linkgroup">
<a href="https://www.scrapmonster.com/metal-prices/base-metals" class="seclabel">Base Metal Prices</a>
<a href="https://www.scrapmonster.com/metal-prices/base-metals/copper/869">Copper</a>
<a href="https://www.scrapmonster.com/metal-prices/base-metals/lead/878">Lead</a>
<a href="https://www.scrapmonster.com/metal-prices/minor-metals" class="seclabel">Minor Metal Prices</a>
<a href="https://www.scrapmonster.com/metal-prices/minor-metals/antimony/653">Antimony</a>
<a href="https://www.scrapmonster.com/metal-prices/rare-earths" class="seclabel">Rare Earth Prices</a>
<a href="https://www.scrapmonster.com/metal-prices/rare-earths/cerium/1">Cerium</a>
</div>"""

METAL_HTML = """
<table><tr><th>Commodity</th><th>Origin</th></tr>
<tr><td><a href="https://www.scrapmonster.com/metal-prices/copper-cathode-9999-min-price/872">Copper Cathode</a></td><td>China</td></tr>
</table>"""

COMMODITY_HTML = """
<ul><li><a href="#scrappricetabs-1">EXW China</a></li><li><a href="#scrappricetabs-2">FOB China</a></li></ul>
<button onclick="metalshistoricalprice('1','147','872')">View</button>
<button onclick="metalshistoricalprice('2','148','872')">View</button>"""

HISTORY_HTML = ("<tr><td>2026-09-21</td><td>1,230.5</td><td>USD/MT</td></tr>"
                "<tr><td>2020-11-18</td><td>820.9</td><td>USD/MT</td></tr>")


class FakeFetcher:
    def __init__(self, pages=None, fail=()):
        self.pages = {START: START_HTML,
                      "https://www.scrapmonster.com/metal-prices/base-metals/copper/869": METAL_HTML}
        self.pages.update({
            "https://www.scrapmonster.com/metal-prices/base-metals/lead/878": METAL_HTML.replace("872", "880"),
            "https://www.scrapmonster.com/metal-prices/minor-metals/antimony/653": METAL_HTML,
            "https://www.scrapmonster.com/metal-prices/rare-earths/cerium/1": METAL_HTML,
        })
        self.pages.update(pages or {})
        self.fail = set(fail)
        self.requested, self.posts = [], []

    def get(self, url):
        self.requested.append(url)
        if url in self.fail:
            raise RuntimeError("boom")
        if "min-price" in url:
            return COMMODITY_HTML
        return self.pages[url]

    def history(self, item_id, country, start, end, referer):
        self.posts.append((item_id, country, start, end))
        return HISTORY_HTML


@pytest.fixture
def fake(monkeypatch):
    f = FakeFetcher()
    monkeypatch.setattr(sm, "_Fetcher", lambda pacer: f)
    return f


def test_parses_the_three_link_groups():
    groups = sm._parse_groups(START_HTML, START)
    assert {g: [m for m, _ in v] for g, v in groups.items()} == {
        "base_metals": ["copper", "lead"],
        "minor_metals": ["antimony"],
        "rare_earths": ["cerium"],
    }


def test_crawls_every_commodity_market_with_full_history(fake):
    rows = sm.run({"start_url": START})
    assert {r["group"] for r in rows} == {"base_metals", "minor_metals", "rare_earths"}
    assert all("status" not in r for r in rows)
    copper = [r for r in rows if r["metal"] == "copper"]
    assert len(copper) == 4  # 2 markets x 2 dates
    assert {"commodity": "copper_cathode_9999_min_price", "item_id": "872", "region": "EXW China",
            "unit": "USD/MT", "date": "2020-11-18", "price": 820.9,
            "group": "base_metals", "metal": "copper"} in copper
    assert {p[3] for p in fake.posts} == {"2099-12-31"}
    assert {p[2] for p in fake.posts} == {"1990-01-01"}


def test_groups_and_metals_filters(fake):
    rows = sm.run({"start_url": START, "groups": ["Minor Metal Prices"]})
    assert {r["metal"] for r in rows} == {"antimony"}
    rows = sm.run({"start_url": START, "metals": ["lead"]})
    assert {r["metal"] for r in rows} == {"lead"}
    with pytest.raises(ValueError):
        sm.run({"start_url": START, "groups": ["nope"]})


def test_failed_page_becomes_marker_row(monkeypatch):
    f = FakeFetcher(fail={"https://www.scrapmonster.com/metal-prices/base-metals/lead/878"})
    monkeypatch.setattr(sm, "_Fetcher", lambda pacer: f)
    rows = sm.run({"start_url": START, "groups": ["base_metals"]})
    assert any(r.get("status") == "error" and r["metal"] == "lead" for r in rows)


def test_job_yaml_is_valid():
    config = load_config("jobs/scrapmonster_all_metals.yaml")
    assert config is not None
    assert config.script == "scrapers.scrapmonster_all_metals"
    assert config.output.key == ["item_id", "region", "date"]


def test_base_metals_job_yaml_is_valid():
    config = load_config("jobs/scrapmonster_base_metals.yaml")
    assert config is not None
    assert config.script == "scrapers.scrapmonster_all_metals"
    assert config.output.key == ["item_id", "region", "date"]
    assert config.parameters["groups"] == ["base_metals"]
    assert config.parameters["metals"] == ["copper", "lead", "tin", "zinc"]
