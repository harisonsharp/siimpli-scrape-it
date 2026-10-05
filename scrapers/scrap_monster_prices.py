"""
ScrapMonster Metal Price Scraper
================================

Scrapes the full historical price series for every market ("region") listed on
a ScrapMonster commodity page, e.g. EXW China, FOB China, Port India, ...

A commodity page shows one chart per region tab, but only the last few points.
The page's own "All" button loads the complete history with a POST to
``/metalprice/chartdata`` (itemid, region, range=0, container).  This scraper
does the same call once per region, so no browser clicking is involved:

  1. GET the commodity page; read the region tabs, each region's unit, and the
     parameters of its "All" range button (``redrawmetalschart(...)``).
  2. POST ``chartdata`` with ``range=0`` for each region and parse the
     ``arrayToDataTable([...])`` block in the response.

If a page has no range buttons, the partial history embedded in the page is
returned instead (region ``default``) and a warning is logged.

Output is tidy / long: one row per region per date, so one job can hold many
regions and commodities:

    commodity  — slug from the URL, e.g. ``antimony_ingot_9965_min_price``
    region     — slug of the tab label, e.g. ``exw_china``
    unit       — price unit slug, e.g. ``usd_per_mt`` (``unknown`` if absent)
    date       — ISO date (YYYY-MM-DD)
    price      — float

Suggested job keys: ``output.key: [commodity, region, date]``.

Parameters (job YAML ``parameters``):
    urls / url     commodity page URL(s)                       (required)
    regions        only these regions: tab labels or region ids (default: all)
    browser        true = fetch through a visible browser (use if the site
                   starts blocking plain HTTP; solve the CAPTCHA when asked)
    delay_min/max  seconds between requests when the job has no ``rate_limit``
                   block (otherwise the job's per-domain limiter decides)

Usage (via job runner):
    script: scrapers.scrap_monster_prices

Usage (direct):
    python -c "
    from scrapers.scrap_monster_prices import run
    run({'url': 'https://www.scrapmonster.com/metal-prices/copper/869'})
    "
"""

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from utils.browser import captcha_gate
from utils.ratelimit import limiter_active, penalize, throttle

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORIGIN = "https://www.scrapmonster.com"
_CHARTDATA_URL = f"{_ORIGIN}/metalprice/chartdata"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) "
    "Gecko/20100101 Firefox/148.0"
)

# The "All" range button of each chart:
#   redrawmetalschart('5_graphcontainer','0','170','655')  →  container, region, item
_REDRAW_ALL_RE = re.compile(
    r"redrawmetalschart\('(\w+)','0','(\d+)','(\d+)'\)"
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
    Convert a price unit string to a safe slug.

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


@dataclass
class Region:
    """One market tab of a commodity page."""
    label: str
    region_id: str
    item_id: str
    container: str
    unit: Optional[str]


def _parse_regions(html: str) -> List[Region]:
    """Find each region's tab label, unit and "All"-range parameters on a commodity page."""
    soup = BeautifulSoup(html, "html.parser")

    labels: Dict[int, str] = {}
    for a in soup.select('a[href^="#scrappricetabs-"]'):
        labels[int(a["href"].rsplit("-", 1)[1])] = a.get_text(strip=True)

    regions: List[Region] = []
    seen = set()
    for container, region_id, item_id in _REDRAW_ALL_RE.findall(html):
        if region_id in seen:
            continue
        seen.add(region_id)
        tab = re.match(r"\d+", container)
        tab_no = int(tab.group()) if tab else None
        unit = None
        pane = soup.find(id=f"scrappricetabs-{tab_no}") if tab_no is not None else None
        if pane:
            span = pane.select_one(".scrapitemprice span")
            unit = span.get_text(strip=True) if span else None
        regions.append(Region(
            label=labels.get(tab_no, f"region_{region_id}"),
            region_id=region_id, item_id=item_id, container=container, unit=unit,
        ))
    return regions


def _parse_points(html: str) -> List[tuple]:
    """Parse ``arrayToDataTable`` rows into (date, price) tuples (empty if none)."""
    match = _DATATABLE_RE.search(html)
    if not match:
        return []
    return [(d, float(p)) for d, p in _DATAPOINT_RE.findall(match.group(1))]


def _page_unit(html: str) -> Optional[str]:
    span = BeautifulSoup(html, "html.parser").select_one(".scrapitemprice span")
    return span.get_text(strip=True) if span else None


def _wants_region(region: Region, wanted: List[str]) -> bool:
    return not wanted or region.region_id in wanted or _slugify(region.label) in wanted


# ---------------------------------------------------------------------------
# Fetching (plain HTTP or a visible browser session)
# ---------------------------------------------------------------------------


class _Pacer:
    """Polite pause between requests, used only when the job has no rate_limit block."""

    def __init__(self, delay_min: float, delay_max: float):
        self.delay_min, self.delay_max = delay_min, delay_max
        self._first = True

    def wait(self) -> None:
        if limiter_active():
            return  # the job's per-domain limiter spaces requests (via throttle())
        if not self._first:
            delay = random.uniform(self.delay_min, self.delay_max)
            logger.debug("Polite delay: %.1fs", delay)
            time.sleep(delay)
        self._first = False


def _post_headers(referer: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": referer,
        "Origin": _ORIGIN,
        "X-Requested-With": "XMLHttpRequest",
    }


class _HttpFetcher:
    def __init__(self, pacer: _Pacer):
        self.pacer = pacer
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _USER_AGENT})

    def get(self, url: str) -> str:
        self.pacer.wait()
        throttle(url)
        logger.info("GET %s", url)
        response = self.session.get(url, timeout=30)
        penalize(url, response.status_code, response.headers)
        response.raise_for_status()
        return response.text

    def post_chart(self, payload: Dict[str, str], referer: str) -> str:
        self.pacer.wait()
        throttle(_CHARTDATA_URL)
        logger.info("POST %s %s", _CHARTDATA_URL, payload)
        response = self.session.post(
            _CHARTDATA_URL, data=payload, headers=_post_headers(referer), timeout=30)
        penalize(_CHARTDATA_URL, response.status_code, response.headers)
        response.raise_for_status()
        return response.text


class _BrowserFetcher:
    """Same interface over a Playwright page, sharing its cookies / CAPTCHA clearance."""

    def __init__(self, page, pacer: _Pacer):
        self.page = page
        self.pacer = pacer

    def get(self, url: str) -> str:
        self.pacer.wait()  # navigations are rate limited by the browser context itself
        logger.info("Browser GET %s", url)
        self.page.goto(url, wait_until="load")
        return self.page.content()

    def post_chart(self, payload: Dict[str, str], referer: str) -> str:
        self.pacer.wait()
        throttle(_CHARTDATA_URL)  # an API call, not a navigation: throttle explicitly
        logger.info("Browser POST %s %s", _CHARTDATA_URL, payload)
        response = self.page.request.post(
            _CHARTDATA_URL, form=payload, headers=_post_headers(referer))
        penalize(_CHARTDATA_URL, response.status, response.headers)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status} from {_CHARTDATA_URL}")
        return response.text()


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


def _scrape_commodity(fetcher, page_url: str, wanted: List[str]) -> List[Dict[str, Any]]:
    """Return tidy rows for every (wanted) region of one commodity page."""
    commodity = _commodity_slug_from_url(page_url)
    html = fetcher.get(page_url)
    regions = _parse_regions(html)

    if not regions:
        points = _parse_points(html)
        if not points:
            logger.warning("No chart data found for %s", commodity)
            return [{"status": "no_data", "commodity": commodity, "url": page_url}]
        logger.warning(
            "%s: no range buttons found; returning only the page's inline (partial) history",
            commodity)
        unit = _page_unit(html)
        unit_slug = _slugify_unit(unit) if unit else "unknown"
        return [{"commodity": commodity, "region": "default", "unit": unit_slug,
                 "date": d, "price": p} for d, p in points]

    selected = [r for r in regions if _wants_region(r, wanted)]
    if not selected:
        raise ValueError(
            f"None of the requested regions {wanted} exist for {commodity}; "
            f"available: {[_slugify(r.label) for r in regions]}")

    rows: List[Dict[str, Any]] = []
    for region in selected:
        slug = _slugify(region.label)
        chart_html = fetcher.post_chart(
            {"itemid": region.item_id, "region": region.region_id,
             "range": "0", "container": region.container},
            referer=page_url,
        )
        points = _parse_points(chart_html)
        if not points:
            logger.warning("%s / %s: chart response had no data points", commodity, slug)
            continue
        unit_slug = _slugify_unit(region.unit) if region.unit else "unknown"
        logger.info("%s / %s: %d points (%s)", commodity, slug, len(points), unit_slug)
        rows.extend({"commodity": commodity, "region": slug, "unit": unit_slug,
                     "date": d, "price": p} for d, p in points)

    if not rows:
        return [{"status": "no_data", "commodity": commodity, "url": page_url}]
    return rows


def _use_browser(params: Dict[str, Any]) -> bool:
    """``browser`` param, with the old ``headless`` flag (headless=False meant a visible browser)."""
    if "browser" in params:
        return bool(params["browser"])
    if "headless" in params:
        logger.warning("Parameter 'headless' is deprecated; use 'browser: true' for a visible browser")
        return not params["headless"]
    return False


def run(params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Scrape full price history for every region of one or more commodity pages.

    Returns tidy rows ``{commodity, region, unit, date, price}``.  Pages or
    regions that fail yield marker rows (``{"status": "error" | "no_data", ...}``)
    which a job's ``validation.required_fields`` will reject.
    """
    params = params or {}

    urls: List[str] = params.get("urls") or ([params["url"]] if "url" in params else [])
    if not urls:
        logger.error("No URLs provided. Set 'urls' or 'url' in parameters.")
        return [{"status": "error", "reason": "no_urls_provided"}]

    wanted = [_slugify(str(r)) if not str(r).isdigit() else str(r)
              for r in (params.get("regions") or [])]
    pacer = _Pacer(float(params.get("delay_min", 2.0)), float(params.get("delay_max", 5.0)))

    def scrape_all(fetcher) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for page_url in urls:
            logger.info("Processing %s", page_url)
            try:
                results.extend(_scrape_commodity(fetcher, page_url, wanted))
            except Exception as exc:
                logger.error("Failed to scrape %s: %s", page_url, exc)
                results.append({
                    "status": "error",
                    "commodity": _commodity_slug_from_url(page_url),
                    "url": page_url,
                    "reason": str(exc),
                })
        return results

    if _use_browser(params):
        logger.info("Using a visible browser session")
        with captcha_gate(urls[0]) as page:
            results = scrape_all(_BrowserFetcher(page, pacer))
    else:
        results = scrape_all(_HttpFetcher(pacer))

    logger.info("ScrapMonster scrape complete — %d rows total", len(results))
    return results
