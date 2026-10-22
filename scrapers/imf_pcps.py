"""
IMF Primary Commodity Prices (PCPS)
===================================

Fetches monthly commodity prices from the IMF's Primary Commodity Price
System through its SDMX 3.0 API, as CSV. No key is needed. The IMF updates
PCPS early each month with the previous month's averages, back to 1992:

    https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.RES/PCPS/+/G001.PPALLA.USD.M
    (Accept: application/vnd.sdmx.data+csv;version=2.0.0)

The series key is ``<area>.<indicator>.<transformation>.<frequency>``:
G001 (world), the commodity's indicator, USD (the price, not an index) and M
(monthly). The price is the average of the month's daily prices. The IMF's
technical note gives the precious metals per troy ounce; the API does not
repeat the unit, so it is fixed here per indicator.

Siimpli's job 010 already loads PCPS gold, silver and platinum on its own.
This scraper exists for indicators that job does not fetch, palladium first.

  Per indicator, one request for the whole series. Every CSV record must be
  that series (indicator, G001, USD, M) at scale 0, or the run fails rather
  than storing prices scaled by 10^n or for the wrong commodity.

Output is tidy / long, one row per metal and month:

    metal      — e.g. ``palladium``
    commodity  — e.g. ``Palladium``
    market     — ``Monthly average``
    unit       — e.g. ``USD/oz`` (troy ounce)
    date       — ISO date of the month's 1st (YYYY-MM-01)
    price      — float

Suggested job key: ``[commodity, market, date]``. The labels are fixed here
because the server builds series codes from them.

Parameters (job YAML ``parameters``):
    metals         only these metals, by slug     (default: all of INDICATORS)
    start_period   first month to fetch, YYYY-MM   (default: the whole series)
    delay_min/max  seconds between requests when the job has no ``rate_limit``

A series that fails yields a marker row (``{"status": "error", ...}``) which a
job's ``validation.required_fields`` rejects, so a partial fetch never
replaces the last good export.
"""

import csv
import io
import logging
import re
from typing import Any, Dict, List, Optional

from scrapers.scrap_monster_prices import _Pacer, _USER_AGENT
from utils.http import get_with_retries

logger = logging.getLogger(__name__)

_API = "https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.RES/PCPS/+"
_ACCEPT = "application/vnd.sdmx.data+csv;version=2.0.0"

# metal slug -> (PCPS indicator, commodity label, unit)
INDICATORS = {
    "palladium": ("PPALLA", "Palladium", "USD/oz"),
}
AREA = "G001"
TRANSFORMATION = "USD"
FREQUENCY = "M"
MARKET = "Monthly average"

_PERIOD_RE = re.compile(r"^(\d{4})-M(\d{2})$")


def series_url(indicator: str, start_period: Optional[str] = None) -> str:
    url = f"{_API}/{AREA}.{indicator}.{TRANSFORMATION}.{FREQUENCY}"
    # c[TIME_PERIOD]=ge:YYYY-MM, URL-encoded
    return f"{url}?c%5BTIME_PERIOD%5D=ge:{start_period}" if start_period else url


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_period(period: str) -> str:
    """``"2026-M09"`` -> ``"2026-09-01"``. Raises ValueError otherwise."""
    match = _PERIOD_RE.match(period.strip())
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise ValueError(f"unexpected period {period!r}")
    return f"{match.group(1)}-{match.group(2)}-01"


def parse_series(text: str, metal: str) -> List[Dict[str, Any]]:
    """Rows of one series' CSV. Raises ValueError if a record belongs to
    another series or carries a scale other than 0."""
    indicator, commodity, unit = INDICATORS[metal]
    expected = (AREA, indicator, TRANSFORMATION, FREQUENCY)
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        got = (rec.get("COUNTRY"), rec.get("INDICATOR"), rec.get("DATA_TRANSFORMATION"), rec.get("FREQUENCY"))
        if got != expected:
            raise ValueError(f"unexpected series {'.'.join(map(str, got))} in the {indicator} response")
        if (rec.get("SCALE") or "0") != "0":
            raise ValueError(f"{indicator} is published at scale {rec.get('SCALE')!r}, not 0")
        value = (rec.get("OBS_VALUE") or "").strip()
        if value.lower() in ("", "nan"):
            continue
        rows.append({"metal": metal, "commodity": commodity, "market": MARKET, "unit": unit,
                     "date": parse_period(rec["TIME_PERIOD"]), "price": float(value)})
    return rows


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


class _Fetcher:
    def __init__(self, pacer: _Pacer):
        self.pacer = pacer

    def get(self, url: str) -> str:
        self.pacer.wait()
        logger.info("GET %s", url)
        response = get_with_retries(url, headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT},
                                    timeout=60)
        response.raise_for_status()
        return response.text


def run(params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Returns tidy rows ``{metal, commodity, market, unit, date, price}``."""
    params = params or {}
    metals = [str(m).lower() for m in (params.get("metals") or INDICATORS)]
    unknown = [m for m in metals if m not in INDICATORS]
    if unknown:
        raise ValueError(f"unknown metals {unknown}; this scraper knows {list(INDICATORS)}")
    start_period = params.get("start_period")

    fetcher = _Fetcher(_Pacer(float(params.get("delay_min", 1.0)),
                              float(params.get("delay_max", 2.0))))
    rows: List[Dict[str, Any]] = []
    for metal in metals:
        url = series_url(INDICATORS[metal][0], start_period)
        try:
            series = parse_series(fetcher.get(url), metal)
            if not series:
                raise ValueError("no prices in the response")
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", url, exc)
            rows.append({"status": "error", "metal": metal, "url": url, "reason": str(exc)})
            continue
        dates = [r["date"] for r in series]
        logger.info("%s: %d months, %s to %s", metal, len(series), min(dates), max(dates))
        rows.extend(series)

    logger.info("IMF PCPS fetch complete: %d rows", len(rows))
    return rows
