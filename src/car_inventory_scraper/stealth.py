"""Playwright stealth helpers for bypassing bot detection (e.g. Cloudflare).

The ``playwright-stealth`` library patches common browser fingerprinting
vectors that bot-detection services check:

- ``navigator.webdriver`` property
- Chrome DevTools protocol indicators
- Missing browser plugins / language headers
- WebGL renderer strings
- Consistent iframe ``contentWindow`` behaviour

Usage with scrapy-playwright — pass :func:`apply_stealth` as the
``playwright_page_init_callback`` in request meta::

    yield scrapy.Request(
        url,
        meta={
            "playwright": True,
            "playwright_page_init_callback": apply_stealth,
            ...
        },
    )
"""

from __future__ import annotations

from playwright_stealth import Stealth

_stealth = Stealth()


async def apply_stealth(page, request):
    """scrapy-playwright page-init callback that applies stealth patches."""
    await _stealth.apply_stealth_async(page)
