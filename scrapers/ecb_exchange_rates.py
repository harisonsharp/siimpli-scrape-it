"""
ECB Euro Foreign Exchange Reference Rates
=========================================

Fetches the European Central Bank's euro reference exchange rates from the ECB
Data Portal's SDMX REST API (dataset ``EXR``), as CSV. The ECB publishes one
rate per currency each TARGET working day at about 16:00 CET, daily back to
1999-01-04, and the monthly average of those daily rates:

    https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?format=csvdata&detail=dataonly
    https://data-api.ecb.europa.eu/service/data/EXR/M.USD.EUR.SP00.A?format=csvdata&detail=dataonly

Every rate is quoted against the euro: ``1.1215`` for USD means 1 EUR =
1.1215 USD. A cross rate is the ratio of two of them (GBP per 1 USD =
GBP rate / USD rate).

  1. One request with full detail and the last observation of every daily
     series, to check that each rate is still quoted per 1 EUR with no
     multiplier (``UNIT`` is the currency, ``UNIT_MULT`` 0). A change there
     fails the run rather than storing rates scaled by 10^n.
  2. Per currency and frequency, one request for the whole series.

Output is tidy / long, one row per currency, frequency and date:

    currency   — ISO code, e.g. ``USD``
    base       — ``EUR``
    frequency  — ``daily`` | ``monthly``
    unit       — e.g. ``USD per 1 EUR``
    date       — ISO date (YYYY-MM-DD); a monthly average is dated the 1st
    rate       — float

Suggested job key: ``[currency, frequency, date]``. Days without a rate
(TARGET holidays, weekends) have no row. The ECB leaves a few early days
blank; those are skipped too.

Parameters (job YAML ``parameters``):
    currencies     ISO codes                         (default: CURRENCIES)
    frequencies    ``daily`` and/or ``monthly``      (default: both)
    start_period   first date or month to fetch      (default: the whole series)
    delay_min/max  seconds between requests when the job has no ``rate_limit``

A series that fails yields a marker row (``{"status": "error", ...}``) which a
job's ``validation.required_fields`` rejects, so a partial fetch never
replaces the last good export.
"""

import csv
import io
import logging
from typing import Any, Dict, List, Optional

from scrapers.scrap_monster_prices import _Pacer, _USER_AGENT
from utils.http import get_with_retries

logger = logging.getLogger(__name__)

_API = "https://data-api.ecb.europa.eu/service/data/EXR"

# The 29 currencies the ECB publishes a daily reference rate for (October
# 2026). Others in EXR (RUB, and the pre-euro currencies) are discontinued.
CURRENCIES = (
    "USD", "JPY", "CZK", "DKK", "GBP", "HUF", "PLN", "RON", "SEK", "CHF",
    "ISK", "NOK", "TRY", "AUD", "BRL", "CAD", "CNY", "HKD", "IDR", "ILS",
    "INR", "KRW", "MXN", "MYR", "NZD", "PHP", "SGD", "THB", "ZAR",
)

# frequency label -> SDMX frequency code
FREQUENCIES = {"daily": "D", "monthly": "M"}
BASE = "EUR"


def series_url(freq_code: str, currencies: str, **query: Any) -> str:
    """URL of the EXR spot reference rate series ``<freq>.<currencies>.EUR.SP00.A``."""
    params = {"format": "csvdata", **query}
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    return f"{_API}/{freq_code}.{currencies}.{BASE}.SP00.A?{qs}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_period(period: str, frequency: str) -> str:
    """``"2026-10-20"`` (daily) or ``"2026-09"`` (monthly) -> ISO date.
    Raises ValueError otherwise."""
    parts = period.split("-")
    if frequency == "daily" and len(parts) == 3:
        year, month, day = (int(p) for p in parts)
    elif frequency == "monthly" and len(parts) == 2:
        (year, month), day = (int(p) for p in parts), 1
    else:
        raise ValueError(f"unexpected {frequency} period {period!r}")
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_series(text: str, currency: str, frequency: str) -> List[Dict[str, Any]]:
    """Rows of one series' CSV. Raises ValueError if the CSV holds another
    series than the one asked for."""
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        if (rec.get("CURRENCY"), rec.get("CURRENCY_DENOM"), rec.get("FREQ")) != (
                currency, BASE, FREQUENCIES[frequency]):
            raise ValueError(f"unexpected series {rec.get('KEY')!r} in the {currency} {frequency} response")
        value = (rec.get("OBS_VALUE") or "").strip()
        if value.lower() in ("", "nan"):
            continue
        rows.append({
            "currency": currency, "base": BASE, "frequency": frequency,
            "unit": f"{currency} per 1 {BASE}",
            "date": parse_period(rec["TIME_PERIOD"], frequency),
            "rate": float(value),
        })
    return rows


def check_units(text: str, currencies) -> None:
    """Raises ValueError unless every currency is listed, quoted in its own
    unit per 1 EUR, without a multiplier."""
    seen = {}
    for rec in csv.DictReader(io.StringIO(text)):
        seen[rec.get("CURRENCY")] = rec
    missing = [c for c in currencies if c not in seen]
    if missing:
        raise ValueError(f"the ECB has no daily reference rate for {missing}")
    for currency in currencies:
        rec = seen[currency]
        if rec.get("UNIT") != currency or rec.get("UNIT_MULT") != "0" or rec.get("CURRENCY_DENOM") != BASE:
            raise ValueError(
                f"{currency} is no longer quoted as {currency} per 1 {BASE} "
                f"(UNIT={rec.get('UNIT')!r}, UNIT_MULT={rec.get('UNIT_MULT')!r}, "
                f"CURRENCY_DENOM={rec.get('CURRENCY_DENOM')!r})")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


class _Fetcher:
    def __init__(self, pacer: _Pacer):
        self.pacer = pacer

    def get(self, url: str) -> str:
        self.pacer.wait()
        logger.info("GET %s", url)
        response = get_with_retries(url, headers={"User-Agent": _USER_AGENT}, timeout=60)
        response.raise_for_status()
        return response.text


def run(params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Returns tidy rows ``{currency, base, frequency, unit, date, rate}``."""
    params = params or {}
    currencies = [str(c).upper() for c in (params.get("currencies") or CURRENCIES)]
    frequencies = [str(f).lower() for f in (params.get("frequencies") or FREQUENCIES)]
    unknown = [f for f in frequencies if f not in FREQUENCIES]
    if unknown:
        raise ValueError(f"unknown frequencies {unknown}; use {list(FREQUENCIES)}")
    start_period = params.get("start_period")

    fetcher = _Fetcher(_Pacer(float(params.get("delay_min", 1.0)),
                              float(params.get("delay_max", 2.0))))
    check_units(fetcher.get(series_url("D", "+".join(currencies), lastNObservations=1)), currencies)

    rows: List[Dict[str, Any]] = []
    for frequency in frequencies:
        for currency in currencies:
            url = series_url(FREQUENCIES[frequency], currency, detail="dataonly", startPeriod=start_period)
            try:
                series = parse_series(fetcher.get(url), currency, frequency)
            except Exception as exc:
                logger.error("Failed to fetch %s: %s", url, exc)
                rows.append({"status": "error", "currency": currency, "frequency": frequency,
                             "url": url, "reason": str(exc)})
                continue
            if not series:
                logger.warning("%s %s: no rates", currency, frequency)
            rows.extend(series)

    logger.info("ECB fetch complete: %d rows", len(rows))
    return rows
