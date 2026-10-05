import pytest
import requests

from core import safe_http
from tests.conftest_site import Site

REAL_IP_BLOCKED = safe_http._ip_blocked      # before any fixture loosens it


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


@pytest.fixture
def allow_local(site, tmp_path, monkeypatch):
    """Allowlist 127.0.0.1 and let the test server's port and loopback address through."""
    allow = tmp_path / "allowed_domains.txt"
    allow.write_text("# test\n127.0.0.1\n", encoding="utf-8")
    monkeypatch.setenv(safe_http.ALLOWLIST_ENV, str(allow))
    monkeypatch.setattr(safe_http, "_ip_blocked", lambda ip: False)
    monkeypatch.setattr(safe_http, "ALLOWED_PORTS", (None, 80, 443, site.port))
    return site


def test_default_is_deny_without_an_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv(safe_http.ALLOWLIST_ENV, str(tmp_path / "missing.txt"))
    with pytest.raises(safe_http.BlockedRequest, match="allowlist"):
        safe_http.check_url("https://example.com/")


def test_host_allowed_matches_subdomains_but_not_lookalikes():
    allow = ["example.com"]
    assert safe_http.host_allowed("example.com", allow)
    assert safe_http.host_allowed("api.Example.com", allow)
    assert not safe_http.host_allowed("evilexample.com", allow)
    assert not safe_http.host_allowed("example.com.evil.net", allow)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "https://user:pw@example.com/",
    "https://example.com:8080/", "https:///nohost",
])
def test_bad_urls_are_refused(url):
    with pytest.raises(safe_http.BlockedRequest):
        safe_http.check_url(url, ["example.com"])


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",
    "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1",
])
def test_non_public_addresses_are_blocked(ip):
    assert safe_http._ip_blocked(ip)


def test_public_addresses_pass():
    assert not safe_http._ip_blocked("93.184.216.34")
    assert not safe_http._ip_blocked("2606:2800:220:1:248:1893:25c8:1946")


def test_allowlisted_host_resolving_to_a_private_address_is_blocked(monkeypatch):
    monkeypatch.setattr(safe_http, "_resolve", lambda host: ["93.184.216.34", "10.0.0.7"])
    with pytest.raises(safe_http.BlockedRequest, match="non-public"):
        safe_http.check_url("https://example.com/", ["example.com"])


def test_loopback_is_blocked_even_when_allowlisted(site, tmp_path, monkeypatch):
    allow = tmp_path / "a.txt"
    allow.write_text("127.0.0.1\n", encoding="utf-8")
    monkeypatch.setenv(safe_http.ALLOWLIST_ENV, str(allow))
    monkeypatch.setattr(safe_http, "ALLOWED_PORTS", (None, site.port))
    with pytest.raises(safe_http.BlockedRequest, match="non-public"):
        safe_http.request("GET", site.base + "/")
    assert site.hits == []          # nothing was sent


def test_plain_get_and_post(allow_local):
    s = allow_local
    s.routes["/hello"] = (200, {}, "<p>hi</p>")
    s.routes["/form"] = (200, {}, {"ok": True})
    assert safe_http.request("GET", s.base + "/hello", params={"a": "1"}).text == "<p>hi</p>"
    assert s.hits[0][1] == "/hello?a=1"
    assert safe_http.request("POST", s.base + "/form", data={"x": "y"}).json() == {"ok": True}
    assert s.hits[1][3] == "x=y"


def test_redirect_to_another_domain_is_blocked_at_the_hop(allow_local):
    s = allow_local
    s.routes["/go"] = (302, {"Location": "https://example.org/secret"}, "")
    with pytest.raises(safe_http.BlockedRequest, match="example.org"):
        safe_http.request("GET", s.base + "/go")


def test_redirect_to_a_private_address_is_blocked(allow_local, monkeypatch):
    s = allow_local
    s.routes["/go"] = (302, {"Location": "http://169.254.169.254/latest/meta-data"}, "")
    # only loopback is loosened (so the test server is reachable); the cloud
    # metadata address is allowlisted too, so the address check is what stops it
    monkeypatch.setattr(safe_http, "_ip_blocked", lambda ip: False if ip == "127.0.0.1" else REAL_IP_BLOCKED(ip))
    with pytest.raises(safe_http.BlockedRequest, match="non-public"):
        safe_http.request("GET", s.base + "/go", allowlist=["127.0.0.1", "169.254.169.254"])


def test_redirects_within_the_allowlist_work_and_post_becomes_get(allow_local):
    s = allow_local
    s.routes["/a"] = (303, {"Location": "/b"}, "")
    s.routes["/b"] = (200, {}, "landed")
    r = safe_http.request("POST", s.base + "/a", data={"x": "1"})
    assert r.text == "landed"
    assert [h[0] for h in s.hits] == ["POST", "GET"]


def test_redirect_loop_is_cut_off(allow_local):
    s = allow_local
    s.routes["/loop"] = (302, {"Location": "/loop"}, "")
    with pytest.raises(safe_http.BlockedRequest, match="redirects"):
        safe_http.request("GET", s.base + "/loop")


def test_oversized_response_is_refused(allow_local, monkeypatch):
    s = allow_local
    s.routes["/big"] = (200, {}, "x" * 5000)
    monkeypatch.setattr(safe_http, "MAX_BYTES", 1000)
    with pytest.raises(safe_http.BlockedRequest, match="limit"):
        safe_http.request("GET", s.base + "/big")


def test_proxy_environment_is_ignored(allow_local, monkeypatch):
    s = allow_local
    s.routes["/p"] = (200, {}, "direct")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    assert safe_http.request("GET", s.base + "/p").text == "direct"


def test_retries_503_then_succeeds_and_gives_up_eventually(allow_local):
    s = allow_local
    calls = {"n": 0}

    def flaky(handler):
        calls["n"] += 1
        return (503, {"Retry-After": "0"}, "busy") if calls["n"] < 3 else (200, {}, "ok")

    s.routes["/flaky"] = flaky
    assert safe_http.request("GET", s.base + "/flaky").text == "ok"

    s.routes["/down"] = (503, {"Retry-After": "0"}, "no")
    with pytest.raises(requests.exceptions.RetryError):
        safe_http.request("GET", s.base + "/down", retries=1)
