"""
Hardened HTTP client for declarative jobs (and so for agent-written ones).

Every request, including each redirect hop, must pass these checks:

* http/https only, no credentials in the URL, ports 80/443 only;
* the host must be on the allowlist (``allowed_domains.txt`` in the working
  directory, or the file named by ``SIIMPLI_ALLOWED_DOMAINS_FILE``). The default
  is deny: a missing or empty file refuses every request;
* every address the host resolves to must be a public one (no loopback,
  private, link-local or cloud-metadata ranges), which blocks SSRF;
* the response body is capped at ``MAX_BYTES``;
* ambient proxy settings are ignored, so they can't route around the checks.

Requests also go through the shared per-domain rate limiter (``throttle`` /
``penalize``) exactly like the other scrapers, so a job's ``rate_limit`` block
and ``respect_robots`` apply.

Known limit: the hostname is resolved for the check and again by ``requests``
when it connects, so a DNS-rebinding server could in principle answer
differently the second time. Run unattended jobs on a host with no sensitive
internal services reachable, and keep the allowlist to domains you trust.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import requests

from utils.ratelimit import parse_retry_after, penalize, throttle

logger = logging.getLogger(__name__)

ALLOWLIST_ENV = "SIIMPLI_ALLOWED_DOMAINS_FILE"
DEFAULT_ALLOWLIST = "allowed_domains.txt"
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 30
ALLOWED_PORTS = (None, 80, 443)
RETRY_STATUSES = (429, 500, 502, 503, 504)
# The only request headers a job may set; anything else (Cookie, Authorization,
# Host, ...) is refused when the job is validated.
ALLOWED_REQUEST_HEADERS = ("accept", "accept-language", "referer", "content-type", "x-requested-with")


class BlockedRequest(Exception):
    """A request was refused by the allowlist or the network-safety checks."""


def load_allowlist(path: Optional[str] = None) -> List[str]:
    """Domains from the allowlist file (lowercase; ``#`` comments allowed)."""
    file = Path(path or os.environ.get(ALLOWLIST_ENV) or DEFAULT_ALLOWLIST)
    try:
        lines = file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    domains = []
    for line in lines:
        entry = line.split("#", 1)[0].strip().lower()
        if entry:
            domains.append(entry.lstrip("."))
    return domains


def host_allowed(host: str, allowlist: List[str]) -> bool:
    """True if ``host`` equals an allowlisted domain or is a subdomain of one."""
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in allowlist)


def _resolve(host: str) -> List[str]:
    """All addresses ``host`` resolves to (patched in tests)."""
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except socket.gaierror as e:
        raise BlockedRequest(f"cannot resolve host {host!r}: {e}")


def _ip_blocked(ip: str) -> bool:
    """True for anything that isn't a plain public address (patched in tests)."""
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped
    return not addr.is_global


def check_url(url: str, allowlist: Optional[List[str]] = None) -> str:
    """Raise ``BlockedRequest`` unless ``url`` may be fetched; returns the URL."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedRequest(f"only http/https URLs are allowed, got {url!r}")
    host = parts.hostname
    if not host:
        raise BlockedRequest(f"URL has no host: {url!r}")
    if parts.username or parts.password:
        raise BlockedRequest("credentials in URLs are not allowed")
    try:
        port = parts.port
    except ValueError:
        raise BlockedRequest(f"invalid port in {url!r}")
    if port not in ALLOWED_PORTS:
        raise BlockedRequest(f"port {port} is not allowed (only 80 and 443)")

    allow = load_allowlist() if allowlist is None else allowlist
    if not host_allowed(host, allow):
        raise BlockedRequest(
            f"{host} is not on the domain allowlist ({DEFAULT_ALLOWLIST}); "
            "ask the owner to add it"
        )
    for ip in _resolve(host):
        if _ip_blocked(ip):
            raise BlockedRequest(f"{host} resolves to a non-public address ({ip})")
    return url


def _read_capped(response: requests.Response) -> None:
    """Read the body into ``response`` but refuse anything over ``MAX_BYTES``."""
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_BYTES:
        response.close()
        raise BlockedRequest(f"response is {declared} bytes, over the {MAX_BYTES} byte limit")
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        total += len(chunk)
        if total > MAX_BYTES:
            response.close()
            raise BlockedRequest(f"response exceeded the {MAX_BYTES} byte limit")
        chunks.append(chunk)
    response._content = b"".join(chunks)
    response._content_consumed = True


def request(
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    json: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
    retries: int = 2,
    allowlist: Optional[List[str]] = None,
) -> requests.Response:
    """Fetch ``url`` under the safety rules above, following redirects manually
    so each hop is re-checked, and retrying 429/5xx through the shared limiter."""
    allow = load_allowlist() if allowlist is None else allowlist
    session = requests.Session()
    session.trust_env = False      # no proxy / netrc surprises
    method = method.upper()
    current = url
    redirects = 0
    attempt = 0

    while True:
        check_url(current, allow)
        throttle(current)
        response = session.request(
            method, current, params=params, data=data, json=json, headers=headers,
            allow_redirects=False, stream=True, timeout=TIMEOUT_SECONDS,
        )
        status = response.status_code

        if status in (301, 302, 303, 307, 308) and response.headers.get("Location"):
            response.close()
            redirects += 1
            if redirects > MAX_REDIRECTS:
                raise BlockedRequest(f"more than {MAX_REDIRECTS} redirects from {url}")
            current = urljoin(current, response.headers["Location"])
            if status in (301, 302, 303) and method == "POST":
                method, data, json = "GET", None, None
            params = None      # already part of the first URL
            continue

        if status in RETRY_STATUSES:
            backed_off = penalize(current, status, response.headers)
            response.close()
            if attempt >= retries:
                raise requests.exceptions.RetryError(
                    f"{method} {current} still returning {status} after {retries} retries"
                )
            if backed_off is None:
                delay = parse_retry_after(response.headers)
                time.sleep(0.5 * (2 ** attempt) if delay is None else delay)
            attempt += 1
            continue

        _read_capped(response)
        logger.info("%s %s -> %s (%d bytes)", method, current, status, len(response.content))
        return response
