"""
ScrapMonster Metal Price Chart Scraper
=======================================

Scrapes historical price time-series data from ScrapMonster commodity pages.

The site now embeds chart data directly in the page as a Google Charts
``arrayToDataTable(...)`` block, so no secondary AJAX call is required.

Single-phase process per commodity URL:

  Fetch the page (via requests or a Playwright browser) and parse the
  ``arrayToDataTable([...])`` block that is rendered into the page HTML.
  The current price unit is extracted from the ``td.metalltp`` element.

Legacy fallback:

  If no ``arrayToDataTable`` block is found (e.g. older page style), the
  scraper falls back to the original two-phase AJAX approach: extract
  ``drawchart(itemid, region, section, range)`` onclick params, then POST
  to ``/metalprice/getchart``.

Returns rows suitable for CSV storage — one row per data point:
    date                     — ISO date string (YYYY-MM-DD)
    price_{commodity}_{unit} — numeric price value

Example output column:  ``price_copper_usd_per_mt``

Usage (via job runner):
    script: scrapers.scrap_monster_prices

Usage (direct):
    python -c "
    from scrapers.scrap_monster_prices import run
    run({'urls': ['https://www.scrapmonster.com/metal-prices/copper/869']})
    "
"""

import logging
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from utils.browser import get_browser_page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GETCHART_URL = "https://www.scrapmonster.com/metalprice/getchart"
_SCRAPMONSTER_ORIGIN = "https://www.scrapmonster.com"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) "
    "Gecko/20100101 Firefox/148.0"
)

# Legacy: drawchart('872','146','324','0')  →  itemid, region, section, range
_DRAWCHART_RE = re.compile(
    r"drawchart\('(\d+)','(\d+)','(\d+)','(\d+)'\)"
)

# Full arrayToDataTable([...]) block — captured group is everything inside []
_DATATABLE_RE = re.compile(
    r"arrayToDataTable\s*\(\s*\[(.*?)\]\s*\)",
    re.DOTALL,
)

# Individual data point: ['2024-03-18',9620.28]
_DATAPOINT_RE = re.compile(
    r"\['(\d{4}-\d{2}-\d{2})',\s*(-?\d+(?:\.\d+)?)\]"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Lowercase alphanumeric slug with underscores."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _slugify_unit(unit: str) -> str:
    """
    Convert a price unit string to a safe column-name slug.

    Examples:
        "$US/MT"  →  "usd_per_mt"
        "USD/t"   →  "usd_per_t"
        "¢/lb"    →  "c_per_lb"
    """
    unit = unit.replace("$US", "usd").replace("$", "usd").replace("¢", "c")
    unit = unit.replace("/", "_per_")
    return re.sub(r"[^a-z0-9_]", "", unit.lower()).strip("_")


def _commodity_slug_from_url(url: str) -> str:
    """
    Derive a commodity slug from the page URL path.

    https://www.scrapmonster.com/metal-prices/antimony-ingot-9965-min-price/655
    →  "antimony_ingot_9965_min_price"
    """
    path = urlparse(url).path.rstrip("/")
    parts = [p for p in path.split("/") if p]
    # The commodity slug is the second-to-last segment (before the numeric ID).
    if len(parts) >= 2 and parts[-1].isdigit():
        slug = parts[-2]
    elif parts:
        slug = parts[-1]
    else:
        slug = "unknown"
    return _slugify(slug)


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT})
    return session


def _fetch_page_html(session: requests.Session, page_url: str) -> str:
    """Fetch the commodity page HTML via plain HTTP requests."""
    logger.info("Fetching commodity page: %s", page_url)
    response = session.get(page_url, timeout=30)
    response.raise_for_status()
    return response.text


def _fetch_page_html_browser(page_url: str) -> str:
    """
    Fetch the fully rendered commodity page HTML via a visible Chromium window.

    Loads the page, clicks the "All" time-range button to request full history,
    then captures the page HTML after the chart data settles.
    """
    logger.info("Opening visible browser for: %s", page_url)
    with get_browser_page(headless=False) as page:
        page.goto(page_url, wait_until="load")
        logger.info("Page loaded, waiting for chart UI to render...")

        # Wait for range buttons to appear (they contain the "All" button).
        try:
            page.wait_for_selector("[onclick*='drawchart']", timeout=10_000)
        except Exception:
            logger.warning("Timed out waiting for chart range buttons")

        # Click the "All" button to request the full historical data range.
        clicked = False
        try:
            all_btn = page.get_by_role("link", name=re.compile(r"^All$", re.IGNORECASE)).first
            if all_btn.is_visible():
                logger.info("Clicking 'All' range button...")
                all_btn.click()
                clicked = True
        except Exception:
            logger.warning("Could not find 'All' link by role")

        if not clicked:
            # Fallback: try clicking any button/link with onclick containing range 0
            try:
                page.locator("[onclick*=\",'0')\"]").first.click()
                logger.info("Clicked range button via fallback selector")
                clicked = True
            except Exception:
                logger.warning("Fallback click also failed")

        # Wait for the chart data to update after clicking (if it updated dynamically).
        page.wait_for_timeout(3000)
        html = page.content()

    return html


def _legacy_fetch_chart_html(
    session: requests.Session,
    page_url: str,
    page_html: str,
) -> Optional[str]:
    """
    Legacy fallback: if the page still uses drawchart() onclick buttons,
    extract the chart params and POST to the AJAX endpoint.

    Returns the AJAX response HTML, or None if no drawchart() params found.
    """
    matches = _DRAWCHART_RE.findall(page_html)
    if not matches:
        return None

    itemid, region, section, _ = matches[0]
    logger.debug(
        "Legacy chart params — itemid=%s  region=%s  section=%s",
        itemid, region, section,
    )

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": page_url,
        "Origin": _SCRAPMONSTER_ORIGIN,
        "X-Requested-With": "XMLHttpRequest",
    }
    payload = {"itemid": itemid, "region": region, "range": "0", "section": section}
    logger.info("Legacy: POSTing to chart API (itemid=%s, section=%s)", itemid, section)
    response = session.post(_GETCHART_URL, data=payload, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def _parse_chart_data(html: str) -> Tuple[List[Tuple[str, float]], Optional[str]]:
    """
    Parse ``arrayToDataTable(...)`` data points and price unit from HTML.

    Works on both the full commodity page and the legacy AJAX response.

    Returns:
        data_points — list of (date_str, price_float) tuples
        unit        — price unit string (e.g. "$US/MT"), or None if not found
    """
    soup = BeautifulSoup(html, "html.parser")

    datatable_match = _DATATABLE_RE.search(html)
    if not datatable_match:
        logger.warning("arrayToDataTable not found in HTML")
        return [], None

    table_content = datatable_match.group(1)
    data_points = [
        (date, float(price))
        for date, price in _DATAPOINT_RE.findall(table_content)
    ]
    logger.debug("Parsed %d data points", len(data_points))

    unit: Optional[str] = None
    price_cell = soup.find("td", class_="metalltp")
    if price_cell:
        span = price_cell.find("span")
        if span:
            unit = span.get_text(strip=True)

    return data_points, unit


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Scrape historical price chart data from one or more ScrapMonster
    commodity pages.

    Parameters (via job YAML ``parameters`` block):

    ============  ===========  ================================================
    Key           Default      Description
    ============  ===========  ================================================
    urls          required     List of ScrapMonster commodity page URLs.
    url           —            Single URL shorthand (used if ``urls`` absent).
    delay_min     2.0          Minimum polite delay between page requests (s).
    delay_max     5.0          Maximum polite delay between page requests (s).
    headless      true         Set to false to open a visible Chromium window.
                               Useful when the page uses heavy JS rendering.
    ============  ===========  ================================================

    Returns:
        List[Dict] — one entry per data point, shaped for CSV storage:
        ``{"date": "YYYY-MM-DD", "price_{commodity}_{unit}": float}``
    """
    params = params or {}

    urls: List[str] = params.get("urls") or (
        [params["url"]] if "url" in params else []
    )
    if not urls:
        logger.error("No URLs provided. Set 'urls' or 'url' in parameters.")
        return [{"status": "error", "reason": "no_urls_provided"}]

    delay_min = float(params.get("delay_min", 2.0))
    delay_max = float(params.get("delay_max", 5.0))
    headless = bool(params.get("headless", True))

    if not headless:
        logger.info("Running in visible-browser mode (headless=False)")

    results: List[Dict[str, Any]] = []
    session = _build_session()

    for i, page_url in enumerate(urls):
        if i > 0:
            delay = random.uniform(delay_min, delay_max)
            logger.debug("Polite delay: %.1fs", delay)
            time.sleep(delay)

        commodity_slug = _commodity_slug_from_url(page_url)
        logger.info("Processing commodity: %s (%s)", commodity_slug, page_url)

        try:
            if headless:
                page_html = _fetch_page_html(session, page_url)
            else:
                page_html = _fetch_page_html_browser(page_url)

            # Primary: parse arrayToDataTable embedded directly in the page.
            data_points, unit = _parse_chart_data(page_html)

            # Legacy fallback: if data not in page, try the old AJAX endpoint.
            if not data_points:
                logger.info(
                    "No inline chart data found — trying legacy AJAX fallback"
                )
                delay = random.uniform(delay_min, delay_max)
                time.sleep(delay)
                ajax_html = _legacy_fetch_chart_html(session, page_url, page_html)
                if ajax_html:
                    data_points, unit = _parse_chart_data(ajax_html)

            if not data_points:
                logger.warning("No data points found for %s", commodity_slug)
                results.append({
                    "status": "no_data",
                    "commodity": commodity_slug,
                    "url": page_url,
                })
                continue

            unit_slug = _slugify_unit(unit) if unit else "price"
            price_col = f"price_{commodity_slug}_{unit_slug}"
            logger.info(
                "  %s: %d points, column=%s, unit=%s",
                commodity_slug, len(data_points), price_col, unit,
            )

            for date, price in data_points:
                results.append({"date": date, price_col: price})

        except Exception as exc:
            logger.error("Failed to scrape %s: %s", page_url, exc)
            results.append({
                "status": "error",
                "commodity": commodity_slug,
                "url": page_url,
                "reason": str(exc),
            })

    logger.info("ScrapMonster scrape complete — %d rows total", len(results))
    return results
