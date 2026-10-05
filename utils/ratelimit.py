"""
Per-domain request rate limiting shared across jobs and processes.

The runner (or the subprocess worker) activates a ``RateLimiter`` for the
duration of a job when the job YAML has a ``rate_limit`` block.  Every request
helper then calls ``throttle(url)`` first:

* ``utils.http.get_with_retries``       — plain HTTP
* ``utils.browser`` page/context helpers — main-frame navigations in Playwright
* ``utils.downloader.FileDownloader``    — file downloads

Limits are per *domain*, not per job: request slots are reserved in the SQLite
database (``utils.db``), so concurrent jobs and the scheduler's worker
processes hitting the same site share one budget.  If the database is
unavailable the limiter degrades to a per-process budget.

Adaptive backoff: ``penalize()`` records a 429/503 (honouring ``Retry-After``)
as a per-domain backoff that every job sees.  Optionally ``respect_robots``
refuses URLs disallowed by robots.txt.
"""
import email.utils
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Dict, Iterator, List, Mapping, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from schemas.job_schema import RateLimitConfig
from utils import db

logger = logging.getLogger(__name__)

THROTTLE_STATUSES = (429, 503)
DEFAULT_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 600.0


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids fetching a URL and respect_robots is on."""


def domain_of(url: str) -> str:
    """Host (with port) of a URL, lowercased; a bare domain is returned as-is."""
    return (urlparse(url).netloc or url).lower()


def parse_retry_after(headers: Optional[Mapping[str, str]]) -> Optional[float]:
    """Seconds from a Retry-After header (delta-seconds or HTTP date), or None."""
    if not headers:
        return None
    value = headers.get("Retry-After") or headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            return None
    return max(0.0, min(seconds, MAX_BACKOFF_SECONDS))


class RateLimiter:
    """Domain-level requests-per-minute cap plus a polite gap between requests.

    The gap is a minimum time since the previous request to the same domain
    (from any job), so time a scraper already spent sleeping counts towards it.
    """

    def __init__(self, rate_limit: int, delay_min: float, delay_max: float,
                 respect_robots: bool = False):
        self.rate_limit = max(1, rate_limit)
        self.delay_min = max(0.0, delay_min)
        self.delay_max = max(self.delay_min, delay_max)
        self.respect_robots = respect_robots
        self._warned = False
        # Per-process fallback state, used only if the database is unavailable
        self._local_hits: Dict[str, List[float]] = {}
        self._local_backoff: Dict[str, float] = {}
        self._robots: Dict[str, Optional[RobotFileParser]] = {}

    # -- scheduling -----------------------------------------------------

    def _reserve(self, domain: str) -> float:
        try:
            return db.reserve_slot(domain, self.rate_limit, self.delay_min, self.delay_max)
        except Exception as e:
            self._warn_fallback(e)
            now = time.time()
            hits = [h for h in self._local_hits.get(domain, []) if h > now - 60]
            gap = self.delay_min if self.delay_min == self.delay_max else \
                (self.delay_min + self.delay_max) / 2
            slot = db.plan_slot(hits, self._local_backoff.get(domain, 0.0), now,
                                self.rate_limit, gap)
            self._local_hits[domain] = hits + [slot]
            return max(0.0, slot - now)

    def _warn_fallback(self, error: Exception) -> None:
        if not self._warned:
            logger.warning(f"Shared rate-limit state unavailable ({error}); "
                           "limiting this process only")
            self._warned = True

    def wait(self, url: str) -> None:
        """Block until a request to ``url`` is allowed, then count it."""
        if self.respect_robots and not self.robots_allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        delay = self._reserve(domain_of(url))
        if delay > 0:
            logger.debug("Rate limit: waiting %.1fs before %s", delay, domain_of(url))
            time.sleep(delay)

    def backoff(self, url: str, seconds: float) -> None:
        """Hold off all requests to ``url``'s domain for ``seconds``."""
        domain = domain_of(url)
        try:
            db.set_backoff(domain, seconds)
        except Exception as e:
            self._warn_fallback(e)
            self._local_backoff[domain] = max(self._local_backoff.get(domain, 0.0),
                                              time.time() + seconds)

    # -- robots.txt -----------------------------------------------------

    def robots_allows(self, url: str) -> bool:
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return True
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: Optional[RobotFileParser] = None
            try:
                resp = requests.get(f"{origin}/robots.txt", timeout=10)
                if resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
            except Exception as e:  # unreachable robots.txt means "no rules"
                logger.debug("Could not read robots.txt for %s: %s", origin, e)
            self._robots[origin] = parser
        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch("*", url)


_current: ContextVar[Optional[RateLimiter]] = ContextVar("current_rate_limiter", default=None)


@contextmanager
def activate(config: Optional[RateLimitConfig]) -> Iterator[Optional[RateLimiter]]:
    """Make a limiter built from ``config`` current; no-op when config is None."""
    limiter = (
        RateLimiter(config.rate_limit, config.delay_min, config.delay_max, config.respect_robots)
        if config else None
    )
    token = _current.set(limiter)
    try:
        yield limiter
    finally:
        _current.reset(token)


def current_limiter() -> Optional[RateLimiter]:
    """The running job's limiter, or None.

    Code that runs outside the job's own call stack (e.g. Playwright's sync API
    dispatches event handlers in a separate greenlet, which does not inherit
    context variables) should capture this once and pass it as ``limiter=``.
    """
    return _current.get()


def limiter_active() -> bool:
    """True when the running job has a rate_limit block."""
    return _current.get() is not None


def throttle(url: str, limiter: Optional[RateLimiter] = None) -> None:
    """Wait on the active (or given) limiter for ``url``'s domain, if any."""
    limiter = limiter or _current.get()
    if limiter:
        limiter.wait(url)


def penalize(url: str, status_code: int,
             headers: Optional[Mapping[str, str]] = None,
             limiter: Optional[RateLimiter] = None) -> Optional[float]:
    """Back the domain off after a 429/503 response.

    Honours ``Retry-After`` (default 30s, capped at 10 minutes).  Returns the
    seconds backed off, or None if nothing was recorded (no active limiter, or
    a status that doesn't signal throttling).
    """
    limiter = limiter or _current.get()
    if not limiter or status_code not in THROTTLE_STATUSES:
        return None
    seconds = parse_retry_after(headers)
    if seconds is None:
        seconds = DEFAULT_BACKOFF_SECONDS
    logger.warning("HTTP %s from %s — backing the domain off for %.0fs",
                   status_code, domain_of(url), seconds)
    limiter.backoff(url, seconds)
    return seconds
