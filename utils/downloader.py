"""
FileDownloader — rate-limited, extension-aware binary file downloader.

Supports PDF and Excel files (.xlsx/.xls), routes each to its own output
directory, enforces a sliding-window requests-per-minute cap, and applies
an optional rename template before writing to disk.
"""

import logging
import random
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS: frozenset = frozenset({".pdf", ".xlsx", ".xls"})

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; siimpli-scrape-it/0.1; "
    "+https://github.com/siimpli; research-use-only)"
)


class FileDownloader:
    """
    Download PDF and Excel files from URLs with polite rate limiting.

    Args:
        pdf_dir:        Directory where .pdf files are saved.
        xlsx_dir:       Directory where .xlsx / .xls files are saved.
        rate_limit:     Maximum requests per 60-second window.
        delay_min:      Minimum polite delay between requests (seconds).
        delay_max:      Maximum polite delay between requests (seconds).
        skip_existing:  If True, skip URLs whose destination file already exists.
        rename_template:
            Optional Python format string applied to build the saved filename.
            Available variables: ``{commodity}``, ``{original_stem}``, ``{ext}``.
            Example: ``"{commodity}_{original_stem}{ext}"``
            When None, the original filename from the URL is preserved.
    """

    def __init__(
        self,
        pdf_dir: str,
        xlsx_dir: str,
        rate_limit: int = 10,
        delay_min: float = 3.0,
        delay_max: float = 7.0,
        skip_existing: bool = True,
        rename_template: Optional[str] = None,
    ) -> None:
        self.pdf_dir = Path(pdf_dir)
        self.xlsx_dir = Path(xlsx_dir)
        self.rate_limit = max(1, rate_limit)
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.skip_existing = skip_existing
        self.rename_template = rename_template

        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self.xlsx_dir.mkdir(parents=True, exist_ok=True)

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _DEFAULT_USER_AGENT})

        # Sliding window: timestamps (monotonic) of recent requests.
        self._window: Deque[float] = deque()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def download_url(
        self,
        url: str,
        rename_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Download a single file.

        Args:
            url:        Direct download URL.
            rename_to:  Override filename (already fully resolved).  Takes
                        precedence over ``rename_template``.
            metadata:   Extra key/value pairs merged into the result dict
                        (e.g. ``commodity``, ``link_text``).

        Returns:
            A dict with at least ``url``, ``status``, and on success ``path``.
        """
        meta = dict(metadata or {})
        ext = Path(urlparse(url).path).suffix.lower()

        if ext not in _SUPPORTED_EXTENSIONS:
            return {"url": url, "status": "skipped",
                    "reason": f"unsupported_extension:{ext}", **meta}

        dest_dir = self.pdf_dir if ext == ".pdf" else self.xlsx_dir
        filename = rename_to or Path(urlparse(url).path).name
        dest_path = dest_dir / filename

        if self.skip_existing and dest_path.exists():
            logger.info("Skipping (already exists): %s", dest_path)
            return {"url": url, "status": "skipped", "reason": "already_exists",
                    "path": str(dest_path), "filename": filename, **meta}

        self._polite_wait()

        try:
            response = self._session.get(url, timeout=60, stream=True)
            response.raise_for_status()
            dest_path.write_bytes(response.content)
            logger.info("Downloaded %s -> %s", url, dest_path)
            return {"url": url, "status": "success",
                    "path": str(dest_path), "filename": filename, **meta}

        except requests.HTTPError as exc:
            logger.warning("HTTP error downloading %s: %s", url, exc)
            return {"url": url, "status": "error", "reason": str(exc), **meta}
        except Exception as exc:
            logger.error("Failed to download %s: %s", url, exc)
            return {"url": url, "status": "error", "reason": str(exc), **meta}

    def download_batch(
        self, items: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Download a list of files described by dicts with at least a ``url`` key.
        Each dict may optionally contain ``rename_to`` and ``metadata`` keys.

        Returns a list of result dicts in the same order as ``items``.
        """
        return [
            self.download_url(
                url=item["url"],
                rename_to=item.get("rename_to"),
                metadata=item.get("metadata"),
            )
            for item in items
        ]

    def build_rename(
        self,
        url: str,
        commodity: str,
        year: Optional[str] = None,
        month: Optional[str] = None,
    ) -> str:
        """
        Construct a destination filename from URL context and publication metadata.

        Default scheme:
            Monthly:  ``{commodity}_{month}-{year}_{original_stem}{ext}``
                      e.g. ``aluminum_jan-2025_mis-202501-alumi.xlsx``
            Annual:   ``{commodity}_{year}_{original_stem}{ext}``
                      e.g. ``aluminum_2026_mcs2026-aluminum.pdf``
            Unknown:  ``{commodity}_{original_stem}{ext}``

        When ``rename_template`` is set it is used instead.
        Template variables: ``{commodity}``, ``{original_stem}``, ``{ext}``,
        ``{year}``, ``{month}``.
        """
        original = Path(urlparse(url).path).name
        stem = Path(original).stem
        ext = Path(original).suffix
        slug = _slugify(commodity)

        if self.rename_template:
            try:
                return self.rename_template.format(
                    commodity=slug, original_stem=stem, ext=ext,
                    year=year or "", month=month or "",
                )
            except KeyError as exc:
                logger.warning(
                    "rename_template has unknown variable %s; using default", exc
                )

        parts = [slug]
        if month and year:
            parts.append(f"{month}-{year}")
        elif year:
            parts.append(year)
        parts.append(stem)
        return f"{'_'.join(parts)}{ext}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _polite_wait(self) -> None:
        """
        Block until sending the next request respects both the rate cap and
        the per-request jitter delay.

        Uses a sliding window: we track the monotonic timestamp of each
        request in a deque and sleep when the window is full.
        """
        now = time.monotonic()

        # Evict entries outside the 60-second window.
        while self._window and now - self._window[0] >= 60.0:
            self._window.popleft()

        if len(self._window) >= self.rate_limit:
            # Oldest request in window; sleep until it falls out.
            sleep_for = 60.0 - (now - self._window[0]) + 0.05
            if sleep_for > 0:
                logger.debug("Rate cap reached — sleeping %.1fs", sleep_for)
                time.sleep(sleep_for)

        # Polite inter-request jitter regardless of cap.
        jitter = random.uniform(self.delay_min, self.delay_max)
        time.sleep(jitter)

        self._window.append(time.monotonic())


def _slugify(text: str) -> str:
    """Convert a commodity name to a lowercase underscore slug."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
