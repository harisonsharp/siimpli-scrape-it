import pytest
from playwright.sync_api import Page
from utils.browser import get_browser_page

def test_browser_context_yields_page_and_closes():
    """System integration check verifying Playwright process yields effectively and safely disposes processes."""
    page_ref = None
    with get_browser_page(headless=True) as page:
        assert isinstance(page, Page)
        page.goto("data:text/html,<h1>Test Content</h1>")
        assert "Test Content" in page.content()
        page_ref = page
        
    assert page_ref.is_closed()

def test_browser_context_safely_disposes_on_exception():
    """Verify Playwright securely closes even if scraper fails internally."""
    page_ref = None
    with pytest.raises(RuntimeError, match="Dummy error"):
        with get_browser_page(headless=True) as page:
            page_ref = page
            raise RuntimeError("Dummy error")
            
    assert page_ref is not None
    assert page_ref.is_closed()
