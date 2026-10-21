"""
Westmetall LME Base Metal Price Scraper
=======================================

Scrapes the daily official LME prices that westmetall.com republishes for the
six LME base metals (copper, tin, lead, zinc, aluminium, nickel). Each metal
has one table page per year, back to 2008:

    /en/markdaten.php?action=table&field=LME_Cu_cash            current year
    /en/markdaten.php?action=table&field=LME_Cu_cash&year=2015  any other year

A table has the columns date, "LME Copper Cash-Settlement", "LME Copper
3-month" and "LME Copper stock". The two price columns become two markets; the
stock column (tonnes, not a price) is not kept.

  1. GET the market data overview and check that the LME prices are still
     quoted in US dollars per ton, since the table pages do not say.
  2. Per metal, GET the current-year table, read the year links on it, then
     GET every other year.

Output is tidy / long, one row per metal, market and date:

    metal      — ``copper`` | ``tin`` | ``lead`` | ``zinc`` | ``aluminium`` | ``nickel``
    commodity  — e.g. ``LME Copper``
    market     — ``Cash-Settlement`` | ``3-month``
    unit       — ``USD/MT``
    date       — ISO date (YYYY-MM-DD)
    price      — float

Suggested job key: ``[commodity, market, date]``. The commodity and market
labels are fixed here rather than read from the page, because the server
builds series codes from them: a reworded header must fail the run, not
start new series.

Parameters (job YAML ``parameters``):
    metals         only these metals, by slug     (default: all six)
    from_year      first year to fetch            (default: every listed year)
    delay_min/max  seconds between requests when the job has no ``rate_limit``

A page that fails yields a marker row (``{"status": "error", ...}``) which a
job's ``validation.required_fields`` rejects, so a partial scrape never
replaces the last good export.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from scrapers.scrap_monster_prices import _Pacer, _USER_AGENT
from utils.http import get_with_retries

logger = logging.getLogger(__name__)

_ORIGIN = "https://www.westmetall.com"
_OVERVIEW_URL = f"{_ORIGIN}/en/markdaten.php"

# metal slug -> (Westmetall field, commodity label)
METALS = {
    "copper": ("LME_Cu_cash", "LME Copper"),
    "tin": ("LME_Sn_cash", "LME Tin"),
    "lead": ("LME_Pb_cash", "LME Lead"),
    "zinc": ("LME_Zn_cash", "LME Zinc"),
    "aluminium": ("LME_Al_cash", "LME Aluminium"),
    "nickel": ("LME_Ni_cash", "LME Nickel"),
}

# Price columns of a table, after the date column, with the header ending
# each must have.
MARKETS = ("Cash-Settlement", "3-month")
UNIT = "USD/MT"

_MONTHS = {name: i for i, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

# "02. January 2008"
_DATE_RE = re.compile(r"^(\d{1,2})\. ([A-Za-z]+) (\d{4})$")
_YEAR_LINK_RE = re.compile(r"[?&]year=(\d{4})")


def table_url(field: str, year: Optional[int] = None) -> str:
    url = f"{_OVERVIEW_URL}?action=table&field={field}"
    return f"{url}&year={year}" if year is not None else url


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_date(text: str) -> str:
    """``"02. January 2008"`` -> ``"2008-01-02"``. Raises ValueError otherwise."""
    match = _DATE_RE.match(text.strip())
    if not match or match.group(2) not in _MONTHS:
        raise ValueError(f"unexpected date {text!r}")
    day, month, year = int(match.group(1)), _MONTHS[match.group(2)], int(match.group(3))
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_price(text: str) -> Optional[float]:
    """``"14,540.00"`` -> 14540.0; ``"-"`` or blank (no price that day) -> None."""
    text = text.strip()
    if text in ("", "-"):
        return None
    return float(text.replace(",", ""))


def parse_years(html: str, field: str) -> List[int]:
    """Years linked from a table page for this field, oldest first."""
    soup = BeautifulSoup(html, "html.parser")
    years = set()
    for a in soup.select("a[href]"):
        href = a["href"]
        if "action=table" in href and f"field={field}" in href:
            match = _YEAR_LINK_RE.search(href)
            if match:
                years.add(int(match.group(1)))
    return sorted(years)


def parse_table(html: str, commodity: str) -> List[Tuple[str, str, float]]:
    """``(date, market, price)`` for each price cell of a table page.

    Raises ValueError if the page has no price table or its headers are not
    "<commodity> Cash-Settlement" and "<commodity> 3-month".
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ValueError("no price table on the page")
    headers = [th.get_text(strip=True) for th in table.find("tr").find_all("th")]
    expected = [f"{commodity} {m}" for m in MARKETS]
    if headers[1:3] != expected:
        raise ValueError(f"table headers {headers} do not start with date, {expected}")

    points: List[Tuple[str, str, float]] = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 3:
            continue  # the header row, which the page repeats every month
        date = parse_date(cells[0])
        for market, cell in zip(MARKETS, cells[1:3]):
            price = parse_price(cell)
            if price is not None:
                points.append((date, market, price))
    return points


def check_overview(html: str) -> None:
    """Raises ValueError unless the overview still quotes LME prices in USD per ton."""
    text = " ".join(BeautifulSoup(html, "html.parser").get_text(" ").split())
    if "Official LME-Prices in US Dollar" not in text or "in US Dollar per ton" not in text:
        raise ValueError("the market data page no longer says the LME prices are in US Dollar per ton")


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


class _Fetcher:
    def __init__(self, pacer: _Pacer):
        self.pacer = pacer

    def get(self, url: str) -> str:
        self.pacer.wait()
        logger.info("GET %s", url)
        response = get_with_retries(url, headers={"User-Agent": _USER_AGENT}, timeout=30)
        response.raise_for_status()
        return response.text


def _rows(metal: str, commodity: str, points) -> List[Dict[str, Any]]:
    return [{"metal": metal, "commodity": commodity, "market": market, "unit": UNIT,
             "date": date, "price": price} for date, market, price in points]


def _scrape_metal(fetcher: _Fetcher, metal: str, from_year: Optional[int]) -> List[Dict[str, Any]]:
    field, commodity = METALS[metal]
    current_url = table_url(field)
    current_html = fetcher.get(current_url)
    years = parse_years(current_html, field)
    if not years:
        raise ValueError(f"no year links on {current_url}")

    points = parse_table(current_html, commodity)
    # The page without a year holds the newest year, which is also linked.
    done = {int(date[:4]) for date, _, _ in points}
    from_year = from_year or 0
    rows = _rows(metal, commodity, [p for p in points if int(p[0][:4]) >= from_year])
    for year in years:
        if year in done or year < from_year:
            continue
        url = table_url(field, year)
        try:
            year_points = parse_table(fetcher.get(url), commodity)
        except Exception as exc:
            logger.error("Failed to scrape %s: %s", url, exc)
            rows.append({"status": "error", "metal": metal, "url": url, "reason": str(exc)})
            continue
        if not year_points:
            logger.warning("%s %d: no prices", commodity, year)
        rows.extend(_rows(metal, commodity, year_points))
    logger.info("%s: %d rows from %d years", commodity, len(rows), len(years))
    return rows


def run(params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Returns tidy rows ``{metal, commodity, market, unit, date, price}``."""
    params = params or {}
    metals = [str(m).lower() for m in (params.get("metals") or METALS)]
    unknown = [m for m in metals if m not in METALS]
    if unknown:
        raise ValueError(f"unknown metals {unknown}; Westmetall has {list(METALS)}")
    from_year = int(params["from_year"]) if params.get("from_year") else None

    fetcher = _Fetcher(_Pacer(float(params.get("delay_min", 2.0)),
                              float(params.get("delay_max", 5.0))))
    check_overview(fetcher.get(_OVERVIEW_URL))

    rows: List[Dict[str, Any]] = []
    for metal in metals:
        try:
            rows.extend(_scrape_metal(fetcher, metal, from_year))
        except Exception as exc:
            logger.error("Failed to scrape %s: %s", metal, exc)
            rows.append({"status": "error", "metal": metal, "reason": str(exc)})

    logger.info("Westmetall scrape complete: %d rows", len(rows))
    return rows
