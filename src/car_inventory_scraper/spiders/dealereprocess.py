"""Spider for Dealer eProcess (DEP) / DealerInspire dealership websites.

Dealer eProcess is a dealership website platform that serves many Toyota
(and other brand) dealerships.  Inventory search pages live at paths like
``/search/new-…`` and render JavaScript-heavy vehicle cards via the DEP
``dep_require`` module system.  Vehicle detail pages embed JSON-LD
``Vehicle`` schema data as well as a ``data-vehicle`` attribute with
URL-encoded JSON metadata.

The platform uses Cloudflare protection which typically requires a
non-headless browser and anti-automation-detection flags to bypass.

Example usage::

    car-inventory-scraper crawl dealereprocess \\
        --url "https://www.doxontoyota.com/search/new-2026-toyota-rav4-auburn-wa/"
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
    EXCLUDED_PACKAGES,
    format_pkg_price,
    normalize_color,
    normalize_drivetrain,
    normalize_pkg_name,
    parse_price,
)
from car_inventory_scraper.stealth import apply_stealth


class DealerEprocessSpider(scrapy.Spider):
    """Scrape vehicle inventory from a Dealer eProcess-powered dealership site."""

    name = "dealereprocess"

    # Passed via ``-a url=…`` or the CLI wrapper.
    def __init__(self, url: str | None = None, dealer_name: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if url is None:
            raise ValueError(
                "A starting URL is required. "
                "Pass it with: -a url=https://example.com/search/new-…"
            )
        self.start_url = url
        self._dealer_name_override = dealer_name

    # ------------------------------------------------------------------
    # Search results page — collect detail links
    # ------------------------------------------------------------------

    _SRP_SELECTOR = ".vehicle_item"
    _VDP_SELECTOR = ".vdp_content, .veh_pricing_container, .bolded_label_value"

    async def start(self):
        yield scrapy.Request(
            self.start_url,
            meta={
                "playwright": True,
                "playwright_page_init_callback": apply_stealth,
                "playwright_page_methods": [
                    PageMethod("wait_for_selector", self._SRP_SELECTOR),
                ],
            },
            callback=self.parse_search,
            errback=self.errback,
        )

    async def parse_search(self, response: HtmlResponse):
        """Parse the search results page and follow each vehicle detail link."""
        base_url = response.url
        dealer_name = (
            self._dealer_name_override
            or response.css("title::text").get("").split(" - ")[-1].strip()
        )

        # Collect detail-page URLs from vehicle cards
        vehicle_cards = response.css(".vehicle_item")
        self.logger.info("Found %d vehicle cards on %s", len(vehicle_cards), response.url)

        seen: set[str] = set()
        for card in vehicle_cards:
            # Primary: dedicated vehicle link element
            href = card.css(".vehicle_item__vehicle_link::attr(href)").get("")
            if not href:
                # Fallback: title link inside the card
                href = card.css("h2.vehicle_title a::attr(href)").get("")
            if not href:
                # Final fallback: data-vehicle JSON has vdpHref
                href = _extract_vdp_href_from_data(card)
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
                    "playwright_page_init_callback": apply_stealth,
                    "playwright_page_methods": [
                        PageMethod("wait_for_selector", self._VDP_SELECTOR),
                    ],
                    "dealer_name": dealer_name,
                    "dealer_url": base_url,
                },
                callback=self.parse_detail,
                errback=self.errback,
            )

        # --- Pagination ---
        next_url = _build_next_page_url(response)
        if next_url:
            self.logger.info("Following next page: %s", next_url)
            yield scrapy.Request(
                next_url,
                meta={
                    "playwright": True,
                    "playwright_page_init_callback": apply_stealth,
                    "playwright_page_methods": [
                        PageMethod("wait_for_selector", self._SRP_SELECTOR),
                    ],
                },
                callback=self.parse_search,
                errback=self.errback,
            )

    # ------------------------------------------------------------------
    # Vehicle detail page — extract all information
    # ------------------------------------------------------------------

    async def parse_detail(self, response: HtmlResponse):
        """Extract full vehicle details from a DEP Vehicle Detail Page.

        DEP VDPs embed structured data in three main sources:

        1. **JSON-LD** ``<script type="application/ld+json">`` blocks with
           ``@type: Vehicle`` — contains VIN, stock number, year, model, trim,
           colors, price, engine, transmission, and fuel economy.
        2. **``data-vehicle``** URL-encoded JSON attribute — contains model code,
           marketing name, VIN, year, and pricing.
        3. **DOM elements** — pricing container (``.veh_pricing_container``),
           installed options (``.installed_options__item``), stock/VIN
           (``.bolded_label_value``), and the page title.
        """
        item = CarItem()
        item["detail_url"] = response.url
        item["dealer_name"] = response.meta.get("dealer_name", "")
        item["dealer_url"] = response.meta.get("dealer_url", "")

        # --- JSON-LD Vehicle data ---
        json_ld = _extract_json_ld_vehicle(response)

        # --- data-vehicle JSON ---
        data_vehicle = _extract_data_vehicle(response)

        # --- Core vehicle info ---
        item["vin"] = (
            json_ld.get("vehicleIdentificationNumber")
            or data_vehicle.get("vin")
            or _bolded_label_value(response, "VIN")
        )
        item["stock_number"] = (
            json_ld.get("sku")
            or _bolded_label_value(response, "STOCK")
        )
        item["model_code"] = data_vehicle.get("modelCd") or None
        item["year"] = json_ld.get("vehicleModelDate") or str(data_vehicle.get("year", "")) or None
        item["trim"] = json_ld.get("vehicleConfiguration") or None

        # --- Colors ---
        item["exterior_color"] = normalize_color(json_ld.get("color"))
        item["interior_color"] = normalize_color(json_ld.get("vehicleInteriorColor"))

        # --- Drivetrain ---
        # DEP stores drivetrain in the page title / name field rather than
        # a dedicated JSON-LD property.
        name = json_ld.get("name", "")
        engine_name = ""
        engine_spec = json_ld.get("vehicleEngine")
        if isinstance(engine_spec, dict):
            engine_name = engine_spec.get("name", "")
        item["drivetrain"] = normalize_drivetrain(name, engine_name)

        # --- Packages / installed options ---
        packages = []
        dealer_acc_packages: list[dict[str, str | None]] = []
        for opt in response.css(".installed_options__item"):
            opt_name = opt.css(".installed_options__title::text").get("").strip()
            opt_price_raw = opt.css(".installed_options__cost::text").get("").strip()
            if not opt_name:
                continue
            if opt_name.upper() in EXCLUDED_PACKAGES:
                continue
            opt_price = format_pkg_price(opt_price_raw) if opt_price_raw else None
            pkg = {"name": normalize_pkg_name(opt_name), "price": opt_price}
            if _is_dealer_accessory(opt_name):
                dealer_acc_packages.append(pkg)
            else:
                packages.append(pkg)
        item["packages"] = packages or None

        total_packages_price = sum(
            parse_price(p.get("price")) or 0 for p in packages
        )
        item["total_packages_price"] = total_packages_price or None

        # --- Pricing ---
        # TSRP / MSRP from the pricing container
        msrp = _extract_pricing_label(response, "TSRP")
        if not msrp:
            msrp = _extract_pricing_label(response, "MSRP")
        if not msrp:
            # Fall back to JSON-LD offers price
            msrp = _json_ld_offer_price(json_ld)
        item["msrp"] = msrp
        item["base_price"] = (msrp - total_packages_price) if msrp else None

        # Total / advertised price — look for an e-price or advertised
        # price label first, then fall back to MSRP.
        total_price = _extract_pricing_label(response, "PRICE")
        if not total_price:
            total_price = _extract_pricing_label(response, "EPRICE")
        if not total_price:
            total_price = parse_price(data_vehicle.get("advertisedPrice"))
        if not total_price:
            total_price = parse_price(data_vehicle.get("ePrice"))
        item["total_price"] = total_price if total_price else msrp

        # Adjustments
        if msrp and item["total_price"] and item["total_price"] != msrp:
            item["adjustments"] = item["total_price"] - msrp
        else:
            item["adjustments"] = None

        # Dealer accessories
        dealer_acc_total = sum(
            parse_price(p.get("price")) or 0 for p in dealer_acc_packages
        )
        item["dealer_accessories"] = dealer_acc_total or None

        # --- Status ---
        item["status"] = _extract_status(response, json_ld)

        # --- Availability date ---
        item["availability_date"] = _extract_availability_date(response)

        yield item

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def errback(self, failure):
        self.logger.error("Request failed: %s", failure.value)


# ---------------------------------------------------------------------------
# Helpers — dealer-installed accessories detection
# ---------------------------------------------------------------------------

# Package names (normalised to lowercase) that are dealer-installed
# accessories rather than factory packages.  These are excluded from the
# packages list / total and counted under dealer_accessories & adjustments.
_DEALER_ACCESSORY_NAMES: set[str] = {
    "pulse",
}


def _is_dealer_accessory(name: str) -> bool:
    """Return ``True`` if *name* matches a known dealer-installed accessory."""
    return normalize_pkg_name(name).lower() in _DEALER_ACCESSORY_NAMES


# ---------------------------------------------------------------------------
# Helpers — JSON-LD
# ---------------------------------------------------------------------------

def _extract_json_ld_vehicle(response: HtmlResponse) -> dict:
    """Return the first JSON-LD block with ``@type`` of ``Vehicle``, or ``{}``."""
    for script in response.css('script[type="application/ld+json"]::text').getall():
        try:
            data = json.loads(script)
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            obj_type = obj.get("@type", "")
            if isinstance(obj_type, list):
                if "Vehicle" in obj_type:
                    return obj
            elif obj_type == "Vehicle":
                return obj
    return {}


def _json_ld_offer_price(json_ld: dict) -> int | None:
    """Extract the numeric price from a JSON-LD ``offers`` block."""
    offers = json_ld.get("offers")
    if isinstance(offers, dict):
        return parse_price(offers.get("price"))
    if isinstance(offers, list) and offers:
        return parse_price(offers[0].get("price"))
    return None


# ---------------------------------------------------------------------------
# Helpers — data-vehicle attribute
# ---------------------------------------------------------------------------

def _extract_data_vehicle(response: HtmlResponse) -> dict:
    """Decode the first ``data-vehicle`` URL-encoded JSON attribute on the page."""
    import urllib.parse

    raw = response.css("[data-vehicle]::attr(data-vehicle)").get("")
    if not raw:
        return {}
    try:
        return json.loads(urllib.parse.unquote(raw))
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_vdp_href_from_data(card) -> str | None:
    """Extract ``vdpHref`` from a card's ``data-vehicle`` attribute."""
    import urllib.parse

    raw = card.css("[data-vehicle]::attr(data-vehicle)").get("")
    if not raw:
        return None
    try:
        data = json.loads(urllib.parse.unquote(raw))
        return data.get("vdpHref")
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Helpers — bolded label/value pairs
# ---------------------------------------------------------------------------

def _bolded_label_value(response: HtmlResponse, label: str) -> str | None:
    """Extract the value from a ``.bolded_label_value`` block with the given label.

    DEP VDPs display stock number and VIN in ``<div class="bolded_label_value">``
    elements with structure::

        <span class="bolded_label_value__label">STOCK</span>
        <span>41631</span>
    """
    for el in response.css(".bolded_label_value"):
        lbl = el.css(".bolded_label_value__label::text").get("").strip()
        if lbl.upper() == label.upper():
            # The value is in the next <span> sibling
            val = el.css("span:not(.bolded_label_value__label)::text").get("").strip()
            return val or None
    return None


# ---------------------------------------------------------------------------
# Helpers — pricing
# ---------------------------------------------------------------------------

def _extract_pricing_label(response: HtmlResponse, label: str) -> int | None:
    """Extract a price value from the pricing container by its ``<dt>`` label.

    DEP VDPs display pricing as ``<dl>`` definition lists inside
    ``.veh_pricing_container``::

        <dl>
          <dt>TSRP</dt>
          <dd>$45,703</dd>
        </dl>
    """
    for dl in response.css(".veh_pricing_container dl"):
        dt_text = dl.css("dt::text").get("").strip().upper()
        if dt_text == label.upper():
            dd_text = dl.css("dd::text").get("").strip()
            return parse_price(dd_text)
    return None


# ---------------------------------------------------------------------------
# Helpers — status
# ---------------------------------------------------------------------------

def _extract_status(response: HtmlResponse, json_ld: dict) -> str | None:
    """Determine vehicle availability status.

    Checks for the ``.intransit`` CSS class first (used by DEP for
    "in transit" / "sale pending" badges), then falls back to JSON-LD
    ``offers.availability``.
    """
    intransit_el = response.css(".intransit")
    if intransit_el:
        # The status text may live in a child <span> rather than as a
        # direct text node, so collect *all* descendant text.
        text = " ".join(
            t.strip() for t in intransit_el.css("*::text").getall() if t.strip()
        ).lower()
        if "sale pending" in text:
            return "Sale Pending"
        return "In Transit"

    # Check JSON-LD availability
    offers = json_ld.get("offers")
    offer = offers[0] if isinstance(offers, list) and offers else offers
    if isinstance(offer, dict):
        avail = offer.get("availability", "")
        if "InStock" in avail:
            return "In Stock"
        if "PreOrder" in avail or "PreSale" in avail:
            return "In Transit"

    return None


# ---------------------------------------------------------------------------
# Helpers — availability date
# ---------------------------------------------------------------------------

_AVAIL_DATE_RE = re.compile(
    r"(?:estimated\s+)?(?:arrival|availability)\s+(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)


def _extract_availability_date(response: HtmlResponse) -> str | None:
    """Extract an estimated arrival / availability date from the page text."""
    # Collect all descendant text — the date may be inside a child <span>.
    text = " ".join(
        t.strip()
        for t in response.css(".intransit *::text").getall()
        if t.strip()
    )
    match = _AVAIL_DATE_RE.search(text)
    if match:
        return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Helpers — pagination
# ---------------------------------------------------------------------------

def _build_next_page_url(response: HtmlResponse) -> str | None:
    """Build the URL for the next SRP page, or return *None* on the last page.

    DEP uses a JavaScript-driven pagination widget with an ``<input>``
    field and left/right arrow buttons.  The page number is passed as
    the ``p`` query-string parameter.  This helper reads the current
    page and total pages from the embedded script to determine whether
    a next page exists.
    """
    # Extract current page and total pages from the inline pagination script.
    current_page = 1
    page_count = 1

    for script in response.css(".srp_pagination_links_container script::text").getall():
        m_current = re.search(r"var\s+current_page\s*=\s*(\d+)", script)
        m_count = re.search(r"var\s+page_count\s*=\s*(\d+)", script)
        if m_current:
            current_page = int(m_current.group(1))
        if m_count:
            page_count = int(m_count.group(1))

    if current_page >= page_count:
        return None

    # Build next page URL by setting p=<next>
    parsed = urlparse(response.url)
    qs = parse_qs(parsed.query)
    qs["p"] = [str(current_page + 1)]
    # Use quote_via to preserve special chars in param names (e.g. "s:pr")
    # that the DEP site expects unencoded.
    new_query = urlencode({k: v[0] for k, v in qs.items()}, quote_via=lambda s, *_a, **_kw: s)
    return urlunparse(parsed._replace(query=new_query))
