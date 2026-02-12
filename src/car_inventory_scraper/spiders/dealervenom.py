"""Spider for DealerVenom-powered dealership websites.

DealerVenom is a dealership website platform that uses Algolia InstantSearch
for its inventory search pages.  URLs typically follow the pattern
``/new-vehicles/`` with query parameters for filters (``model``, ``yr``,
etc.).  Vehicle cards are rendered client-side and wrapped in
``.listing-container`` elements with ``data-url`` attributes pointing to
the vehicle detail page (VDP).

The VDP embeds structured vehicle data in three places:

1. **``data-vehicle``** attribute — a JSON object on the ``<main>`` element
   with VIN, stock number, year, make, model, trim, colors, drivetrain,
   MSRP, and displayed price.
2. **JSON-LD** (``<script type="application/ld+json">``) — standard
   ``Car``/``Product`` schema with model code (``mpn``), body type,
   fuel economy, and an ``offers`` block.
3. **DOM elements** — rendered packages (``.vdp-package-item``),
   pricing stack (``.buy-price``), availability dates, and in-transit
   status (``.in-transit-vehicle``).

The SRP is rendered client-side by Algolia, but pagination uses
standard ``<a href>`` links with a ``pg`` query parameter, so each
page is fetched as an independent Playwright request.

Example usage::

    car-inventory-scraper crawl dealervenom \\
        --url "https://www.burientoyota.com/new-vehicles/?model=RAV4&yr=2026"
"""

from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin

import scrapy
from scrapy.http import HtmlResponse
from scrapy_playwright.page import PageMethod

from car_inventory_scraper.parsing_helpers import (
    EXCLUDED_PACKAGES,
    extract_json_ld_car,
    normalize_drivetrain,
    parse_price,
    safe_int,
)
from car_inventory_scraper.items import CarItem
from car_inventory_scraper.stealth import apply_stealth


class DealerVenomSpider(scrapy.Spider):
    """Scrape vehicle inventory from a DealerVenom-powered dealership site."""

    name = "dealervenom"

    # Passed via ``-a url=…`` or the CLI wrapper.
    def __init__(self, url: str | None = None, dealer_name: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if url is None:
            raise ValueError(
                "A starting URL is required. "
                "Pass it with: -a url=https://example.com/new-vehicles/"
            )
        self.start_url = url
        self._dealer_name_override = dealer_name

    # ------------------------------------------------------------------
    # Search results page — collect detail links
    # ------------------------------------------------------------------

    _SRP_SELECTOR = ".listing-container"

    async def start(self):
        yield scrapy.Request(
            self.start_url,
            meta={
                "playwright": True,
                "playwright_page_init_callback": apply_stealth,
                "playwright_page_methods": [
                    PageMethod("wait_for_selector", self._SRP_SELECTOR, timeout=30_000),
                ],
                # DealerVenom sites use heavy analytics that prevent
                # networkidle from resolving; domcontentloaded + selector
                # wait is sufficient.
                "playwright_page_goto_kwargs": {
                    "wait_until": "domcontentloaded",
                },
            },
            callback=self.parse_search,
            errback=self.errback_close_page,
        )

    async def parse_search(self, response: HtmlResponse):
        """Extract vehicle detail URLs from the Algolia-rendered SRP.

        DealerVenom uses URL-based pagination via a ``pg`` query parameter.
        Each page is fetched as an independent Playwright request and the
        "next page" link (identified by its right-chevron icon) is
        followed automatically.
        """
        dealer_name = response.meta.get("dealer_name") or (
            self._dealer_name_override
            or response.css("title::text").get("").split("|")[-1].strip()
        )

        detail_urls = _extract_detail_urls(response, response.url)
        self.logger.info(
            "Found %d vehicle detail URLs on %s",
            len(detail_urls), response.url,
        )

        for detail_url in detail_urls:
            yield scrapy.Request(
                detail_url,
                meta={
                    "playwright": True,
                    "playwright_include_page": True,
                    "playwright_page_init_callback": apply_stealth,
                    "playwright_page_goto_kwargs": {
                        "wait_until": "domcontentloaded",
                    },
                    "playwright_page_methods": [
                        PageMethod("wait_for_timeout", 3_000),
                    ],
                    "dealer_name": dealer_name,
                    "dealer_url": self.start_url,
                },
                callback=self.parse_detail,
                errback=self.errback_close_page,
            )

        # --- URL-based pagination ---
        next_url = _extract_next_page_url(response)
        if next_url:
            self.logger.info("Following next page: %s", next_url)
            yield scrapy.Request(
                next_url,
                meta={
                    "playwright": True,
                    "playwright_page_init_callback": apply_stealth,
                    "playwright_page_methods": [
                        PageMethod(
                            "wait_for_selector",
                            self._SRP_SELECTOR,
                            timeout=30_000,
                        ),
                    ],
                    "playwright_page_goto_kwargs": {
                        "wait_until": "domcontentloaded",
                    },
                    "dealer_name": dealer_name,
                },
                callback=self.parse_search,
                errback=self.errback_close_page,
            )

    # ------------------------------------------------------------------
    # Vehicle detail page — extract all information
    # ------------------------------------------------------------------

    async def parse_detail(self, response: HtmlResponse):
        """Extract full vehicle details from a DealerVenom VDP.

        DealerVenom VDP pages embed structured vehicle data in a
        ``data-vehicle`` JSON attribute, JSON-LD (``@type: Car``), and
        rendered DOM elements for packages and pricing.
        """
        page = response.meta.get("playwright_page")
        if page:
            await page.close()

        item = CarItem()
        item["detail_url"] = response.url
        item["dealer_name"] = response.meta.get("dealer_name", "")
        item["dealer_url"] = response.meta.get("dealer_url", "")

        # --- Primary data source: data-vehicle JSON attribute ---
        vehicle_data = _extract_vehicle_data(response)
        json_ld = extract_json_ld_car(response)

        # --- Core vehicle identifiers ---
        item["vin"] = vehicle_data.get("vin") or json_ld.get("vehicleIdentificationNumber")
        item["stock_number"] = vehicle_data.get("stockNumber") or json_ld.get("sku")
        # DealerVenom stores model code as ``mpn`` in JSON-LD
        item["model_code"] = json_ld.get("mpn")

        # --- Vehicle info ---
        item["year"] = (
            str(vehicle_data["year"]) if vehicle_data.get("year")
            else json_ld.get("vehicleModelDate")
        )
        item["trim"] = (
            vehicle_data.get("trim")
            or json_ld.get("vehicleConfiguration")
        )

        # --- Drivetrain ---
        drivetrain_raw = vehicle_data.get("drivetrain", "")
        item["drivetrain"] = normalize_drivetrain(drivetrain_raw, item.get("trim", ""))

        # --- Colors ---
        item["exterior_color"] = (
            vehicle_data.get("exteriorColor")
            or json_ld.get("color")
        )
        item["interior_color"] = (
            vehicle_data.get("interiorColor")
            or json_ld.get("vehicleInteriorColor")
        )

        # --- Packages ---
        packages: list[dict[str, str | None]] = []
        for pkg_el in response.css(".vdp-package-item"):
            name = pkg_el.css(".vdp-package-name::text").get("").strip()
            price_str = pkg_el.css(".vdp-package-price::text").get("").strip()
            if name and name.upper() not in EXCLUDED_PACKAGES:
                packages.append({
                    "name": name,
                    "price": price_str or None,
                })
        item["packages"] = packages or None

        # --- Pricing ---
        msrp = (
            safe_int(vehicle_data.get("msrp"))
            or parse_price(json_ld.get("offers", [{}])[0].get("price")
                            if isinstance(json_ld.get("offers"), list)
                            else (json_ld.get("offers") or {}).get("price"))
        )
        item["msrp"] = msrp

        total_packages_price = sum(
            parse_price(p.get("price")) or 0 for p in packages
        )
        item["total_packages_price"] = total_packages_price or None
        item["base_price"] = (msrp - total_packages_price) if msrp else None

        # Displayed / advertised price
        displayed_price = safe_int(vehicle_data.get("displayedPrice"))
        if not displayed_price:
            displayed_price = parse_price(
                response.css(".buy-price::text").get("")
            )
        item["total_price"] = displayed_price if displayed_price else msrp

        # Dealer accessories — not broken out separately on this platform
        item["dealer_accessories"] = None

        # Adjustments = difference between total price and MSRP
        if msrp and item["total_price"] and item["total_price"] != msrp:
            item["adjustments"] = item["total_price"] - msrp
        else:
            item["adjustments"] = None

        # --- Status ---
        # Determine the base stock status from DOM elements.
        if response.css(".in-transit-vehicle"):
            status = "In Transit"
        elif response.css(".allocated-vehicle"):
            # "Allocated" vehicles are in the build/production phase and
            # have not yet entered transit.
            status = "In Production"
        elif response.css(".in-stock-vehicle"):
            status = "In Stock"
        else:
            # Fall back to data-vehicle status field
            status_raw = vehicle_data.get("status", "")
            status = _normalize_dv_status(status_raw)

        # Check for "Sale Pending" prefix in the disclaimer / status text.
        status_text = " ".join(response.css(
            ".dv-sp-disclaimer::text, "
            ".vdp-allocated-veh-text::text"
        ).getall())
        if re.search(r"(?i)sale\s+pending", status_text):
            status = f"Sale Pending - {status}" if status else "Sale Pending"

        item["status"] = status

        # --- Availability date ---
        avail_text = " ".join(response.css(
            ".dv-sp-disclaimer::text, "
            ".vdp-allocated-veh-text::text"
        ).getall())
        avail_match = re.search(
            r"(?i)(?:Estimated\s+)?availability\s+(\d{2}/\d{2}/\d{2,4})",
            avail_text,
        )
        item["availability_date"] = avail_match.group(1) if avail_match else None

        yield item

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def errback_close_page(self, failure):
        page = failure.request.meta.get("playwright_page")
        if page:
            await page.close()
        self.logger.error("Request failed: %s", failure.value)


# ---------------------------------------------------------------------------
# Helpers — SRP extraction
# ---------------------------------------------------------------------------

def _extract_detail_urls(response: HtmlResponse, base_url: str) -> list[str]:
    """Extract vehicle detail URLs from ``.listing-container[data-url]`` elements."""
    urls: list[str] = []
    for container in response.css(".listing-container[data-url]"):
        href = container.attrib.get("data-url", "")
        if href:
            urls.append(urljoin(base_url, href))
    return urls


def _extract_next_page_url(response: HtmlResponse) -> str | None:
    """Return the URL of the next SRP page, or *None* if on the last page.

    DealerVenom renders a custom pagination widget inside
    ``ul.ais-pagination-ul``.  The "next" link is an ``<a>`` tag that
    wraps an ``<i class="fa-solid fa-chevron-right">`` icon.
    """
    next_href = response.xpath(
        '//ul[contains(@class,"ais-pagination-ul")]'
        '//a[contains(@class,"ais-Pagination-link")]'
        '[.//i[contains(@class,"fa-chevron-right")]]'
        '/@href'
    ).get()
    if next_href:
        return urljoin(response.url, next_href)
    return None


# ---------------------------------------------------------------------------
# Helpers — VDP data extraction
# ---------------------------------------------------------------------------

def _extract_vehicle_data(response: HtmlResponse) -> dict:
    """Extract the ``data-vehicle`` JSON attribute from the VDP.

    DealerVenom encodes the vehicle data object as HTML-entity-escaped
    JSON in a ``data-vehicle`` attribute (typically on the analytics
    wrapper or ``<main>`` element).
    """
    raw = response.css("[data-vehicle]::attr(data-vehicle)").get("")
    if not raw:
        # Fallback: search in the raw HTML for the encoded attribute
        match = re.search(r'data-vehicle="(\{.*?\})"', response.text)
        if match:
            raw = unescape(match.group(1))
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return {}



def _normalize_dv_status(raw: str) -> str | None:
    """Map a condition string like ``New`` to a status label.

    DealerVenom stores the *condition* (New/Used) in ``data-vehicle``,
    not the stock status.  The actual in-transit / in-stock status is
    determined from DOM elements in the caller.
    """
    if not raw or raw.lower() in ("new", "used"):
        return None
    return raw
