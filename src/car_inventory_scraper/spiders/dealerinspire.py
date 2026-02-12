"""Spider for DealerInspire-powered dealership websites.

DealerInspire is a dealership website platform that uses Algolia
InstantSearch for its inventory search pages.  Vehicle search result
pages render client-side cards wrapped in
``.result-wrap.new-vehicle`` elements, each carrying a ``data-vehicle``
JSON attribute with the core vehicle metadata (VIN, stock number, year,
make, model, trim, exterior color, MSRP, advertised price, and
expected arrival date).

Additional DOM elements provide:

- **Status** (``.hit-status span``) — "In Transit", "Build Phase", etc.
- **TSRP / price** (``.hit-price__value .ashallow``) — the
  Total Suggested Retail Price shown on the card.
- **Title** (``.title-bottom``) — encodes year, model, trim, and
  drivetrain (e.g. "2026  RAV4 XLE Premium AWD").
- **Detail link** (``a.hit-link``) — URL to the vehicle detail page.

Unlike other platforms, the search results page already contains all
the information available for each vehicle, so this spider does **not**
follow links to individual vehicle detail pages.

The platform is protected by Cloudflare bot-detection and requires a
non-headless browser with stealth patches to bypass.  Prefix the
command with ``xvfb-run`` when running on a headless server::

    xvfb-run --auto-servernum --server-args="-screen 0 1920x1080x24" \\
        car-inventory-scraper crawl dealerinspire --no-headless \\
            --url "https://www.marysvilletoyota.com/new-vehicles/rav4/…"

Example usage::

    car-inventory-scraper crawl dealerinspire --no-headless \\
        --url "https://www.marysvilletoyota.com/new-vehicles/rav4/…"
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import scrapy
from scrapy.http import HtmlResponse
from scrapy_playwright.page import PageMethod

from car_inventory_scraper.items import CarItem
from car_inventory_scraper.parsing_helpers import (
    normalize_color,
    normalize_drivetrain,
    parse_price,
)
from car_inventory_scraper.stealth import apply_stealth


class DealerInspireSpider(scrapy.Spider):
    """Scrape vehicle inventory from a DealerInspire-powered dealership site."""

    name = "dealerinspire"

    # Passed via ``-a url=…`` or the CLI wrapper.
    def __init__(self, url: str | None = None, dealer_name: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if url is None:
            raise ValueError(
                "A starting URL is required. "
                "Pass it with: -a url=https://example.com/new-vehicles/…"
            )
        self.start_url = url
        self._dealer_name_override = dealer_name

    # ------------------------------------------------------------------
    # Search results page — parse all vehicles directly
    # ------------------------------------------------------------------

    _SRP_SELECTOR = ".new-vehicle"

    # After the vehicle cards load, click every "Details" tab so the
    # interior-color elements are rendered into the DOM.
    _CLICK_DETAILS_JS = """
    document.querySelectorAll(
        '.new-vehicle button[aria-label="details tab"]'
    ).forEach(btn => btn.click());
    """

    def _srp_page_methods(self):
        """Playwright page methods for loading and preparing an SRP."""
        return [
            PageMethod("wait_for_selector", self._SRP_SELECTOR, timeout=30_000),
            PageMethod("evaluate", self._CLICK_DETAILS_JS),
            # Give the tab content a moment to render.
            PageMethod("wait_for_timeout", 1_000),
        ]

    async def start(self):
        yield scrapy.Request(
            self.start_url,
            meta={
                "playwright": True,
                "playwright_include_page": True,
                "playwright_page_init_callback": apply_stealth,
                "playwright_page_methods": self._srp_page_methods(),
            },
            callback=self.parse_search,
            errback=self.errback_close_page,
        )

    async def parse_search(self, response: HtmlResponse):
        """Extract vehicle data directly from search result cards.

        DealerInspire embeds a ``data-vehicle`` JSON attribute on each
        vehicle card (``.result-wrap.new-vehicle``) containing the core
        metadata.  Supplementary info (status, TSRP, drivetrain) is
        pulled from DOM elements within the card.
        """
        page = response.meta.get("playwright_page")
        if page:
            await page.close()

        dealer_name = (
            self._dealer_name_override
            or response.css("title::text").get("").split("|")[-1].strip()
        )

        vehicle_cards = response.css(".result-wrap.new-vehicle")
        self.logger.info(
            "Found %d vehicle cards on %s", len(vehicle_cards), response.url,
        )

        for card in vehicle_cards:
            item = self._parse_card(card, dealer_name, response.url)
            if item is not None:
                yield item

        # --- Pagination ---
        next_url = _build_next_page_url(response, len(vehicle_cards))
        if next_url:
            self.logger.info("Following next page: %s", next_url)
            yield scrapy.Request(
                next_url,
                meta={
                    "playwright": True,
                    "playwright_include_page": True,
                    "playwright_page_init_callback": apply_stealth,
                    "playwright_page_methods": self._srp_page_methods(),
                },
                callback=self.parse_search,
                errback=self.errback_close_page,
            )

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(
        self,
        card,
        dealer_name: str,
        page_url: str,
    ) -> CarItem | None:
        """Parse a single vehicle card into a :class:`CarItem`."""
        raw = card.attrib.get("data-vehicle", "")
        if not raw:
            self.logger.warning("Card missing data-vehicle attribute, skipping")
            return None

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self.logger.warning("Failed to parse data-vehicle JSON, skipping")
            return None

        item = CarItem()

        # --- Identifiers ---
        item["vin"] = data.get("vin")
        item["stock_number"] = data.get("stock") or None
        item["model_code"] = None  # not available on SRP

        # --- Vehicle info ---
        item["year"] = str(data.get("year", "")) or None
        item["trim"] = data.get("trim") or None

        # Drivetrain: extract from the card title (e.g. "2026  RAV4 XSE AWD")
        title = card.css(".title-bottom::text").get("")
        item["drivetrain"] = normalize_drivetrain(title)

        # --- Colors ---
        item["exterior_color"] = normalize_color(data.get("ext_color"))
        item["interior_color"] = _extract_color(card, "interior-color")

        # --- Pricing ---
        # data-vehicle carries ``msrp`` and ``price`` — on this platform
        # they are typically the same (the advertised / TSRP value).
        # The DOM ``hit-price`` block mirrors the TSRP.
        msrp = parse_price(data.get("msrp"))
        advertised_price = parse_price(data.get("price"))

        # Fall back to the DOM TSRP if the JSON values are missing.
        if not advertised_price:
            advertised_price = parse_price(
                card.css(".hit-price__value .ashallow::text").get()
            )
        if not msrp:
            msrp = advertised_price

        item["msrp"] = msrp
        item["total_price"] = advertised_price
        item["base_price"] = None  # no package breakdown on SRP
        item["total_packages_price"] = None
        item["dealer_accessories"] = None

        # Adjustments (difference between MSRP and advertised price)
        if msrp and advertised_price and advertised_price != msrp:
            item["adjustments"] = advertised_price - msrp
        else:
            item["adjustments"] = None

        # --- Packages ---
        item["packages"] = None  # not available on SRP

        # --- Status ---
        item["status"] = _extract_status(card)

        # --- Availability date ---
        item["availability_date"] = _extract_availability_date(card)

        # --- Dealer / links ---
        item["dealer_name"] = dealer_name
        item["dealer_url"] = page_url

        detail_href = card.css("a.hit-link::attr(href)").get("")
        item["detail_url"] = urljoin(page_url, detail_href) if detail_href else None

        return item

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def errback_close_page(self, failure):
        page = failure.request.meta.get("playwright_page")
        if page:
            await page.close()
        self.logger.error("Request failed: %s", failure.value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_color(card, testid: str) -> str | None:
    """Extract a colour value from the Details tab by ``data-testid``.

    The rendered text looks like ``"Interior: Black SofTex® [softex]"``.
    This helper strips the label prefix, registered-trademark symbols,
    and bracketed annotations.
    """
    el = card.css(f'[data-testid="{testid}"]')
    if not el:
        return None
    texts = el.css("::text").getall()
    raw = " ".join(t.strip() for t in texts if t.strip())
    if not raw:
        return None
    # Strip "Interior: " / "Exterior: " prefix
    if ":" in raw:
        raw = raw.split(":", 1)[1].strip()
    # Truncate at bracketed annotations like "[softex]" — everything
    # after the bracket (e.g. "Mixed Media") is redundant.
    raw = re.sub(r"\s*\[.*", "", raw)
    return normalize_color(raw)


_AVAIL_DATE_RE = re.compile(
    r"(?:estimated\s+)?availability\s+(\d{1,2}/\d{1,2}/\d{2,4})"
    r"(?:\s*-\s*(\d{1,2}/\d{1,2}/\d{2,4}))?",
    re.IGNORECASE,
)


def _extract_availability_date(card) -> str | None:
    """Extract an estimated availability date from the card's disclaimer.

    The disclaimer text may contain a date or date-range like:

    - ``Estimated availability 02/14/26-02/28/26.``
    - ``Estimated availability 03/01/26.``

    When a range is present the full range is returned (e.g.
    ``"02/14/26-02/28/26"``).  Returns ``None`` when no date is found
    (e.g. "Contact dealer to confirm availability date.").
    """
    disclaimer = card.css(".disclaimer::text").get("")
    m = _AVAIL_DATE_RE.search(disclaimer)
    if not m:
        return None
    if m.group(2):
        return f"{m.group(1)}-{m.group(2)}"
    return m.group(1)


def _extract_status(card) -> str | None:
    """Determine vehicle availability status from the card's DOM.

    DealerInspire displays a status badge inside ``.hit-status`` with
    values like "In Transit" or "Build Phase".
    """
    status_text = card.css(".hit-status span:first-child::text").get("")
    status_text = status_text.strip()
    if not status_text:
        return None
    # Normalise common labels
    lower = status_text.lower()
    if "transit" in lower:
        return "In Transit"
    if "build" in lower:
        return "Build Phase"
    if "stock" in lower:
        return "In Stock"
    if "sale pending" in lower:
        return "Sale Pending"
    return status_text


def _extract_hits_per_page(response: HtmlResponse) -> int:
    """Extract the Algolia ``hitsPerPage`` value from inline scripts.

    Falls back to 20 (the Algolia default) if not found.
    """
    for script in response.css("script::text").getall():
        m = re.search(r'"hitsPerPage"\s*:\s*"?(\d+)"?', script)
        if m:
            return int(m.group(1))
    return 20


def _build_next_page_url(response: HtmlResponse, card_count: int) -> str | None:
    """Build the URL for the next SRP page, or return *None* on the last page.

    DealerInspire uses a ``_p`` query parameter for zero-indexed
    pagination (page 0 is the first page, ``_p=1`` is the second, etc.).
    If the current page has fewer cards than ``hitsPerPage``, we're on
    the last page.
    """
    hits_per_page = _extract_hits_per_page(response)
    if card_count < hits_per_page:
        return None

    parsed = urlparse(response.url)
    qs = parse_qs(parsed.query)

    current_page = int(qs.get("_p", ["0"])[0])
    qs["_p"] = [str(current_page + 1)]

    new_query = urlencode({k: v[0] for k, v in qs.items()}, safe="%")
    return urlunparse(parsed._replace(query=new_query))
