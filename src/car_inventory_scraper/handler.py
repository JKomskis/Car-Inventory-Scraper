"""Custom Scrapy download handler that uses *cloudscraper* to bypass Cloudflare.

Inherits from Scrapy's :class:`HTTP11DownloadHandler` so regular HTTP(s)
requests pass through unchanged.  Spiders opt-in to the cloudscraper
path by setting ``meta["use_cloudscraper"] = True`` on individual
requests.

Register the handler in ``settings.py``::

    DOWNLOAD_HANDLERS = {
        "http": "car_inventory_scraper.handler.CloudScraperHandler",
        "https": "car_inventory_scraper.handler.CloudScraperHandler",
    }

Example spider usage::

    yield scrapy.Request(
        url,
        meta={"use_cloudscraper": True},
        callback=self.parse,
    )
"""

from __future__ import annotations

import logging

import cloudscraper
from scrapy import Request
from scrapy.core.downloader.handlers.http11 import HTTP11DownloadHandler
from scrapy.http import HtmlResponse
from scrapy.utils.defer import deferred_to_future
from twisted.internet.threads import deferToThread

logger = logging.getLogger(__name__)


class CloudScraperHandler(HTTP11DownloadHandler):
    """HTTPS handler with optional *cloudscraper* bypass.

    When a request carries ``meta["use_cloudscraper"] == True``, the
    response is fetched via a persistent :class:`cloudscraper.CloudScraper`
    session (cookies are shared across requests, just like a real browser).
    All other requests are delegated to the parent
    :class:`HTTP11DownloadHandler`.
    """

    def __init__(self, crawler):
        super().__init__(crawler)
        self._crawler = crawler
        self._scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True},
        )

    # ------------------------------------------------------------------
    async def download_request(self, request: Request):
        """Route *request* through cloudscraper or the default HTTP client."""
        if not request.meta.get("use_cloudscraper") or request.method != "GET":
            return await super().download_request(request)

        timeout = request.meta.get("download_timeout", self._crawler.settings.getint("DOWNLOAD_TIMEOUT", 180))

        resp = await deferred_to_future(deferToThread(
            self._scraper.get,
            url=request.url,
            timeout=timeout,
        ))

        # Strip Content-Encoding since resp.text is already decoded by
        # requests/urllib3; leaving it causes Scrapy to double-decompress.
        headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-encoding"}
        return HtmlResponse(
            url=request.url,
            status=resp.status_code,
            headers=headers,
            body=resp.text,
            encoding="utf-8",
            request=request,
        )
