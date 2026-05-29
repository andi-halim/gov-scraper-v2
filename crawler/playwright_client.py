"""T-42: Headless Playwright wrapper for JS-heavy pages."""
import asyncio
import logging

from playwright.async_api import async_playwright

from crawler.http_client import USER_AGENT

logger = logging.getLogger(__name__)

_PAGE_TIMEOUT_MS = 30_000


async def _render_async(url: str) -> str:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(user_agent=USER_AGENT)
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=_PAGE_TIMEOUT_MS)
            return await page.content()
        finally:
            await browser.close()


def fetch_rendered(url: str) -> str:
    """Navigate url with headless Chromium and return the fully rendered HTML."""
    return asyncio.run(_render_async(url))
