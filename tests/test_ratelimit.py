import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from core.runner import execute_job
from schemas.job_schema import JobConfig, RateLimitConfig
from utils import db
from utils import ratelimit
from utils.http import get_with_retries
from utils.ratelimit import (
    RateLimiter, RobotsDisallowed, activate, domain_of, limiter_active,
    parse_retry_after, penalize, throttle,
)


@pytest.fixture
def fake_clock(monkeypatch):
    """Deterministic time: sleep advances the clock instead of blocking."""
    clock = {"t": 1_000_000.0, "sleeps": []}

    def fake_sleep(s):
        clock["sleeps"].append(s)
        clock["t"] += s
    monkeypatch.setattr("utils.ratelimit.time.time", lambda: clock["t"])
    monkeypatch.setattr("utils.ratelimit.time.sleep", fake_sleep)
    monkeypatch.setattr("utils.db.time.time", lambda: clock["t"])
    return clock


class Server:
    """Tiny local HTTP server; `handler(self)` is called for every GET."""

    def __init__(self, handler):
        outer = self
        self.paths = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.paths.append(self.path)
                handler(self)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def serve():
    servers = []

    def make(handler):
        srv = Server(handler)
        servers.append(srv)
        return srv
    yield make
    for srv in servers:
        srv.close()


def reply(h, status=200, body=b"ok", headers=None, ctype="text/html"):
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


# ---- pure planning -------------------------------------------------------

def test_plan_slot_enforces_gap_and_window():
    assert db.plan_slot([], 0, now=100, rate_limit=2, gap=5) == 100
    assert db.plan_slot([100], 0, now=100, rate_limit=2, gap=5) == 105
    # window full: third request waits for the oldest hit to leave the 60s window
    assert db.plan_slot([100, 100], 0, now=100, rate_limit=2, gap=0) == 160
    # backoff wins when later
    assert db.plan_slot([], 500, now=100, rate_limit=2, gap=0) == 500
    # a hit already outside the window doesn't count
    assert db.plan_slot([0, 100], 0, now=100, rate_limit=2, gap=0) == 100


def test_parse_retry_after():
    assert parse_retry_after({"Retry-After": "12"}) == 12
    assert parse_retry_after({"retry-after": "2"}) == 2
    assert parse_retry_after({"Retry-After": "99999"}) == ratelimit.MAX_BACKOFF_SECONDS
    assert parse_retry_after({"Retry-After": "garbage"}) is None
    assert parse_retry_after({}) is None and parse_retry_after(None) is None
    future = time.time() + 40
    http_date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(future))
    assert 35 <= parse_retry_after({"Retry-After": http_date}) <= 41


def test_domain_of():
    assert domain_of("https://WWW.Example.com:8080/a/b?q=1") == "www.example.com:8080"
    assert domain_of("example.com") == "example.com"


# ---- limiter behaviour ---------------------------------------------------

def test_limiter_caps_requests_per_minute(fake_clock):
    limiter = RateLimiter(rate_limit=2, delay_min=0, delay_max=0)
    for _ in range(3):
        limiter.wait("http://a.test/x")
    # the first two requests go straight through; only the third waits out the window
    assert fake_clock["sleeps"] == [pytest.approx(60, abs=0.01)]


def test_gap_counts_time_already_elapsed(fake_clock):
    limiter = RateLimiter(rate_limit=100, delay_min=5, delay_max=5)
    limiter.wait("http://a.test/")
    fake_clock["t"] += 7  # the scraper already slept 7s
    limiter.wait("http://a.test/")
    assert fake_clock["sleeps"] == []


def test_domains_are_independent(fake_clock):
    limiter = RateLimiter(rate_limit=1, delay_min=0, delay_max=0)
    limiter.wait("http://a.test/")
    limiter.wait("http://b.test/")
    assert fake_clock["sleeps"] == []


def test_budget_is_shared_between_limiters_and_jobs(fake_clock):
    """Two jobs (two limiter instances) hitting one domain share a single budget."""
    job1 = RateLimiter(rate_limit=2, delay_min=0, delay_max=0)
    job2 = RateLimiter(rate_limit=2, delay_min=0, delay_max=0)
    job1.wait("http://shared.test/a")
    job2.wait("http://shared.test/b")
    job1.wait("http://shared.test/c")        # third request in the window, from either job
    assert max(fake_clock["sleeps"]) == pytest.approx(60, abs=0.01)


def test_budget_is_shared_across_processes(tmp_path):
    """A second *process* sees the slots a first process reserved."""
    code = (
        "from utils import db\n"
        "print(db.reserve_slot('xproc.test', 1, 0, 0))\n"
    )
    env = {"SIIMPLI_DB": str(tmp_path / "test.db"), "PYTHONPATH": ".", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    waits = []
    for _ in range(2):
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
        waits.append(float(out.stdout.strip()))
    assert waits[0] == 0
    assert 55 < waits[1] <= 60   # rate_limit=1/min: the second process must wait


def test_backoff_blocks_all_limiters_for_the_domain(fake_clock):
    RateLimiter(10, 0, 0).backoff("http://slow.test/", 45)
    other = RateLimiter(10, 0, 0)
    other.wait("http://slow.test/page")
    assert fake_clock["sleeps"] == [pytest.approx(45, abs=0.01)]
    other.wait("http://elsewhere.test/")
    assert len(fake_clock["sleeps"]) == 1


def test_backoff_never_shortens(fake_clock):
    db.set_backoff("d.test", 100, now=fake_clock["t"])
    until = db.set_backoff("d.test", 10, now=fake_clock["t"])
    assert until == fake_clock["t"] + 100


def test_falls_back_to_local_budget_when_db_unavailable(fake_clock, monkeypatch):
    def broken(*a, **k):
        raise OSError("db gone")
    monkeypatch.setattr(db, "reserve_slot", broken)
    monkeypatch.setattr(db, "set_backoff", broken)
    limiter = RateLimiter(rate_limit=1, delay_min=0, delay_max=0)
    limiter.wait("http://a.test/")
    limiter.wait("http://a.test/")
    assert max(fake_clock["sleeps"]) == pytest.approx(60, abs=0.01)
    limiter.backoff("http://a.test/", 30)  # must not raise


def test_throttle_and_penalize_are_noops_without_limiter():
    assert not limiter_active()
    throttle("http://a.test/")
    assert penalize("http://a.test/", 429, {"Retry-After": "5"}) is None
    with activate(None):
        throttle("http://a.test/")
        assert not limiter_active()


def test_penalize_only_for_throttling_statuses(fake_clock):
    with activate(RateLimitConfig(rate_limit=10, delay_min=0, delay_max=0)):
        assert penalize("http://a.test/", 200, {}) is None
        assert penalize("http://a.test/", 500, {}) is None
        assert penalize("http://a.test/", 429, {"Retry-After": "7"}) == 7
        assert penalize("http://a.test/", 503, {}) == ratelimit.DEFAULT_BACKOFF_SECONDS


# ---- robots.txt ----------------------------------------------------------

def test_robots_disallow_is_enforced_only_when_enabled(serve):
    def handler(h):
        if h.path == "/robots.txt":
            reply(h, body=b"User-agent: *\nDisallow: /private\n", ctype="text/plain")
        else:
            reply(h)
    srv = serve(handler)

    with activate(RateLimitConfig(rate_limit=60, delay_min=0, delay_max=0, respect_robots=True)):
        throttle(f"{srv.url}/public")  # allowed
        with pytest.raises(RobotsDisallowed):
            throttle(f"{srv.url}/private/data")
        with pytest.raises(RobotsDisallowed):
            get_with_retries(f"{srv.url}/private/data")

    with activate(RateLimitConfig(rate_limit=60, delay_min=0, delay_max=0)):
        throttle(f"{srv.url}/private/data")  # respect_robots defaults to off


def test_missing_robots_txt_means_allowed(serve):
    srv = serve(lambda h: reply(h, status=404) if h.path == "/robots.txt" else reply(h))
    with activate(RateLimitConfig(rate_limit=60, delay_min=0, delay_max=0, respect_robots=True)):
        throttle(f"{srv.url}/anything")


# ---- http integration ----------------------------------------------------

def test_get_with_retries_429_backs_domain_off_for_everyone(serve, fake_clock):
    state = {"n": 0}

    def handler(h):
        state["n"] += 1
        if state["n"] == 1:
            reply(h, status=429, headers={"Retry-After": "20"})
        else:
            reply(h)
    srv = serve(handler)

    with activate(RateLimitConfig(rate_limit=60, delay_min=0, delay_max=0)):
        resp = get_with_retries(srv.url, backoff_factor=0.01)
    assert resp.status_code == 200 and state["n"] == 2
    assert any(s == pytest.approx(20, abs=0.5) for s in fake_clock["sleeps"])

    # a different job/limiter now also has to wait out the same backoff
    fake_clock["sleeps"].clear()
    state["n"] = 5
    db.set_backoff(domain_of(srv.url), 25, now=fake_clock["t"])
    RateLimiter(60, 0, 0).wait(srv.url)
    assert fake_clock["sleeps"] == [pytest.approx(25, abs=0.5)]


def test_get_with_retries_throttles_each_attempt(serve, monkeypatch):
    srv = serve(lambda h: reply(h, status=502))
    seen = []
    monkeypatch.setattr(RateLimiter, "wait", lambda self, url: seen.append(url))
    with activate(RateLimitConfig(rate_limit=60, delay_min=0, delay_max=0)):
        with pytest.raises(requests.exceptions.RetryError):
            get_with_retries(srv.url, retries=2, backoff_factor=0.0)
    assert len(seen) == 3 and len(srv.paths) == 3


# ---- runner / subprocess integration ------------------------------------

def test_runner_activates_limiter_for_scraper(tmp_path, monkeypatch):
    import sys as _sys
    import types
    seen = {}

    def fn(params=None):
        seen["active"] = limiter_active()
        throttle("http://a.test/")
        return [{"a": 1}]
    mod = types.ModuleType("fake_rl2")
    mod.run = fn
    monkeypatch.setitem(_sys.modules, "fake_rl2", mod)
    calls = []
    monkeypatch.setattr(RateLimiter, "wait", lambda self, url: calls.append((self.rate_limit, url)))
    job = JobConfig(
        job_name="rl_job", script="fake_rl2", schedule="hourly", isolate=False,
        output={"type": "json", "path": str(tmp_path / "o.json")},
        retry={"retries": 1, "retry_delay": 0},
        rate_limit={"rate_limit": 7, "delay_min": 0, "delay_max": 0},
    )
    assert execute_job(job)
    assert seen["active"] is True and calls == [(7, "http://a.test/")]
    assert not limiter_active()  # deactivated after the job


# ---- Playwright navigation throttling -----------------------------------

def test_browser_throttles_only_main_frame_navigations(serve, monkeypatch):
    from utils.browser import get_browser_page

    def handler(h):
        if h.path.startswith("/page"):
            reply(h, body=b"<html><body><img src='/pic.png'><iframe src='/frame'></iframe>hi</body></html>")
        else:
            reply(h, body=b"x", ctype="image/png" if h.path.endswith("png") else "text/html")
    srv = serve(handler)

    seen = []
    monkeypatch.setattr(RateLimiter, "wait", lambda self, url: seen.append(url))
    with activate(RateLimitConfig(rate_limit=60, delay_min=0, delay_max=0)):
        with get_browser_page(headless=True) as page:
            page.goto(f"{srv.url}/page1")
            page.goto(f"{srv.url}/page2")
    assert seen == [f"{srv.url}/page1", f"{srv.url}/page2"]   # no image, no iframe


def test_browser_records_429_backoff(serve, fake_clock):
    from utils.browser import get_browser_page
    srv = serve(lambda h: reply(h, status=429, headers={"Retry-After": "90"}))
    with activate(RateLimitConfig(rate_limit=60, delay_min=0, delay_max=0)):
        with get_browser_page(headless=True) as page:
            page.goto(srv.url)
    assert db.reserve_slot(domain_of(srv.url), 60, 0, 0, now=time.time()) > 60


def test_browser_blocks_robots_disallowed_navigation(serve):
    from utils.browser import get_browser_page

    def handler(h):
        if h.path == "/robots.txt":
            reply(h, body=b"User-agent: *\nDisallow: /secret\n", ctype="text/plain")
        else:
            reply(h, body=b"<html>open</html>")
    srv = serve(handler)
    with activate(RateLimitConfig(rate_limit=60, delay_min=0, delay_max=0, respect_robots=True)):
        with get_browser_page(headless=True) as page:
            page.goto(f"{srv.url}/ok")
            assert "open" in page.content()
            with pytest.raises(Exception):
                page.goto(f"{srv.url}/secret")
    assert "/secret" not in srv.paths


# ---- downloader ----------------------------------------------------------

def test_downloader_uses_shared_limiter_instead_of_local_waits(serve, tmp_path, monkeypatch):
    from utils.downloader import FileDownloader
    srv = serve(lambda h: reply(h, body=b"%PDF-data", ctype="application/pdf"))
    seen = []
    monkeypatch.setattr(RateLimiter, "wait", lambda self, url: seen.append(url))
    local_sleeps = []
    monkeypatch.setattr("utils.downloader.time.sleep", lambda s: local_sleeps.append(s))

    dl = FileDownloader(str(tmp_path / "pdf"), str(tmp_path / "xlsx"), delay_min=5, delay_max=5)
    with activate(RateLimitConfig(rate_limit=60, delay_min=0, delay_max=0)):
        result = dl.download_url(f"{srv.url}/a.pdf")
    assert result["status"] == "success"
    assert seen == [f"{srv.url}/a.pdf"] and local_sleeps == []


def test_downloader_standalone_keeps_local_limiter(serve, tmp_path, monkeypatch):
    from utils.downloader import FileDownloader
    srv = serve(lambda h: reply(h, body=b"%PDF-data", ctype="application/pdf"))
    local_sleeps = []
    monkeypatch.setattr("utils.downloader.time.sleep", lambda s: local_sleeps.append(s))
    dl = FileDownloader(str(tmp_path / "pdf"), str(tmp_path / "xlsx"), delay_min=3, delay_max=3)
    assert dl.download_url(f"{srv.url}/b.pdf")["status"] == "success"
    assert local_sleeps == [3]
