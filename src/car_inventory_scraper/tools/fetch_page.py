"""Fetch the fully-rendered HTML of a page using Playwright.

This is a small standalone helper intended to be called by an AI agent (or
from the command line) so it can inspect the DOM of a JavaScript-heavy
dealership page without needing a full Scrapy crawl.

Usage as a CLI::

    uv run python -m car_inventory_scraper.tools.fetch_page \
        "https://www.toyotaofbellevue.com/searchnew.aspx?Make=Toyota"

Usage from Python::

    from car_inventory_scraper.tools.fetch_page import fetch_page
    html = fetch_page("https://www.example.com/inventory")
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def fetch_page(
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30_000,
    headless: bool = True,
    wait_for_selector: str | None = None,
    extra_wait_ms: int = 2_000,
) -> str:
    """Launch a headless browser, navigate to *url*, and return the page HTML.

    Parameters
    ----------
    url:
        The URL to fetch.
    wait_until:
        Playwright *wait_until* event (``"domcontentloaded"``,
        ``"load"``, ``"networkidle"``).
    timeout:
        Navigation timeout in milliseconds.
    headless:
        Whether to run the browser headlessly.
    wait_for_selector:
        Optional CSS selector to wait for before capturing HTML.
        Useful for pages that lazy-load vehicle cards.
    extra_wait_ms:
        Extra time (ms) to wait after the page / selector is ready,
        giving JS frameworks a moment to finish rendering.

    Returns
    -------
    str
        The outer HTML of the ``<html>`` element (i.e. the full page source
        after JS execution).
    """
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)

            if wait_for_selector:
                await page.wait_for_selector(
                    wait_for_selector, timeout=timeout
                )

            # Give any remaining JS a moment to settle.
            if extra_wait_ms:
                await page.wait_for_timeout(extra_wait_ms)

            html = await page.content()
        finally:
            await context.close()
            await browser.close()

    return html


# ── CLI entry-point ──────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fetch fully-rendered HTML of a page via Playwright.",
    )
    parser.add_argument("url", help="URL to fetch")
    parser.add_argument(
        "--wait-for",
        default=None,
        dest="wait_for_selector",
        help="CSS selector to wait for before capturing HTML",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30_000,
        help="Navigation timeout in ms (default: 30000)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window",
    )
    args = parser.parse_args(argv)

    html = asyncio.run(
        fetch_page(
            args.url,
            headless=not args.no_headless,
            wait_for_selector=args.wait_for_selector,
            timeout=args.timeout,
        )
    )
    sys.stdout.write(html)


if __name__ == "__main__":
    main()
