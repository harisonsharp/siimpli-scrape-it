"""
ScrapMonster All-Metals Price History Scraper
=============================================

Crawls every metal listed in the "Base Metal Prices", "Minor Metal Prices" and
"Rare Earth Prices" link groups at the bottom of any ScrapMonster metal page,
visits every commodity in each metal's "<Metal> Prices" table, and pulls the
full price history of each commodity market, the same way the
``scrapmonster_copper_cathode`` job does:

  1. GET the start page and read the three link groups (metal name + URL).
  2. GET each metal page and read the links in its "Commodity" column.
  3. GET each commodity page; each market tab has a "View Prices" button
     ``metalshistoricalprice('<tab>','<country>','<item>')``.
  4. POST ``/metalprice/gethistoricaldata`` per market with a start date far in
     the past, which returns every price the site holds (earliest to now).

Output is tidy / long, one row per commodity, market and date:

    group      — "base_metals" | "minor_metals" | "rare_earths"
    metal      — slug of the metal page, e.g. ``copper``
    commodity  — slug from the commodity URL, e.g. ``copper_cathode_9999_min_price``
    item_id    — numeric ScrapMonster item id
    region     — market tab label, e.g. ``Delivered China``
    unit       — unit as shown by the site, e.g. ``USD/MT``
    date       — ISO date (YYYY-MM-DD)
    price      — float

Suggested job key: ``[item_id, region, date]``.

Parameters (job YAML ``parameters``):
    start_url   page holding the link groups   (default: the copper page)
    groups      which groups to crawl          (default: all three; names such as
                "Base Metal Prices" or slugs such as "base_metals")
    metals      only these metals, by slug     (default: all)
    start_date  first date to request          (default: 1990-01-01)
    end_date    last date to request           (default: 2099-12-31)
    delay_min/max  seconds between requests when the job has no ``rate_limit``

A page or market that fails yields a marker row (``{"status": "error", ...}``)
which a job's ``validation.required_fields`` rejects, so a partial crawl never
replaces the last good export.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from scrapers.scrap_monster_prices import (
    _ORIGIN,
    _Pacer,
    _USER_AGENT,
    _commodity_slug_from_url,
    _slugify,
)
from utils.ratelimit import penalize, throttle

logger = logging.getLogger(__name__)

_HISTORY_URL = f"{_ORIGIN}/metalprice/gethistoricaldata"
_DEFAULT_START_URL = f"{_ORIGIN}/metal-prices/base-metals/copper/869"

GROUPS = {
    "Base Metal Prices": "base_metals",
    "Minor Metal Prices": "minor_metals",
    "Rare Earth Prices": "rare_earths",
}

# metalshistoricalprice('1','146','872')  ->  tab number, country (market) id, item id
_HISTORY_BUTTON_RE = re.compile(
    r"metalshistoricalprice\('(\d+)','(\d+)','(\d+)'\)"
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_groups(html: str, page_url: str) -> Dict[str, List[Tuple[str, str]]]:
    """Return ``{group_slug: [(metal_slug, metal_url), ...]}`` from the link groups."""
    soup = BeautifulSoup(html, "html.parser")
    found: Dict[str, List[Tuple[str, str]]] = {}
    for label in soup.select("a.seclabel"):
        group = GROUPS.get(label.get_text(strip=True))
        if not group:
            continue
        metals: List[Tuple[str, str]] = []
        for sib in label.find_next_siblings():
            if sib.name != "a":
                continue
            if "seclabel" in (sib.get("class") or []):
                break
            href = sib.get("href")
            if href:
                url = urljoin(page_url, href)
                # .../base-metals/copper/869  ->  "copper"
                metals.append((_slugify(url.rstrip("/").split("/")[-2]), url))
        found[group] = metals
    return found


def _parse_commodity_links(html: str, page_url: str) -> List[str]:
    """Commodity page URLs from the "Commodity" column of a metal page's price table."""
    soup = BeautifulSoup(html, "html.parser")
    urls: List[str] = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if "Commodity" not in headers:
            continue
        for a in table.select("td a[href]"):
            url = urljoin(page_url, a["href"])
            if url not in urls:
                urls.append(url)
    return urls


def _parse_markets(html: str) -> List[Dict[str, str]]:
    """Each market of a commodity page: ``{region, country, item_id}``."""
    soup = BeautifulSoup(html, "html.parser")
    labels: Dict[str, str] = {}
    for a in soup.select('a[href^="#scrappricetabs-"]'):
        labels[a["href"].rsplit("-", 1)[1]] = a.get_text(strip=True)
    markets, seen = [], set()
    for tab, country, item in _HISTORY_BUTTON_RE.findall(html):
        if country in seen:
            continue
        seen.add(country)
        markets.append({"region": labels.get(tab, f"region_{country}"),
                        "country": country, "item_id": item})
    return markets


def _parse_history(html: str) -> List[Tuple[str, float, Optional[str]]]:
    """``(date, price, unit)`` from the history table rows returned by the site."""
    soup = BeautifulSoup(html, "html.parser")
    points = []
    for tr in soup.select("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 2:
            continue
        try:
            price = float(cells[1].replace(",", ""))
        except ValueError:
            continue
        points.append((cells[0], price, cells[2] if len(cells) > 2 else None))
    return points


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


class _Fetcher:
    def __init__(self, pacer: _Pacer):
        self.pacer = pacer
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _USER_AGENT})

    def _send(self, method: str, url: str, **kwargs) -> str:
        self.pacer.wait()
        throttle(url)
        logger.info("%s %s", method, url)
        response = self.session.request(method, url, timeout=30, **kwargs)
        penalize(url, response.status_code, response.headers)
        response.raise_for_status()
        return response.text

    def get(self, url: str) -> str:
        return self._send("GET", url)

    def history(self, item_id: str, country: str, start: str, end: str, referer: str) -> str:
        return self._send(
            "POST", _HISTORY_URL,
            data={"itemid": item_id, "country": country, "sdate": start, "edate": end},
            headers={"Referer": referer, "Origin": _ORIGIN, "X-Requested-With": "XMLHttpRequest"},
        )


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


def _scrape_commodity(fetcher: _Fetcher, url: str, base: Dict[str, Any],
                      start: str, end: str) -> List[Dict[str, Any]]:
    commodity = _commodity_slug_from_url(url)
    markets = _parse_markets(fetcher.get(url))
    if not markets:
        return [{"status": "no_data", "commodity": commodity, "url": url}]
    rows: List[Dict[str, Any]] = []
    for market in markets:
        points = _parse_history(
            fetcher.history(market["item_id"], market["country"], start, end, url))
        if not points:
            logger.warning("%s / %s: no history returned", commodity, market["region"])
            continue
        logger.info("%s / %s: %d points", commodity, market["region"], len(points))
        rows.extend({**base, "commodity": commodity, "item_id": market["item_id"],
                     "region": market["region"], "unit": unit, "date": d, "price": p}
                    for d, p, unit in points)
    return rows or [{"status": "no_data", "commodity": commodity, "url": url}]


def _selected_groups(params: Dict[str, Any]) -> List[str]:
    wanted = params.get("groups")
    if not wanted:
        return list(GROUPS.values())
    slugs = {_slugify(name): slug for name, slug in GROUPS.items()}
    slugs.update({slug: slug for slug in GROUPS.values()})
    unknown = [g for g in wanted if _slugify(str(g)) not in slugs]
    if unknown:
        raise ValueError(f"Unknown groups {unknown}; choose from {list(GROUPS)}")
    return [slugs[_slugify(str(g))] for g in wanted]


def run(params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Return tidy price-history rows for every commodity of every selected metal."""
    params = params or {}
    start_url = params.get("start_url", _DEFAULT_START_URL)
    start = str(params.get("start_date", "1990-01-01"))
    end = str(params.get("end_date", "2099-12-31"))
    only_metals = {_slugify(str(m)) for m in params.get("metals") or []}
    groups = _selected_groups(params)
    fetcher = _Fetcher(_Pacer(float(params.get("delay_min", 2.0)),
                              float(params.get("delay_max", 5.0))))

    found = _parse_groups(fetcher.get(start_url), start_url)
    missing = [g for g in groups if not found.get(g)]
    if missing:
        raise RuntimeError(f"No metal links found for {missing} on {start_url}; has the page changed?")

    results: List[Dict[str, Any]] = []
    for group in groups:
        for metal, metal_url in found[group]:
            if only_metals and metal not in only_metals:
                continue
            logger.info("[%s] %s", group, metal)
            try:
                commodity_urls = _parse_commodity_links(fetcher.get(metal_url), metal_url)
            except Exception as exc:
                logger.error("Failed to read metal page %s: %s", metal_url, exc)
                results.append({"status": "error", "metal": metal, "url": metal_url,
                                "reason": str(exc)})
                continue
            if not commodity_urls:
                logger.warning("%s: no commodities found", metal)
                results.append({"status": "no_data", "metal": metal, "url": metal_url})
                continue
            for url in commodity_urls:
                try:
                    results.extend(_scrape_commodity(
                        fetcher, url, {"group": group, "metal": metal}, start, end))
                except Exception as exc:
                    logger.error("Failed to scrape %s: %s", url, exc)
                    results.append({"status": "error", "metal": metal, "url": url,
                                    "reason": str(exc)})
    logger.info("ScrapMonster all-metals scrape complete — %d rows", len(results))
    return results
