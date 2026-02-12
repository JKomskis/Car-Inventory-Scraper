"""Spider for Dealer.com (DDC / Cox Automotive) dealership websites.

Dealer.com is one of the most widely used dealership website platforms,
powering thousands of franchise dealerships across the US and Canada.
Inventory search pages live at paths like ``/new-inventory/index.htm`` and
render vehicle cards via React (``ws-inv-listing`` widget).  Vehicle detail
pages embed rich structured data as JSON-LD and in ``DDC.WS.state`` script
blocks, which this spider extracts.

Example usage::

    car-inventory-scraper crawl dealercom \\
        --url "https://www.toyotaofkirkland.com/new-inventory/index.htm?year=2026&model=RAV4"
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import scrapy
from scrapy.http import HtmlResponse
from scrapy_playwright.page import PageMethod

from car_inventory_scraper.parsing_helpers import (
    extract_json_ld_car,
    format_pkg_price,
    json_ld_price,
    normalize_drivetrain,
    normalize_pkg_name,
    parse_price,
)
from car_inventory_scraper.items import CarItem
from car_inventory_scraper.stealth import apply_stealth


class DealerComSpider(scrapy.Spider):
    """Scrape vehicle inventory from a Dealer.com-powered dealership site."""

    name = "dealercom"

    # Passed via ``-a url=…`` or the CLI wrapper.
    def __init__(self, url: str | None = None, dealer_name: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if url is None:
            raise ValueError(
                "A starting URL is required. "
                "Pass it with: -a url=https://example.com/new-inventory/index.htm"
            )
        self.start_url = url
        self._dealer_name_override = dealer_name

    # ------------------------------------------------------------------
    # Search results page — collect detail links
    # ------------------------------------------------------------------

    _SRP_SELECTOR = ".vehicle-card-detailed"

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
            ".vehicle-card.vehicle-card-detailed, "
            "li[data-uuid].vehicle-card"
        )
        self.logger.info("Found %d vehicle cards on %s", len(vehicle_cards), response.url)

        seen: set[str] = set()
        for card in vehicle_cards:
            title_link = card.css(
                ".vehicle-card-title a, "
                "h2.vehicle-card-title a, "
                "a[href$='.htm']"
            )
            href = title_link.attrib.get("href", "")
            if not href or href == "#":
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
                    # DDC VDP pages load slowly to networkidle; the data we
                    # need lives in server-rendered <script> tags, so
                    # domcontentloaded is sufficient.
                    "playwright_page_goto_kwargs": {
                        "wait_until": "domcontentloaded",
                    },
                    "dealer_name": dealer_name,
                    "dealer_url": base_url,
                },
                callback=self.parse_detail,
                errback=self.errback_close_page,
            )

        # --- Pagination ---
        next_page = response.css(
            ".pagination-next a::attr(href), "
            "a[aria-label='Go to next page']::attr(href)"
        ).get()
        if next_page:
            next_url = _merge_query_params(base_url, urljoin(base_url, next_page))
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
        """Extract full vehicle details from a Dealer.com VDP.

        Dealer.com VDP pages embed structured vehicle data in two places:

        1. **JSON-LD** (``<script type="application/ld+json">``) — contains
           VIN, year, make, model, trim, colors, price, and more.
        2. **``DDC.WS.state``** script blocks — contain detailed specs
           (``ws-quick-specs``), pricing breakdown (``ws-detailed-pricing``),
           packages/options (``ws-packages-options``), and vehicle status
           (``ws-vehicle-title``).

        Both sources are server-rendered and available without waiting for
        client-side JavaScript to execute.
        """
        page = response.meta.get("playwright_page")
        if page:
            await page.close()

        item = CarItem()
        item["detail_url"] = response.url
        item["dealer_name"] = response.meta.get("dealer_name", "")
        item["dealer_url"] = response.meta.get("dealer_url", "")

        # --- Structured data sources ---
        json_ld = extract_json_ld_car(response)
        ddc_specs = _extract_ddc_state(response, "ws-quick-specs")
        ddc_pricing = _extract_ddc_state(response, "ws-detailed-pricing")
        ddc_packages = _extract_ddc_state(response, "ws-packages-options")
        ddc_title = _extract_ddc_state(response, "ws-vehicle-title")
        ddc_datalayer = _extract_ddc_datalayer_vehicle(response)

        vehicle = ddc_specs.get("vehicle", {}) if ddc_specs else {}

        # --- Core vehicle identifiers ---
        item["vin"] = (
            vehicle.get("vin")
            or json_ld.get("vehicleIdentificationNumber")
        )
        item["stock_number"] = _get_localized(vehicle, "stockNumber") or None
        item["model_code"] = _get_localized(vehicle, "modelCode")

        # --- Vehicle info ---
        item["year"] = (
            str(vehicle["year"]) if vehicle.get("year") else json_ld.get("vehicleModelDate")
        )
        item["trim"] = (
            _get_localized(vehicle, "trim")
            or json_ld.get("vehicleConfiguration")
        )

        # --- Colors ---
        item["exterior_color"] = (
            _get_localized(vehicle, "exteriorColor")
            or json_ld.get("color")
        )
        item["interior_color"] = (
            _get_localized(vehicle, "interiorColor")
            or json_ld.get("vehicleInteriorColor")
        )

        # --- Drivetrain ---
        body_style = _get_localized(vehicle, "bodyStyle") or json_ld.get("bodyType", "")
        trim = item.get("trim", "") or ""
        item["drivetrain"] = normalize_drivetrain(body_style, trim)

        # --- Packages & accessories ---
        all_raw_packages: list[dict[str, str | None]] = []
        if ddc_packages:
            for opt in ddc_packages.get("options", []):
                name = normalize_pkg_name(opt.get("name", ""))
                price = opt.get("price")
                if name:
                    all_raw_packages.append({
                        "name": name,
                        "price": format_pkg_price(price),
                    })
            for pkg in ddc_packages.get("packages", []):
                name = normalize_pkg_name(pkg.get("name", ""))
                price = pkg.get("price")
                if name:
                    all_raw_packages.append({
                        "name": name,
                        "price": format_pkg_price(price),
                    })

        # Separate dealer-installed accessories from factory packages
        packages: list[dict[str, str | None]] = []
        dealer_acc_packages: list[dict[str, str | None]] = []
        for pkg in all_raw_packages:
            if _is_dealer_accessory(pkg.get("name", "")):
                dealer_acc_packages.append(pkg)
            else:
                packages.append(pkg)
        item["packages"] = packages or None

        # --- Pricing ---
        dprice_map = _build_dprice_map(ddc_pricing)

        # Fall back to DOM-based pricing if DDC state is empty
        if not dprice_map:
            dprice_map = _extract_dom_pricing(response)

        msrp = (
            dprice_map.get("Total SRP", {}).get("value")
            or json_ld_price(json_ld)
        )
        item["msrp"] = msrp

        total_packages_price = sum(
            parse_price(p.get("price")) or 0 for p in packages
        )
        item["total_packages_price"] = total_packages_price or None

        # Dealer-installed accessories — prefer the dprice breakdown value;
        # fall back to the sum of packages we identified as dealer accessories.
        dealer_acc_from_pkgs = sum(
            parse_price(p.get("price")) or 0 for p in dealer_acc_packages
        )
        dprice_dealer_acc = dprice_map.get("Dealer Accessories", {}).get("value")
        dealer_acc = dprice_dealer_acc or dealer_acc_from_pkgs or None
        item["dealer_accessories"] = dealer_acc

        item["base_price"] = (msrp - total_packages_price) if msrp else None

        total_price = dprice_map.get("Advertised Price", {}).get("value")
        if not total_price:
            total_price = json_ld_price(json_ld)
        item["total_price"] = total_price if total_price else msrp

        # Adjustments — only negative (discount) adjustments are recorded.
        discount_entry = dprice_map.get("Dealer Adjustment", {})
        if discount_entry.get("value") and discount_entry.get("isDiscount"):
            adj = -discount_entry["value"]
        elif msrp and item["total_price"] and item["total_price"] < msrp:
            adj = item["total_price"] - msrp
        else:
            adj = None

        item["adjustments"] = adj

        # --- Status ---
        raw_status = ""
        if ddc_title:
            raw_status = ddc_title.get("vehicle", {}).get("status", "")
        item["status"] = _normalize_ddc_status(raw_status)

        # --- Availability date ---
        item["availability_date"] = _format_date_range(
            ddc_datalayer.get("deliveryDateRange")
        )

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
# Helpers — DDC.WS.state extraction
# ---------------------------------------------------------------------------

_DDC_STATE_RE = re.compile(
    r"DDC\.WS\.state\['([^']+)'\]\['[^']+'\]\s*=\s*"
)


def _extract_ddc_state(response: HtmlResponse, widget_key: str) -> dict | None:
    """Extract a ``DDC.WS.state['<widget_key>']['…']`` JSON object.

    Dealer.com pages store per-widget configuration and data as inline
    JavaScript assignments.  This helper finds the first assignment for
    *widget_key* and parses the JSON value.
    """
    text = response.text
    pattern = rf"DDC\.WS\.state\['{re.escape(widget_key)}'\]\['[^']+'\]\s*=\s*"
    match = re.search(pattern, text)
    if not match:
        return None

    start = match.end()
    # Walk forward counting braces to find the end of the JSON object
    depth = 0
    for i in range(start, min(start + 200_000, len(text))):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ---------------------------------------------------------------------------
# Helpers — DDC.dataLayer extraction
# ---------------------------------------------------------------------------

_DATALAYER_RE = re.compile(
    r"DDC\.dataLayer\['vehicles'\]\s*=\s*\["
)


def _extract_ddc_datalayer_vehicle(response: HtmlResponse) -> dict:
    """Extract the first vehicle object from ``DDC.dataLayer['vehicles']``.

    This analytics data-layer block contains fields not available in the
    ``DDC.WS.state`` widgets, notably ``deliveryDateRange``.
    The source uses escaped hyphens (``\\-``) which must be unescaped
    before JSON parsing.
    """
    text = response.text
    match = _DATALAYER_RE.search(text)
    if not match:
        return {}

    # Find the opening brace of the first object in the array
    start = text.find("{", match.end())
    if start == -1:
        return {}

    depth = 0
    for i in range(start, min(start + 200_000, len(text))):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                raw = text[start : i + 1]
                # DDC escapes hyphens in this block (e.g. 2026\-03\-22)
                raw = raw.replace(r"\-", "-")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {}
    return {}


def _format_date_range(raw: str | None) -> str | None:
    """Format a ``deliveryDateRange`` value like ``2026-03-22 - 2026-04-18``.

    Converts ISO dates to a friendlier ``MM/DD/YY`` format.
    Returns the range string, or ``None`` if not available.
    """
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(" - ")]
    formatted: list[str] = []
    for part in parts:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", part)
        if m:
            year, month, day = m.group(1), m.group(2), m.group(3)
            formatted.append(f"{month}/{day}/{year[2:]}")
        else:
            formatted.append(part)
    return " - ".join(formatted) if formatted else None


# ---------------------------------------------------------------------------
# Helpers — DDC localized values
# ---------------------------------------------------------------------------

def _get_localized(data: dict, key: str) -> str | None:
    """Get a value from a DDC localized field.

    DDC stores some vehicle attributes as either a plain value or a
    localised wrapper like ``{"_type": "s", "en_US": "RAV4"}``.
    This helper transparently unwraps both forms.
    """
    val = data.get(key)
    if val is None:
        return None
    if isinstance(val, dict):
        # Try common locales
        for locale in ("en_US", "en_CA", "fr_CA"):
            if locale in val:
                return str(val[locale])
        # Fall back to any string value that isn't a metadata key
        for k, v in val.items():
            if k.startswith("_"):
                continue
            if isinstance(v, str):
                return v
        return None
    return str(val) if val else None




def _build_dprice_map(ddc_pricing: dict | None) -> dict:
    """Build a ``{label: {value, isDiscount}}`` map from DDC pricing data.

    Keys are the human-readable labels from the dprice entries, e.g.
    ``"Total SRP"``, ``"Dealer Accessories"``, ``"Dealer Adjustment"``,
    ``"Advertised Price"``.
    """
    if not ddc_pricing:
        return {}
    dprice = (
        ddc_pricing
        .get("pricingModuleData", {})
        .get("inventoryData", {})
        .get("dprice", [])
    )
    if not dprice:
        dprice = (
            ddc_pricing
            .get("wisVehicle", {})
            .get("pricing", {})
            .get("dprice", [])
        )
    result: dict = {}
    for entry in dprice:
        label = entry.get("label", "").strip()
        if not label:
            continue
        result[label] = {
            "value": parse_price(entry.get("value")),
            "isDiscount": entry.get("isDiscount", False),
        }
    return result


def _extract_dom_pricing(response: HtmlResponse) -> dict:
    """Fall back to scraping pricing from the rendered DOM.

    Uses the same label-based keys as ``_build_dprice_map`` so callers
    can look up entries identically regardless of the data source.
    """
    result: dict = {}
    for dd in response.css(".pricing-detail dd[data-key]"):
        key = dd.attrib.get("data-key", "").lower()
        val_text = dd.css(".price-value::text").get("")
        val = parse_price(val_text)
        if "msrp" in key:
            result["Total SRP"] = {"value": val, "isDiscount": False}
        elif "internetprice" in key:
            result["Advertised Price"] = {"value": val, "isDiscount": False}
        elif "abcrule" in key:
            is_discount = "text-discount" in (dd.attrib.get("class", ""))
            result["Dealer Adjustment"] = {"value": val, "isDiscount": is_discount}
        elif "wholesaleprice" in key:
            result["Dealer Accessories"] = {"value": val, "isDiscount": False}
    return result

# ---------------------------------------------------------------------------
# Helpers — dealer-installed accessories detection
# ---------------------------------------------------------------------------

# Package names (normalised to lowercase) that are dealer-installed
# accessories rather than factory packages.  These are excluded from the
# packages list / total and counted under dealer_accessories & adjustments.
_DEALER_ACCESSORY_NAMES: set[str] = {
    "pulse",
    "perma plate appearance protection 5yrs coverage",
    "permaplate appearance protection 5yrs coverage",
    "permaplate windshield protection 5yrs coverage",
    "door edge and cup guards",
    "door edge & cup guards",
    "tint",
    "chiprotect 10yrs coverage",
}


def _is_dealer_accessory(name: str) -> bool:
    """Return ``True`` if *name* matches a known dealer-installed accessory."""
    return normalize_pkg_name(name).lower() in _DEALER_ACCESSORY_NAMES

# ---------------------------------------------------------------------------
# Helpers — DDC status normalisation
# ---------------------------------------------------------------------------

_STATUS_MAP = {
    "IN_STOCK": "In Stock",
    "IN_TRANSIT": "In Transit",
    "IN_TRANSIT_AT_PORT": "In Transit",
    "IN_TRANSIT_AT_FACTORY": "In Transit",
    "IN_PRODUCTION": "In Production",
    "ORDERED": "Ordered",
}


def _normalize_ddc_status(raw: str) -> str | None:
    """Map a DDC status string to a human-readable label."""
    if not raw:
        return None
    return _STATUS_MAP.get(raw.upper().replace(" ", "_"), raw.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Helpers — URL manipulation
# ---------------------------------------------------------------------------

def _merge_query_params(original_url: str, new_url: str) -> str:
    """Merge query parameters from *original_url* into *new_url*.

    Dealer.com pagination links only carry the ``start`` offset and drop
    all filter/sort parameters (``year``, ``model``, ``sortBy``, …).  This
    helper copies the original parameters into the pagination URL so the
    search context is preserved.  Parameters already present in *new_url*
    (e.g. ``start``) take precedence over those from *original_url*.
    """
    orig = urlparse(original_url)
    new = urlparse(new_url)

    orig_params = parse_qs(orig.query, keep_blank_values=True)
    new_params = parse_qs(new.query, keep_blank_values=True)

    # Start from original params, then overlay whatever the new URL provides
    merged = {**orig_params, **new_params}

    merged_query = urlencode(merged, doseq=True)
    return urlunparse(new._replace(query=merged_query))
