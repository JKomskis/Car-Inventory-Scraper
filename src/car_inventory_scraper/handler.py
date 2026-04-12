"""Custom Scrapy download handler that uses *nodriver* to bypass Cloudflare.

``nodriver`` is the async successor to ``undetected-chromedriver``.  It
controls a real Google Chrome instance via CDP without the automation
markers that Cloudflare detects (no ``Runtime.enable``, no ``cdc_``
variables, no ``--enable-automation`` flag).

Inherits from Scrapy's :class:`HTTP11DownloadHandler` so regular HTTP(s)
requests pass through unchanged.  Spiders opt-in to the nodriver path
by setting ``meta["nodriver"] = True`` on individual requests.

Register the handler in ``settings.py``::

    DOWNLOAD_HANDLERS = {
        "http": "car_inventory_scraper.handler.NoDriverHandler",
        "https": "car_inventory_scraper.handler.NoDriverHandler",
    }

Example spider usage::

    yield scrapy.Request(
        url,
        meta={"nodriver": True},
        callback=self.parse,
    )

Spiders can optionally specify a JavaScript expression to wait for
before returning the page content.  The expression must evaluate to a
truthy value once the real page (not a loading screen) is ready::

    yield scrapy.Request(
        url,
        meta={"nodriver": True, "nodriver_wait_js": "document.querySelector('.vehicle_item')"},
        callback=self.parse,
    )
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import socket

import nodriver
import nodriver.cdp.fetch as cdp_fetch
import nodriver.cdp.network as cdp_network
from nodriver.core.config import temp_profile_dir
from scrapy import Request
from scrapy.core.downloader.handlers.http11 import HTTP11DownloadHandler
from scrapy.http import HtmlResponse

logger = logging.getLogger(__name__)

# Resource types to block — saves bandwidth and speeds up page loads.
_BLOCKED_RESOURCE_TYPES = {
    cdp_network.ResourceType.IMAGE,
    cdp_network.ResourceType.MEDIA,
    cdp_network.ResourceType.FONT,
}


class NoDriverHandler(HTTP11DownloadHandler):
    """HTTPS handler with optional *nodriver* bypass.

    When a request carries ``meta["nodriver"] == True``, the response is
    fetched via a persistent :class:`nodriver.Browser` instance that
    bypasses Cloudflare bot-detection.  All other requests are delegated
    to the parent :class:`HTTP11DownloadHandler`.
    """

    def __init__(self, crawler):
        super().__init__(crawler)
        self._crawler = crawler
        self._browser: nodriver.Browser | None = None
        self._chrome_process: asyncio.subprocess.Process | None = None
        self._browser_lock = asyncio.Lock()

    # Candidate browser binaries in preference order.
    _BROWSER_CANDIDATES = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ]

    # How long to wait for Chrome's debug port to accept connections.
    _BROWSER_READY_TIMEOUT = 30

    @staticmethod
    def _free_port() -> int:
        """Find an available TCP port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    async def _wait_for_port(self, host: str, port: int) -> None:
        """Poll until *host:port* accepts a TCP connection."""
        deadline = asyncio.get_event_loop().time() + self._BROWSER_READY_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=2,
                )
                writer.close()
                await writer.wait_closed()
                return
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.5)
        raise RuntimeError(
            f"Chrome debug port {host}:{port} not ready after "
            f"{self._BROWSER_READY_TIMEOUT}s"
        )

    async def _get_browser(self) -> nodriver.Browser:
        """Lazily launch Chrome and connect nodriver to it.

        Instead of letting nodriver both launch *and* connect (with its
        hard-coded ~2.75 s timeout), we launch Chrome ourselves and wait
        for the debug port with a generous timeout before handing the
        running instance to nodriver via ``host`` / ``port``.
        """
        async with self._browser_lock:
            if self._browser is None:
                browser_path = None
                for name in self._BROWSER_CANDIDATES:
                    path = shutil.which(name)
                    if path:
                        browser_path = path
                        break
                if not browser_path:
                    raise FileNotFoundError(
                        "No Chrome/Chromium binary found on PATH"
                    )
                logger.info("Using browser: %s", browser_path)

                host = "127.0.0.1"
                port = self._free_port()
                user_data_dir = temp_profile_dir()

                # Build the same argument set nodriver would use, plus
                # our CI-friendly extras.
                args = [
                    f"--remote-debugging-host={host}",
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={user_data_dir}",
                    "--remote-allow-origins=*",
                    "--no-first-run",
                    "--no-service-autorun",
                    "--no-default-browser-check",
                    "--homepage=about:blank",
                    "--no-pings",
                    "--password-store=basic",
                    "--disable-infobars",
                    "--disable-breakpad",
                    "--disable-dev-shm-usage",
                    "--disable-session-crashed-bubble",
                    "--disable-search-engine-choice-screen",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--no-zygote",
                    "--disable-software-rasterizer",
                ]

                self._chrome_process = await asyncio.create_subprocess_exec(
                    browser_path,
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                logger.info(
                    "Launched Chrome (pid %d) on %s:%d",
                    self._chrome_process.pid,
                    host,
                    port,
                )

                await self._wait_for_port(host, port)

                # Connect nodriver to the already-running browser.
                self._browser = await nodriver.Browser.create(
                    headless=False,
                    sandbox=False,
                    host=host,
                    port=port,
                    browser_executable_path=browser_path,
                )
            return self._browser

    async def download_request(self, request: Request):
        """Route *request* through nodriver or the default HTTP client."""
        if not request.meta.get("nodriver") or request.method != "GET":
            return await super().download_request(request)

        browser = await self._get_browser()
        tab = await browser.get(request.url, new_tab=True)

        # Block images, media, and fonts to save bandwidth.
        await self._block_heavy_resources(tab)

        timeout = request.meta.get(
            "download_timeout",
            self._crawler.settings.getint("DOWNLOAD_TIMEOUT", 180),
        )
        wait_js = request.meta.get("nodriver_wait_js")

        # Wait for the real page to load.  This handles both Cloudflare
        # challenge pages and other loading screens by polling until:
        #   1. The page title is no longer a known challenge title.
        #   2. document.readyState is "complete".
        #   3. (Optional) A spider-specified JS expression is truthy.
        try:
            await asyncio.wait_for(
                self._wait_for_real_page(tab, wait_js),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Page did not finish loading within %ds for %s",
                timeout,
                request.url,
            )

        content = await tab.get_content()

        # Disable fetch interception before closing to prevent pending
        # _intercept tasks from firing on a dead tab.
        try:
            await tab.feed_cdp(cdp_fetch.disable())
        except Exception:
            pass
        await tab.close()

        return HtmlResponse(
            url=request.url,
            body=content,
            encoding="utf-8",
            request=request,
        )

    @staticmethod
    async def _block_heavy_resources(tab: nodriver.Tab) -> None:
        """Use CDP Fetch domain to block images, media, and fonts."""
        async def _intercept(event: cdp_fetch.RequestPaused):
            try:
                if event.resource_type in _BLOCKED_RESOURCE_TYPES:
                    await tab.feed_cdp(
                        cdp_fetch.fail_request(event.request_id, cdp_network.ErrorReason.BLOCKED_BY_CLIENT)
                    )
                else:
                    await tab.feed_cdp(cdp_fetch.continue_request(event.request_id))
            except Exception:
                pass  # tab already closed

        tab.add_handler(cdp_fetch.RequestPaused, _intercept)
        await tab.feed_cdp(cdp_fetch.enable(
            patterns=[cdp_fetch.RequestPattern(url_pattern="*")],
        ))

    # Known loading/challenge page titles to wait past.
    _CHALLENGE_TITLES = {"Just a moment...", ""}

    @staticmethod
    async def _wait_for_real_page(
        tab: nodriver.Tab,
        wait_js: str | None = None,
    ) -> None:
        """Poll until the page is past any challenge and fully loaded.

        Checks three conditions in a loop:
        1. Title is not a known challenge/loading page title.
        2. ``document.readyState`` is ``"complete"``.
        3. If *wait_js* is given, that expression evaluates to truthy.
        """
        while True:
            try:
                title = str(await tab.evaluate("document.title") or "")
                if title in NoDriverHandler._CHALLENGE_TITLES:
                    await asyncio.sleep(0.5)
                    continue

                ready = str(await tab.evaluate("document.readyState") or "")
                if ready != "complete":
                    await asyncio.sleep(0.3)
                    continue

                if wait_js:
                    result = await tab.evaluate(wait_js)
                    if not result:
                        await asyncio.sleep(0.3)
                        continue

                return
            except Exception:
                await asyncio.sleep(0.5)

    async def close(self):
        """Shut down the browser when Scrapy stops."""
        if self._browser:
            self._browser.stop()
            # Give nodriver's internal tasks a moment to wind down so
            # asyncio doesn't warn about pending tasks being destroyed.
            await asyncio.sleep(0.5)
            self._browser = None
        if self._chrome_process:
            try:
                self._chrome_process.terminate()
                await asyncio.wait_for(self._chrome_process.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                self._chrome_process.kill()
            self._chrome_process = None
        await super().close()
