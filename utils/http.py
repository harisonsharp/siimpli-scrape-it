"""
Requests wrapper with retries, per-domain throttling and adaptive backoff.
"""
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from utils.ratelimit import parse_retry_after, penalize, throttle

RETRY_STATUSES = (429, 500, 502, 503, 504)


def get_with_retries(url: str, retries: int = 3, backoff_factor: float = 0.3, **kwargs):
    """
    Execute an HTTP GET request with automatic backoff and retries.

    When the running job has a ``rate_limit`` block, every attempt waits for a
    per-domain request slot, and a 429/503 backs the whole domain off (honouring
    ``Retry-After``) for every job, not just this one.

    Args:
        url (str): The URL to fetch.
        retries (int): Number of retries after the first attempt, for connection
            errors and 429/500/502/503/504 responses.
        backoff_factor (float): Exponential backoff multiplier between retries.
        **kwargs: Additional arguments passed to requests.get().

    Returns:
        requests.Response: Successful (or non-retryable) response object.

    Raises:
        requests.exceptions.RetryError: If the status is still retryable after
            all retries.
        utils.ratelimit.RobotsDisallowed: If the job set ``respect_robots`` and
            robots.txt forbids the URL.
    """
    session = requests.Session()

    # Connection-level retries only; HTTP status retries are handled below so
    # each one goes through the limiter.
    adapter = HTTPAdapter(max_retries=Retry(
        total=retries, backoff_factor=backoff_factor, allowed_methods=["GET"],
    ))
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    for attempt in range(retries + 1):
        throttle(url)  # no-op unless the running job configured rate_limit
        response = session.get(url, **kwargs)
        if response.status_code not in RETRY_STATUSES:
            return response

        backed_off = penalize(url, response.status_code, response.headers)
        if attempt == retries:
            break
        if backed_off is None:
            # No shared backoff recorded: wait locally (Retry-After if sent)
            delay = parse_retry_after(response.headers)
            time.sleep(backoff_factor * (2 ** attempt) if delay is None else delay)

    raise requests.exceptions.RetryError(
        f"GET {url} still returning {response.status_code} after {retries} retries"
    )
