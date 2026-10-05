"""
Playwright context managers, including a manual CAPTCHA-gate helper.
"""

from contextlib import contextmanager
from typing import Generator, List
from playwright.sync_api import sync_playwright, Page, BrowserContext
import logging

logger = logging.getLogger(__name__)

@contextmanager
def get_browser_page(headless: bool = True) -> Generator[Page, None, None]:
    """
    Provide a managed Playwright browser page context.
    
    Args:
        headless (bool): Run the browser in headless mode.
        
    Yields:
        Page: A playwright Page instance.
    """
    playwright = sync_playwright().start()
    browser = None
    try:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        yield page
    finally:
        if browser:
            browser.close()
        playwright.stop()


# ---------------------------------------------------------------------------
# CAPTCHA gate helper
# ---------------------------------------------------------------------------

# Substrings in the page title or body that indicate a bot-block / CAPTCHA page.
_CAPTCHA_SIGNALS: List[str] = [
    "captcha",
    "radware block",
    "we are sorry",
    "bot",
    "access denied",
    "just a moment",   # Cloudflare
    "ddos-guard",
    "please verify",
    "are you human",
]


def _page_needs_captcha(page: Page) -> bool:
    """Return True if the current page appears to be a CAPTCHA / block page."""
    try:
        title = (page.title() or "").lower()
        body_text = (page.inner_text("body") or "").lower()
        return any(sig in title or sig in body_text for sig in _CAPTCHA_SIGNALS)
    except Exception:
        return False


@contextmanager
def captcha_gate(
    url: str,
    *,
    captcha_signals: List[str] | None = None,
    timeout_ms: int = 300_000,
    wait_until: str = "domcontentloaded",
) -> Generator[Page, None, None]:
    """
    Open a visible browser window, navigate to *url*, and pause if a
    CAPTCHA or bot-block page is detected.  The user solves the challenge
    manually; once the page navigates away from the block page, automated
    scraping resumes on the same authenticated session.

    This helper is wired into the runner automatically — individual scraper
    scripts do **not** need to call it.  Scripts that opt in simply replace
    ``get_browser_page`` with ``captcha_gate``.

    Args:
        url:              The target URL to open.
        captcha_signals:  Extra substrings (case-insensitive) that indicate
                          a CAPTCHA page.  Merged with the built-in list.
        timeout_ms:       How long (ms) to wait for the user to solve the
                          CAPTCHA before raising a TimeoutError.  Default 5 min.

    Yields:
        Page: The authenticated Playwright Page, ready for scraping.

    Example::

        with captcha_gate("https://example.com/protected") as page:
            data = page.inner_text("#data-table")

    """
    signals = list(_CAPTCHA_SIGNALS)
    if captcha_signals:
        signals.extend(s.lower() for s in captcha_signals)

    playwright = sync_playwright().start()
    browser = None
    try:
        # Always open visibly so the user can interact
        browser = playwright.chromium.launch(headless=False)
        context: BrowserContext = browser.new_context()
        page = context.new_page()

        logger.info(f"captcha_gate: navigating to {url} (wait_until={wait_until!r})")
        page.goto(url, wait_until=wait_until)

        if _page_needs_captcha(page):
            logger.warning(
                "captcha_gate: CAPTCHA / block page detected. "
                "Waiting for user to solve the challenge in the browser window…"
            )
            print(
                "\n"
                "╔══════════════════════════════════════════════════════════╗\n"
                "║  🔐  CAPTCHA detected — action required                 ║\n"
                "║                                                          ║\n"
                "║  Please solve the CAPTCHA in the browser window that     ║\n"
                "║  just opened.  The scraper will resume automatically     ║\n"
                "║  once the target page has loaded.                        ║\n"
                "╚══════════════════════════════════════════════════════════╝\n"
            )
            # Wait for a navigation away from the block page
            page.wait_for_function(
                """(signals) => {
                    const title = (document.title || '').toLowerCase();
                    const body  = (document.body  ? document.body.innerText : '').toLowerCase();
                    return !signals.some(s => title.includes(s) || body.includes(s));
                }""",
                arg=signals,
                timeout=timeout_ms,
                polling=1000,
            )
            logger.info("captcha_gate: challenge solved, resuming scraping.")

        yield page

    finally:
        if browser:
            browser.close()
        playwright.stop()
