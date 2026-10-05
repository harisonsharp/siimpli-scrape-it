import csv
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from core.config import load_config
from core.runner import execute_job
from schemas.job_schema import RateLimitConfig
from scrapers import scrap_monster_prices as sm
from utils import db
from utils.ratelimit import RateLimiter, activate

REGIONS = [
    ("EXW China", "147", "$US/MT"),
    ("FOB China", "148", "$US/MT"),
    ("Port India", "165", "$US/Kg"),
]
PAGE_PATH = "/metal-prices/test-metal-123/655"


def page_html(regions=REGIONS, buttons=True):
    nav = "".join(f'<li><a href="#scrappricetabs-{i}" class="x">{label}</a></li>'
                  for i, (label, _, _) in enumerate(regions, 1))
    panes = ""
    for i, (label, rid, unit) in enumerate(regions, 1):
        panes += f'<div role="tabpanel" class="tab-pane" id="scrappricetabs-{i}">'
        panes += f'<div class="scrapitemprice"> <i></i>12.5<span>{unit}</span></div>'
        if buttons:
            panes += f'<a href="#" onclick="redrawmetalschart(\'{i}historygraph\',\'7\',\'{rid}\',\'655\')">1 Week</a>'
            panes += f'<a href="#" onclick="redrawmetalschart(\'{i}_graphcontainer\',\'30\',\'{rid}\',\'655\')">1 Month</a>'
            panes += f'<a href="#" onclick="redrawmetalschart(\'{i}_graphcontainer\',\'0\',\'{rid}\',\'655\')">All</a>'
        panes += ("<script>google.charts.setOnLoadCallback(drawChart);function drawChart(){var data = "
                  "google.visualization.arrayToDataTable([ ['Day', 'Price'], ['2025-08-20',1.5],['2025-09-16',2.5], ]);"
                  "}</script></div>")
    return f'<html><body><ul class="nav">{nav}</ul><div class="tab-content">{panes}</div></body></html>'


def chart_html(region_id, n=4):
    rows = ",".join(f"['2020-0{k + 1}-01',{float(region_id) + k}]" for k in range(n))
    return ("<script>google.charts.setOnLoadCallback(drawChart);function drawChart(){var data = "
            f"google.visualization.arrayToDataTable([ ['Day', ''], {rows}, ]);}}</script>")


class Site:
    """Local stand-in for the ScrapMonster page + chartdata endpoint."""

    def __init__(self, page=None, chart=None):
        self.page = page if page is not None else page_html()
        self.chart = chart or (lambda form: (200, {}, chart_html(form["region"])))
        self.gets, self.posts = [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, status, body, headers=None):
                self.send_response(status)
                self.send_header("Content-Type", "text/html")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body.encode())

            def do_GET(self):
                outer.gets.append(self.path)
                self._send(200, outer.page)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                outer.posts.append((self.path, form, dict(self.headers)))
                status, headers, body = outer.chart(form)
                self._send(status, body, headers)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.origin = f"http://127.0.0.1:{self.httpd.server_port}"
        self.page_url = self.origin + PAGE_PATH
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def site(monkeypatch):
    sites = []

    def make(**kw):
        s = Site(**kw)
        sites.append(s)
        monkeypatch.setattr(sm, "_ORIGIN", s.origin)
        monkeypatch.setattr(sm, "_CHARTDATA_URL", s.origin + "/metalprice/chartdata")
        return s
    yield make
    for s in sites:
        s.close()


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(sm.time, "sleep", lambda s: slept.append(s))
    return slept


# ---- parsing -------------------------------------------------------------

def test_parse_regions_reads_labels_units_and_all_range_params():
    regions = sm._parse_regions(page_html())
    assert [(r.label, r.region_id, r.unit, r.container) for r in regions] == [
        ("EXW China", "147", "$US/MT", "1_graphcontainer"),
        ("FOB China", "148", "$US/MT", "2_graphcontainer"),
        ("Port India", "165", "$US/Kg", "3_graphcontainer"),
    ]
    assert all(r.item_id == "655" for r in regions)   # the 7-day/30-day buttons are ignored


def test_parse_regions_empty_without_range_buttons():
    assert sm._parse_regions(page_html(buttons=False)) == []


def test_parse_points():
    assert sm._parse_points(chart_html("100", n=2)) == [("2020-01-01", 100.0), ("2020-02-01", 101.0)]
    assert sm._parse_points("<html>nothing</html>") == []


def test_slugs():
    assert sm._slugify_unit("$US/Kg") == "usd_per_kg"
    assert sm._commodity_slug_from_url("https://x.com/metal-prices/copper-cathode/869#") == "copper_cathode"


# ---- run() against the local site ---------------------------------------

def test_run_returns_full_history_for_every_region_as_tidy_rows(site):
    s = site()
    rows = sm.run({"url": s.page_url})

    assert len(rows) == 12                                  # 3 regions x 4 chart points (not the 2 inline ones)
    assert {r["region"] for r in rows} == {"exw_china", "fob_china", "port_india"}
    first = rows[0]
    assert first == {"commodity": "test_metal_123", "region": "exw_china",
                     "unit": "usd_per_mt", "date": "2020-01-01", "price": 147.0}
    assert {r["unit"] for r in rows if r["region"] == "port_india"} == {"usd_per_kg"}
    assert all(set(r) == {"commodity", "region", "unit", "date", "price"} for r in rows)

    assert s.gets == [PAGE_PATH]
    assert [p[1] for p in s.posts] == [
        {"itemid": "655", "region": "147", "range": "0", "container": "1_graphcontainer"},
        {"itemid": "655", "region": "148", "range": "0", "container": "2_graphcontainer"},
        {"itemid": "655", "region": "165", "range": "0", "container": "3_graphcontainer"},
    ]
    headers = {k.lower(): v for k, v in s.posts[0][2].items()}
    assert headers["referer"] == s.page_url and headers["x-requested-with"] == "XMLHttpRequest"


def test_regions_filter_by_label_or_id(site):
    s = site()
    rows = sm.run({"url": s.page_url, "regions": ["FOB China", "165"]})
    assert {r["region"] for r in rows} == {"fob_china", "port_india"}
    assert len(s.posts) == 2

    only_label_slug = sm.run({"url": s.page_url, "regions": ["exw_china"]})
    assert {r["region"] for r in only_label_slug} == {"exw_china"}


def test_unknown_region_yields_error_marker_listing_available(site):
    s = site()
    (row,) = sm.run({"url": s.page_url, "regions": ["Mars"]})
    assert row["status"] == "error" and "exw_china" in row["reason"] and s.posts == []


def test_page_without_range_buttons_falls_back_to_partial_inline_history(site):
    s = site(page=page_html(buttons=False))
    rows = sm.run({"url": s.page_url})
    assert [(r["region"], r["unit"], r["date"]) for r in rows] == [
        ("default", "usd_per_mt", "2025-08-20"), ("default", "usd_per_mt", "2025-09-16")]
    assert s.posts == []


def test_page_with_no_chart_data_gives_no_data_marker(site):
    s = site(page="<html><body>nothing here</body></html>")
    (row,) = sm.run({"url": s.page_url})
    assert row["status"] == "no_data" and row["commodity"] == "test_metal_123"


def test_region_with_empty_chart_is_skipped_and_all_empty_is_no_data(site):
    s = site(chart=lambda f: (200, {}, "<script></script>" if f["region"] == "165" else chart_html(f["region"])))
    assert {r["region"] for r in sm.run({"url": s.page_url})} == {"exw_china", "fob_china"}

    s2 = site(chart=lambda f: (200, {}, "<p>empty</p>"))
    (row,) = sm.run({"url": s2.page_url})
    assert row["status"] == "no_data"


def test_server_error_on_a_chart_gives_error_marker_not_a_crash(site):
    s = site(chart=lambda f: (500, {}, "boom"))
    (row,) = sm.run({"url": s.page_url})
    assert row["status"] == "error" and "500" in row["reason"]


def test_no_urls_marker():
    assert sm.run({}) == [{"status": "error", "reason": "no_urls_provided"}]


def test_multiple_urls_each_scraped_and_one_failure_does_not_stop_the_rest(site):
    s = site()
    dead = "http://127.0.0.1:1/metal-prices/dead/1"   # nothing listens on port 1
    rows = sm.run({"urls": [dead, s.page_url]})
    assert rows[0]["status"] == "error" and rows[0]["commodity"] == "dead"
    assert len([r for r in rows if "status" not in r]) == 12


# ---- pacing / rate limiting ---------------------------------------------

def test_own_pacing_between_requests_when_no_rate_limit(site, no_real_sleep):
    s = site()
    sm.run({"url": s.page_url, "delay_min": 1, "delay_max": 1})
    assert no_real_sleep == [1.0, 1.0, 1.0]          # 4 requests -> 3 gaps, none before the first


def test_defers_to_job_rate_limit_when_present(site, no_real_sleep, monkeypatch):
    s = site()
    waited = []
    monkeypatch.setattr(RateLimiter, "wait", lambda self, url: waited.append(url))
    with activate(RateLimitConfig(rate_limit=10, delay_min=0, delay_max=0)):
        sm.run({"url": s.page_url})
    assert len(waited) == 4 and waited[0] == s.page_url and waited[1].endswith("/metalprice/chartdata")
    assert no_real_sleep == []                       # the scraper's own sleeps are off


def test_429_on_a_chart_backs_the_domain_off_and_fails_that_page(site):
    import time
    s = site(chart=lambda f: (429, {"Retry-After": "120"}, "slow down"))
    with activate(RateLimitConfig(rate_limit=60, delay_min=0, delay_max=0)):
        (row,) = sm.run({"url": s.page_url})
    assert row["status"] == "error" and "429" in row["reason"]
    domain = sm.urlparse(s.origin).netloc
    assert db.reserve_slot(domain, 60, 0, 0, now=time.time()) > 100


# ---- browser mode --------------------------------------------------------

def test_use_browser_flag_and_deprecated_headless_alias():
    assert sm._use_browser({}) is False
    assert sm._use_browser({"browser": True}) is True
    assert sm._use_browser({"headless": False}) is True      # old meaning: visible browser
    assert sm._use_browser({"headless": True}) is False
    assert sm._use_browser({"browser": False, "headless": False}) is False   # explicit flag wins


def test_browser_fetcher_gets_page_and_posts_through_the_page_context(site):
    from utils.browser import get_browser_page
    s = site()
    with get_browser_page(headless=True) as page:
        fetcher = sm._BrowserFetcher(page, sm._Pacer(0, 0))
        html = fetcher.get(s.page_url)
        assert "EXW China" in html
        out = fetcher.post_chart({"itemid": "655", "region": "147", "range": "0",
                                  "container": "1_graphcontainer"}, referer=s.page_url)
    assert sm._parse_points(out)[0] == ("2020-01-01", 147.0)
    assert s.posts[0][1]["region"] == "147"


# ---- whole pipeline with the shipped job --------------------------------

def test_shipped_job_end_to_end_is_idempotent_and_keeps_regions_apart(site, tmp_path, monkeypatch):
    s = site()
    monkeypatch.setattr(RateLimiter, "wait", lambda self, url: None)
    job = load_config(Path(__file__).parent.parent / "jobs" / "scrap_monster_prices_job.yaml")
    out = tmp_path / "prices.csv"
    job = job.model_copy(update={
        "isolate": False,
        "output": job.output.model_copy(update={"path": str(out)}),
        "parameters": {"url": s.page_url},
    })

    assert execute_job(job) is True
    assert execute_job(job) is True                          # second run updates in place

    with open(out, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 12                                   # not 24: keyed on commodity+region+date
    assert list(rows[0]) == ["commodity", "region", "unit", "date", "price"]
    assert {(r["region"], r["unit"]) for r in rows} == {
        ("exw_china", "usd_per_mt"), ("fob_china", "usd_per_mt"), ("port_india", "usd_per_kg")}
    assert db.recent_runs("scrap_monster_prices")[0]["status"] == "success"


def test_shipped_job_rejects_marker_rows_without_overwriting_history(site, tmp_path, monkeypatch):
    s = site()
    monkeypatch.setattr(RateLimiter, "wait", lambda self, url: None)
    job = load_config(Path(__file__).parent.parent / "jobs" / "scrap_monster_prices_job.yaml")
    out = tmp_path / "prices.csv"
    base = {"isolate": False, "output": job.output.model_copy(update={"path": str(out)}),
            "retry": job.retry.model_copy(update={"retries": 1, "retry_delay": 0})}

    assert execute_job(job.model_copy(update={**base, "parameters": {"url": s.page_url}})) is True
    good = out.read_text(encoding="utf-8")

    s.chart = lambda f: (500, {}, "down")                    # the site breaks
    assert execute_job(job.model_copy(update={**base, "parameters": {"url": s.page_url}})) is False
    assert out.read_text(encoding="utf-8") == good
    assert "Output validation failed" in db.recent_runs("scrap_monster_prices")[0]["error"]
