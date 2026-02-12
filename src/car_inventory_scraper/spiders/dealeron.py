"""Spider for DealerOn-powered dealership websites.

DealerOn is a common platform used by many Toyota, Honda, Ford (and other)
dealerships.  Inventory pages live at paths like ``/searchnew.aspx`` and render
vehicle cards whose detail links this spider follows to scrape full vehicle
information.

Example usage::

    car-inventory-scraper crawl dealeron \
        --url "https://www.toyotaofbellevue.com/searchnew.aspx?Make=Toyota"
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import scrapy
from scrapy.http import HtmlResponse
from scrapy_playwright.page import PageMethod

from car_inventory_scraper.parsing_helpers import (
    EXCLUDED_PACKAGES,
    normalize_pkg_name,
    parse_price,
)
from car_inventory_scraper.items import CarItem
from car_inventory_scraper.stealth import apply_stealth


class DealerOnSpider(scrapy.Spider):
    """Scrape vehicle inventory from a DealerOn-powered dealership site."""

    name = "dealeron"

    # Passed via ``-a url=…`` or the CLI wrapper.
    def __init__(self, url: str | None = None, dealer_name: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if url is None:
            raise ValueError(
                "A starting URL is required. "
                "Pass it with: -a url=https://example.com/searchnew.aspx"
            )
        self.start_url = url
        self._dealer_name_override = dealer_name

    # ------------------------------------------------------------------
    # Search results page — collect detail links
    # ------------------------------------------------------------------

    _SRP_SELECTOR = ".vehicle-card, .vehicleCard, .srp-vehicle-card"
    _VDP_SELECTOR = ".vdp[data-vehicle-information], .vehicle-info, .vdp-content"

    async def start(self):
        yield scrapy.Request(
            self.start_url,
            meta={
                "playwright": True,
                "playwright_include_page": True,
                "playwright_page_init_callback": apply_stealth,
                "playwright_page_methods": [
                    PageMethod("wait_for_selector", self._SRP_SELECTOR, timeout=15_000),
                ],
            },
            callback=self.parse_search,
            errback=self.errback_close_page,
        )

    async def parse_search(self, response: HtmlResponse):
        """Parse the search results page and follow each vehicle detail link."""
        page = response.meta.get("playwright_page")
        if page:
            await page.close()

        base_url = response.url
        dealer_name = (
            self._dealer_name_override
            or response.css("title::text").get("").split("|")[-1].strip()
        )

        # Collect detail-page URLs from vehicle cards
        vehicle_cards = response.css(
            ".vehicle-card, .vehicleCard, .srp-vehicle-card, "
            "[data-vehicle-card], .vehicle-card-details-container"
        )
        self.logger.info("Found %d vehicle cards on %s", len(vehicle_cards), response.url)

        seen = set()
        for card in vehicle_cards:
            title_link = card.css(
                "a[href*='/new-'], a[href*='/used-'], "
                ".vehicle-card__title a, h2 a, .vehicleCardTitle a"
            )
            href = title_link.attrib.get("href", "")
            if not href:
                continue
            detail_url = urljoin(base_url, href)
            if detail_url in seen:
                continue
            seen.add(detail_url)

            yield scrapy.Request(
                detail_url,
                meta={
                    "playwright": True,
                    "playwright_include_page": True,
                    "playwright_page_init_callback": apply_stealth,
                    "playwright_page_methods": [
                        PageMethod("wait_for_selector", self._VDP_SELECTOR, timeout=15_000),
                    ],
                    "dealer_name": dealer_name,
                    "dealer_url": base_url,
                },
                callback=self.parse_detail,
                errback=self.errback_close_page,
            )

        # --- Pagination ---
        # DealerOn SRP 2.0 renders pagination links with href="#" and
        # handles page changes client-side.  The actual page number is
        # communicated via a ``pt`` query parameter in the URL (e.g.
        # ``&pt=2``).  We detect whether a non-disabled "Next" button
        # exists and, if so, build the next page URL ourselves.
        next_url = _build_next_page_url(response)
        if next_url:
            self.logger.info("Following next page: %s", next_url)
            yield scrapy.Request(
                next_url,
                meta={
                    "playwright": True,
                    "playwright_include_page": True,
                    "playwright_page_init_callback": apply_stealth,
                    "playwright_page_methods": [
                        PageMethod("wait_for_selector", self._SRP_SELECTOR, timeout=15_000),
                    ],
                },
                callback=self.parse_search,
                errback=self.errback_close_page,
            )

    # ------------------------------------------------------------------
    # Vehicle detail page — extract all information
    # ------------------------------------------------------------------

    async def parse_detail(self, response: HtmlResponse):
        """Extract full vehicle details from a VDP (Vehicle Detail Page).

        DealerOn VDP pages store all structured vehicle data as ``data-*``
        attributes on the main ``.vdp[data-vehicle-information]`` container,
        which is far more reliable than scraping rendered text.
        """
        page = response.meta.get("playwright_page")
        if page:
            await page.close()

        item = CarItem()
        item["detail_url"] = response.url
        item["dealer_name"] = response.meta.get("dealer_name", "")
        item["dealer_url"] = response.meta.get("dealer_url", "")

        # The main VDP container holds all vehicle metadata as data attrs
        vdp = response.css(".vdp[data-vehicle-information]")
        if not vdp:
            # Fallback: try any element with data-vin
            vdp = response.css("[data-vin]")
        vdp = vdp[0] if vdp else response

        # --- Core vehicle info from data attributes ---
        item["vin"] = vdp.attrib.get("data-vin")
        item["stock_number"] = vdp.attrib.get("data-stocknum") or None
        item["model_code"] = vdp.attrib.get("data-modelcode")
        item["year"] = vdp.attrib.get("data-year")
        item["trim"] = vdp.attrib.get("data-trim")
        item["exterior_color"] = _strip_html(vdp.attrib.get("data-extcolor", ""))
        item["interior_color"] = _strip_html(vdp.attrib.get("data-intcolor", ""))

        # Drivetrain: prefer Highlighted Features section, fall back to data-name
        drivetrain = None
        for feature in response.css(".vehicle-highlights__label::text").getall():
            feature_upper = feature.strip().upper()
            for token in ("AWD", "4WD", "FWD", "RWD", "4X4", "4X2"):
                if token in feature_upper:
                    drivetrain = feature.strip()
                    break
            if drivetrain:
                break

        if not drivetrain:
            name = vdp.attrib.get("data-name", "")
            for token in ("AWD", "4WD", "FWD", "RWD", "4x4", "4x2"):
                if token in name.upper():
                    drivetrain = token
                    break

        item["drivetrain"] = drivetrain

        # --- Packages & Accessories (CSS class: .package-info) ---
        packages = []
        dealer_acc_packages: list[dict[str, str | None]] = []
        for pkg in response.css(".package-info"):
            pkg_name = pkg.css(".package-info__name::text").get("").strip()
            price_str = pkg.css(".package-info__price::text").get("").strip()
            if not pkg_name or pkg_name.upper() in EXCLUDED_PACKAGES:
                continue
            entry = {"name": pkg_name, "price": price_str or None}
            if _is_dealer_accessory(pkg_name):
                dealer_acc_packages.append(entry)
            else:
                packages.append(entry)
        item["packages"] = packages or None

        dealer_acc_total = sum(
            parse_price(p.get("price")) or 0 for p in dealer_acc_packages
        )
        item["dealer_accessories"] = dealer_acc_total or None

        # --- Pricing from price stack ---
        price_stack = {}
        for pi in response.css(
            ".priceBlockResponsiveDesktop .priceBlockItemPrice"
        ):
            label = pi.css(".priceBlocItemPriceLabel::text").get("").strip().rstrip(":")
            val = pi.css(".priceBlocItemPriceValue::text").get("").strip()
            if label and val:
                price_stack[label.upper()] = val

        msrp = (
            parse_price(price_stack.get("TSRP"))
            or parse_price(price_stack.get("MSRP"))
            or parse_price(vdp.attrib.get("data-msrp"))
        )
        item["msrp"] = msrp

        total_packages_price = sum(
            parse_price(p.get("price")) or 0 for p in packages
        )
        item["total_packages_price"] = total_packages_price or None
        item["base_price"] = (msrp - total_packages_price) if msrp else None

        total_price = parse_price(price_stack.get("PRICE"))
        if not total_price:
            total_price = parse_price(vdp.attrib.get("data-price"))
        item["total_price"] = total_price if total_price else msrp

        # Adjustments = difference between total price and sticker, minus
        # dealer accessories (which are already broken out separately).
        if msrp and item["total_price"] and item["total_price"] != msrp:
            adj = item["total_price"] - msrp - (dealer_acc_total or 0)
            item["adjustments"] = adj if adj else None
        else:
            item["adjustments"] = None

        # --- Status from data attributes ---
        if vdp.attrib.get("data-instock", "").lower() == "true":
            item["status"] = "In Stock"
        elif vdp.attrib.get("data-intransit", "").lower() == "true":
            item["status"] = "In Transit"
        elif vdp.attrib.get("data-inproduction", "").lower() == "true":
            item["status"] = "In Production"
        else:
            item["status"] = None

        # --- Availability date ---
        avail_parts = response.css("#price-stack .inTransitDisclaimer::text").getall()
        avail_text = " ".join(avail_parts)
        avail = re.search(
            r"(?i)(?:estimated\s+)?availability\s+(\d{2}/\d{2}/\d{2,4})", avail_text
        )
        item["availability_date"] = avail.group(1) if avail else None

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
# Helpers — dealer-installed accessories detection
# ---------------------------------------------------------------------------

# Package names (normalised to lowercase) that are dealer-installed
# accessories rather than factory packages.  These are excluded from the
# packages list / total and counted under dealer_accessories & adjustments.
_DEALER_ACCESSORY_NAMES: set[str] = {
    "360shield -paintshield and interiorshield",
    "z360shield -paintshield and interiorshield",
}


def _is_dealer_accessory(name: str) -> bool:
    """Return ``True`` if *name* matches a known dealer-installed accessory."""
    return normalize_pkg_name(name).lower() in _DEALER_ACCESSORY_NAMES


# ---------------------------------------------------------------------------
# Helpers — data cleaning
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<a\b[^>]*>.*?</a>|<[^>]+>", re.DOTALL)
_BRACKET_TAG_RE = re.compile(r"\s*\[Extra_Cost_Color\]", re.IGNORECASE)


def _strip_html(value: str) -> str | None:
    """Remove HTML tags and metadata annotations from a data-attribute value.

    DealerOn ``data-intcolor`` / ``data-extcolor`` attributes may contain:

    * Disclaimer anchor elements (``<a …><sup>60</sup></a>``)
    * Bracket-enclosed metadata like ``[Extra_Cost_Color]``

    This helper strips both, returning only the clean color name.
    """
    if not value:
        return None
    cleaned = _HTML_TAG_RE.sub("", value)
    cleaned = _BRACKET_TAG_RE.sub("", cleaned).strip()
    return cleaned or None


# ---------------------------------------------------------------------------
# Helpers — pagination
# ---------------------------------------------------------------------------

def _build_next_page_url(response: HtmlResponse) -> str | None:
    """Build the URL for the next SRP page, or return *None* on the last page.

    DealerOn SRP 2.0 uses a Vue-rendered pagination widget
    (``.srp-pagination``) where every ``<a>`` has ``href="#"`` — page
    changes are handled via JavaScript.  The actual page number is
    passed as the ``pt`` query-string parameter.

    This helper checks for a non-disabled "Next" pagination item and,
    when found, increments ``pt`` in the current URL to produce the
    next page URL.
    """
    # The "Next" <li> has class ``pagination__item--next``.
    # When on the last page it also carries the ``disabled`` class.
    next_li = response.css("li.pagination__item--next")
    if not next_li:
        return None
    li_classes = next_li[0].attrib.get("class", "")
    if "disabled" in li_classes:
        return None

    # Determine the current page number from the ``pt`` query parameter.
    parsed = urlparse(response.url)
    qs = parse_qs(parsed.query)
    current_page = int(qs.get("pt", ["1"])[0])

    # Build the next page URL by setting ``pt`` to current + 1.
    qs["pt"] = [str(current_page + 1)]
    new_query = urlencode({k: v[0] for k, v in qs.items()})
    next_url = urlunparse(parsed._replace(query=new_query))
    return next_url

