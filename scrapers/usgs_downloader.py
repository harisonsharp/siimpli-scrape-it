"""
USGS Mineral Commodity Files Downloader
========================================

Two-phase scraper targeting the USGS National Minerals Information Center:

  Phase 1 — Discovery
    Fetches the commodity index page and follows every link matching the
    standard ``/<commodity>-statistics-and-information`` URL pattern to
    build a list of commodity landing pages.

  Phase 2 — Inventory + Download
    On each commodity page, finds all <a> tags whose href ends in .pdf,
    .xlsx, or .xls.  Downloads each file via FileDownloader (which handles
    rate limiting, polite delays, skip-if-existing, and renaming).

Returns a manifest of dicts — one per attempted download — suitable for
storage as JSON or CSV by the runner's save_data pipeline.

Usage (via job runner):
    script: scrapers.usgs_downloader

Usage (direct):
    python -c "from scrapers.usgs_downloader import run; run()"
"""

import csv
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils.downloader import FileDownloader
from utils.http import get_with_retries

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USGS_ROOT = "https://www.usgs.gov"

_BASE_URL = (
    "https://www.usgs.gov/centers/national-minerals-information-center"
    "/commodity-statistics-and-information"
)

# Matches commodity landing page paths like:
#   /centers/national-minerals-information-center/aluminum-statistics-and-information
_COMMODITY_PATH_RE = re.compile(
    r"/centers/national-minerals-information-center"
    r"/[a-z][a-z0-9\-]+-statistics-and-information$",
    re.IGNORECASE,
)

_DOWNLOAD_EXTENSIONS: frozenset = frozenset({".pdf", ".xlsx", ".xls"})

_COMMODITY_CSV_PATH = "data/usgs/input/usgs_metal_prices_request.csv"

# Matches the year-prefix line in a monthly section, e.g. "2025:" or "2025 :"
_YEAR_PREFIX_RE = re.compile(r"^(1\d{3}|2\d{3})\s*:")

# Matches a bare year anywhere in text (annual link text, fallback)
_YEAR_IN_TEXT_RE = re.compile(r"\b(1\d{3}|2\d{3})\b")

# Normalises any month string to a 3-letter lowercase abbreviation.
# Covers full names, abbreviations, and mixed case.
_MONTH_NORMALIZE: Dict[str, str] = {
    **{k: v for k, v in [
        ("january",   "jan"), ("february",  "feb"), ("march",     "mar"),
        ("april",     "apr"), ("may",        "may"), ("june",      "jun"),
        ("july",      "jul"), ("august",     "aug"), ("september", "sep"),
        ("october",   "oct"), ("november",   "nov"), ("december",  "dec"),
    ]},
    **{v: v for v in ["jan","feb","mar","apr","may","jun",
                      "jul","aug","sep","oct","nov","dec"]},
}

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; seng350_research_project/0.1; "
        "+https://github.com/seng350_research_project; research-use-only)"
    )
}

# ---------------------------------------------------------------------------
# CSV + pattern helpers
# ---------------------------------------------------------------------------


def _load_base_names(csv_path: str) -> List[str]:
    """
    Read the commodity request CSV and return a sorted list of unique,
    lowercase base names (the 'Base Name' column).
    """
    names: set = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row.get("Base Name", "").strip().lower()
            if name:
                names.add(name)
    return sorted(names)


def _build_commodity_pattern(base_names: List[str]) -> re.Pattern:
    """
    Compile a regex that matches only USGS commodity page paths whose
    slug (the segment between 'center/' and '-statistics-and-information')
    is one of the supplied base names.

    Names are sorted longest-first in the alternation so that longer names
    are tried before any shorter prefix they might share.

    Note: USGS uses American spellings in URLs (e.g. 'aluminum', not
    'aluminium').  Names in the CSV must match the URL slug exactly.
    """
    alternation = "|".join(
        re.escape(n) for n in sorted(base_names, key=len, reverse=True)
    )
    return re.compile(
        r"/centers/national-minerals-information-center"
        rf"/({alternation})-statistics-and-information$",
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _fetch_soup(url: str) -> BeautifulSoup:
    """Fetch a URL and return a parsed BeautifulSoup tree."""
    response = get_with_retries(url, headers=_REQUEST_HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _find_commodity_links(
    soup: BeautifulSoup,
    pattern: re.Pattern = _COMMODITY_PATH_RE,
) -> List[str]:
    """
    Extract all unique commodity landing page URLs from the index page.

    Looks for <a href="..."> tags whose path matches *pattern*.
    Pass a pattern built by ``_build_commodity_pattern`` to restrict
    results to only the commodities listed in the request CSV.
    Handles both absolute and relative hrefs.
    """
    seen: set = set()
    urls: List[str] = []

    for tag in soup.find_all("a", href=True):
        href: str = tag["href"].strip()
        # Strip query strings and fragments before matching.
        path = urlparse(href).path.rstrip("/")

        if pattern.search(path):
            full_url = urljoin(_USGS_ROOT, href)
            if full_url not in seen:
                seen.add(full_url)
                urls.append(full_url)

    return urls


def _find_download_links(
    soup: BeautifulSoup, page_url: str
) -> List[Dict[str, str]]:
    """
    Extract all PDF / XLSX / XLS download links from a commodity page,
    enriched with publication type, year, and month where discoverable.

    USGS commodity pages use two layouts; both are handled:

    * Newer pages (e.g. aluminum) — flat ``<h3>`` / ``<p>`` siblings where
      monthly year cursors appear as ``"YYYY:"`` paragraph prefixes.
    * Older pages (e.g. arsenic, antimony) — ``<h3>`` followed by
      ``<ul>``/``<li>`` lists where the year is embedded in each link's text.

    State machine over ``<h3>``, ``<p>``, and ``<li>`` tags:

    * ``<h3>`` — transitions the current section to "monthly" or "annual"
      (or None for unrecognised headings) and resets the year cursor.
    * ``<p>`` or ``<li>`` whose text starts with a 4-digit year and colon
      — updates the year cursor (monthly section only).
    * ``<p>`` or ``<li>`` containing ``<a>`` tags with a recognised file
      extension — emits download records using the current section / year state.

    Returns a list of dicts with keys:
        url               — absolute download URL
        ext               — lowercase file extension
        link_text         — visible anchor text
        publication_type  — "monthly", "annual", or None
        year              — 4-digit string or None
        month             — 3-letter lowercase abbreviation or None
    """
    seen: set = set()
    links: List[Dict[str, str]] = []

    current_section: Optional[str] = None   # "monthly" | "annual" | None
    current_year: Optional[str] = None

    for tag in soup.find_all(["h3", "p", "li"]):

        # ── Section header ──────────────────────────────────────────────
        if tag.name == "h3":
            heading = tag.get_text(strip=True).lower()
            if "monthly" in heading:
                current_section = "monthly"
            elif "annual" in heading:
                current_section = "annual"
            else:
                current_section = None
            current_year = None
            continue

        # ── Paragraph ───────────────────────────────────────────────────
        p_text = tag.get_text(strip=True)

        # Update year cursor when paragraph opens with "YYYY:" (monthly).
        if current_section == "monthly":
            m = _YEAR_PREFIX_RE.match(p_text)
            if m:
                current_year = m.group(1)

        # Collect every download link inside this paragraph.
        for a in tag.find_all("a", href=True):
            href: str = a["href"].strip()
            ext = Path(urlparse(href).path).suffix.lower()

            if ext not in _DOWNLOAD_EXTENSIONS:
                continue

            full_url = urljoin(page_url, href)
            if full_url in seen:
                continue
            seen.add(full_url)

            link_text = a.get_text(strip=True)
            year: Optional[str] = None
            month: Optional[str] = None

            if current_section == "monthly":
                year = current_year
                # Month is the link text; normalise to 3-letter abbreviation.
                raw = link_text.lower().split()[0] if link_text else ""
                month = _MONTH_NORMALIZE.get(raw)

            elif current_section == "annual":
                # Year is embedded in the link text, e.g. "2026" or
                # "2022 Tables-only release".
                m = _YEAR_IN_TEXT_RE.search(link_text)
                year = m.group(1) if m else None

            else:
                # Outside a recognised section — best-effort year extraction.
                m = _YEAR_IN_TEXT_RE.search(link_text)
                year = m.group(1) if m else None

            links.append(
                {
                    "url": full_url,
                    "ext": ext,
                    "link_text": link_text,
                    "publication_type": current_section,
                    "year": year,
                    "month": month,
                }
            )

    return links


def _commodity_name_from_url(url: str) -> str:
    """
    Derive a human-readable commodity name from a commodity page URL.

    Example:
        .../aluminum-statistics-and-information  →  "Aluminum"
        .../rare-earths-statistics-and-information  →  "Rare Earths"
    """
    match = re.search(
        r"/([a-z][a-z0-9\-]+)-statistics-and-information", url, re.IGNORECASE
    )
    if match:
        return match.group(1).replace("-", " ").title()
    return "Unknown"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Crawl the USGS commodity index and download all linked PDFs and Excel files.

    Parameters (via job YAML ``parameters`` block):

    ==================  ==========  ==========================================
    Key                 Default     Description
    ==================  ==========  ==========================================
    pdf_dir             data/usgs/pdf   Destination folder for PDF files.
    xlsx_dir            data/usgs/xlsx  Destination folder for XLSX/XLS files.
    rate_limit          10          Max requests per minute (HTML + downloads).
    delay_min           3.0         Min inter-request sleep (seconds).
    delay_max           7.0         Max inter-request sleep (seconds).
    skip_existing       True        Skip files that already exist on disk.
    rename              True        Apply automated renaming scheme.
    rename_template     None        Format string; vars: {commodity},
                                    {original_stem}, {ext}.  Overrides the
                                    default ``{commodity}_{original}`` scheme.
    max_commodities     None        Crawl at most N commodity pages (testing).
    ==================  ==========  ==========================================

    Returns:
        List[Dict] — one entry per attempted file download, containing at
        minimum ``url``, ``status``, ``commodity``.  Suitable for storage
        as JSON or CSV.
    """
    params = params or {}

    pdf_dir         = params.get("pdf_dir",         "data/usgs/pdf")
    xlsx_dir        = params.get("xlsx_dir",        "data/usgs/xlsx")
    rate_limit      = int(params.get("rate_limit",  10))
    delay_min       = float(params.get("delay_min", 3.0))
    delay_max       = float(params.get("delay_max", 7.0))
    skip_existing   = bool(params.get("skip_existing", True))
    do_rename       = bool(params.get("rename",     True))
    rename_template = params.get("rename_template", None)
    max_commodities = params.get("max_commodities", None)

    downloader = FileDownloader(
        pdf_dir=pdf_dir,
        xlsx_dir=xlsx_dir,
        rate_limit=rate_limit,
        delay_min=delay_min,
        delay_max=delay_max,
        skip_existing=skip_existing,
        rename_template=rename_template if do_rename else None,
    )

    results: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Phase 1: Build commodity-specific URL pattern from the request CSV.
    # ------------------------------------------------------------------
    csv_path = params.get("commodity_csv", _COMMODITY_CSV_PATH)
    try:
        base_names = _load_base_names(csv_path)
        commodity_pattern = _build_commodity_pattern(base_names)
        logger.info(
            "Filtering to %d commodities from CSV (%s): %s",
            len(base_names), csv_path, ", ".join(base_names),
        )
    except FileNotFoundError:
        logger.warning(
            "Commodity CSV not found at %s — falling back to generic pattern",
            csv_path,
        )
        commodity_pattern = _COMMODITY_PATH_RE

    # ------------------------------------------------------------------
    # Phase 1: Discover matching commodity landing pages from the index.
    # ------------------------------------------------------------------
    logger.info("Phase 1 — fetching commodity index: %s", _BASE_URL)
    try:
        index_soup = _fetch_soup(_BASE_URL)
    except Exception as exc:
        logger.error("Failed to fetch commodity index: %s", exc)
        return [{"url": _BASE_URL, "status": "error", "reason": str(exc)}]

    commodity_urls = _find_commodity_links(index_soup, commodity_pattern)
    logger.info("Found %d commodity pages", len(commodity_urls))

    if max_commodities is not None:
        commodity_urls = commodity_urls[: int(max_commodities)]
        logger.info("Limiting to %d commodity pages (max_commodities)", len(commodity_urls))

    if not commodity_urls:
        logger.warning(
            "No commodity links found on index page — the page structure may "
            "have changed.  Returning empty manifest."
        )
        return results

    # ------------------------------------------------------------------
    # Phase 2: Crawl each commodity page and download files.
    # ------------------------------------------------------------------
    for commodity_url in commodity_urls:
        commodity = _commodity_name_from_url(commodity_url)
        logger.info("Phase 2 — crawling: %s (%s)", commodity, commodity_url)

        try:
            page_soup = _fetch_soup(commodity_url)
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", commodity_url, exc)
            results.append(
                {
                    "url": commodity_url,
                    "status": "error",
                    "reason": str(exc),
                    "commodity": commodity,
                    "phase": "crawl",
                }
            )
            continue

        download_links = _find_download_links(page_soup, commodity_url)
        logger.info(
            "  %s: found %d download link(s)", commodity, len(download_links)
        )

        if not download_links:
            results.append(
                {
                    "url": commodity_url,
                    "status": "no_downloads",
                    "commodity": commodity,
                    "phase": "crawl",
                }
            )
            continue

        for link in download_links:
            rename_to: Optional[str] = None
            if do_rename:
                rename_to = downloader.build_rename(
                    link["url"],
                    commodity,
                    year=link.get("year"),
                    month=link.get("month"),
                )

            result = downloader.download_url(
                url=link["url"],
                rename_to=rename_to,
                metadata={
                    "commodity": commodity,
                    "commodity_page": commodity_url,
                    "link_text": link["link_text"],
                    "publication_type": link.get("publication_type"),
                    "year": link.get("year"),
                    "month": link.get("month"),
                },
            )
            results.append(result)

    # ------------------------------------------------------------------
    # Summary log
    # ------------------------------------------------------------------
    successes  = sum(1 for r in results if r.get("status") == "success")
    skipped    = sum(1 for r in results if r.get("status") == "skipped")
    errors     = sum(1 for r in results if r.get("status") == "error")

    logger.info(
        "Download run complete — success: %d  skipped: %d  errors: %d  total: %d",
        successes, skipped, errors, len(results),
    )

    return results
