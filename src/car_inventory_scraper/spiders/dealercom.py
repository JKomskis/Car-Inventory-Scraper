"""Spider for Dealer.com (DDC / Cox Automotive) dealership websites.

Dealer.com is one of the most widely used dealership website platforms,
powering thousands of franchise dealerships across the US and Canada.
Inventory search pages live at paths like ``/new-inventory/index.htm``.

The search page renders ``.vehicle-card`` elements containing links to
vehicle detail pages.  This spider collects those links from each search
results page, follows pagination via the ``.pagination-next`` link, and
then scrapes full pricing, packages, and status information from each
vehicle detail page.

Example usage::

    car-inventory-scraper crawl dealercom \\
        --url "https://www.toyotaofkirkland.com/new-inventory/index.htm?year=2026&model=RAV4"
"""

import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import scrapy

from car_inventory_scraper.spiders import log_request_failure
from scrapy.http import HtmlResponse

from car_inventory_scraper.parsing_helpers import (
    normalize_drivetrain,
    normalize_pkg_name,
    parse_price,
)
from car_inventory_scraper.items import CarItem


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
        self._domain = urlparse(url).netloc

    # ------------------------------------------------------------------
    # Search results page — collect detail links
    # ------------------------------------------------------------------

    async def start(self):
        yield scrapy.Request(
            self.start_url,
            meta={
                "nodriver": True,
                "nodriver_wait_js": "document.querySelector('.vehicle-card-detailed')",
            },
            callback=self.parse_search,
            errback=self.errback,
        )

    async def parse_search(self, response: HtmlResponse):
        """Extract vehicle detail links from the search results page."""
        base_url = response.url
        dealer_name = (
            self._dealer_name_override
            or response.css("title::text").get("").split("|")[-1].strip()
        )

        vehicle_cards = response.css(".vehicle-card-detailed")
        self.logger.info("[%s] Found %d vehicles on %s", self._domain, len(vehicle_cards), response.url)

        seen = set()
        for card in vehicle_cards:
            # The first <a> with an href containing /new/ or /used/ is the detail link.
            href = card.css("a[href*='/new/']::attr(href), a[href*='/used/']::attr(href)").get("")
            if not href or href in seen:
                continue
            seen.add(href)
            detail_url = urljoin(base_url, href)

            yield scrapy.Request(
                detail_url,
                callback=self.parse_detail,
                errback=self.errback,
                meta={
                    "nodriver": True,
                    "nodriver_wait_js": "document.body && document.body.innerHTML.includes('DDC.WS.state')",
                    "dealer_name": dealer_name,
                    "dealer_url": base_url,
                },
            )

        # --- Pagination ---
        next_href = response.css(".pagination-next a::attr(href)").get()
        if next_href:
            # The next-page link only contains ?start=N — merge it into
            # the current page URL so filters (year, model, etc.) are kept.
            current_parsed = urlparse(response.url)
            current_qs = parse_qs(current_parsed.query)
            next_qs = parse_qs(urlparse(next_href).query)
            current_qs.update(next_qs)
            next_url = urlunparse(current_parsed._replace(
                query=urlencode(current_qs, doseq=True),
            ))
            yield scrapy.Request(
                next_url,
                meta={
                    "nodriver": True,
                    "nodriver_wait_js": "document.querySelector('.vehicle-card-detailed')",
                },
                callback=self.parse_search,
                errback=self.errback,
            )

    # ------------------------------------------------------------------
    # Vehicle detail page — extract all information
    # ------------------------------------------------------------------

    async def parse_detail(self, response: HtmlResponse):
        """Extract full vehicle details from a Dealer.com VDP.

        Dealer.com VDP pages embed structured vehicle data in
        ``DDC.WS.state`` script blocks and ``DDC.dataLayer``:

        - ``ws-quick-specs`` — VIN, year, trim, colors, drivetrain
        - ``ws-detailed-pricing`` — MSRP, accessories, adjustments, final price
        - ``ws-packages-options`` — factory packages and dealer accessories
        - ``ws-vehicle-title`` — vehicle status (in stock / in transit)
        - ``DDC.dataLayer['vehicles']`` — delivery date range

        All sources are server-rendered in the initial HTML.
        """
        item = CarItem()
        item["detail_url"] = response.url
        item["dealer_name"] = response.meta.get("dealer_name", "")
        item["dealer_url"] = response.meta.get("dealer_url", "")

        # --- Structured data sources ---
        ddc_specs = _extract_ddc_state(response, "ws-quick-specs")
        ddc_pricing = _extract_ddc_state(response, "ws-detailed-pricing")
        ddc_packages = _extract_ddc_state(response, "ws-packages-options")
        ddc_title = _extract_ddc_state(response, "ws-vehicle-title")
        ddc_datalayer = _extract_ddc_datalayer_vehicle(response)

        vehicle = ddc_specs.get("vehicle", {}) if ddc_specs else {}

        # --- Core vehicle identifiers ---
        item["vin"] = vehicle.get("vin")
        item["stock_number"] = _get_localized(vehicle, "stockNumber") or None
        item["model_code"] = _get_localized(vehicle, "modelCode")

        # --- Vehicle info ---
        item["year"] = str(vehicle["year"]) if vehicle.get("year") else None
        item["trim"] = _get_localized(vehicle, "trim")

        # --- Colors ---
        item["exterior_color"] = _get_localized(vehicle, "exteriorColor")
        item["interior_color"] = _get_localized(vehicle, "interiorColor")

        # --- Drivetrain ---
        body_style = _get_localized(vehicle, "bodyStyle") or ""
        item["drivetrain"] = normalize_drivetrain(body_style)

        # --- Packages & accessories ---
        all_raw_packages: list[dict[str, str | int]] = []
        if ddc_packages:
            for opt in ddc_packages.get("options", []):
                name = normalize_pkg_name(opt.get("name", ""))
                price = opt.get("price")
                if name:
                    all_raw_packages.append({
                        "name": name,
                        "price": parse_price(price),
                    })
            for pkg in ddc_packages.get("packages", []):
                name = normalize_pkg_name(pkg.get("name", ""))
                price = pkg.get("price")
                if name:
                    all_raw_packages.append({
                        "name": name,
                        "price": parse_price(price),
                    })

        item["packages"] = all_raw_packages or None

        # --- Pricing ---
        dprice_map = _build_dprice_map(ddc_pricing)
        item["msrp"] = dprice_map.get("Total SRP", {}).get("value")

        # Dealer accessories — use dprice breakdown value when available;
        # the CalculatedPricesPipeline will sum from dealer_accessories otherwise.
        dprice_dealer_acc = dprice_map.get("Dealer Accessories", {}).get("value")
        if dprice_dealer_acc:
            item["dealer_accessories_price"] = dprice_dealer_acc

        total_price = dprice_map.get("Advertised Price", {}).get("value")
        item["total_price"] = total_price or item["msrp"]

        # Adjustments — use explicit Dealer Adjustment from dprice if available;
        # the CalculatedPricesPipeline will compute from price difference otherwise.
        discount_entry = dprice_map.get("Dealer Adjustment", {})
        if discount_entry.get("value") and discount_entry.get("isDiscount"):
            item["adjustments"] = -discount_entry["value"]

        # --- Status ---
        raw_status = ""
        if ddc_title:
            raw_status = ddc_title.get("vehicle", {}).get("status", "")
        item["status"] = _normalize_ddc_status(raw_status)

        # --- Availability date ---
        if item["status"] != "In Stock":
            item["availability_date"] = _format_date_range(
                ddc_datalayer.get("deliveryDateRange")
            )

        yield item

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    def errback(self, failure):
        log_request_failure(failure, self._domain, self.logger)




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
        for locale in ("en_US", "en_CA", "en_GB"):
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



# ---------------------------------------------------------------------------
# Helpers — DDC status normalisation
# ---------------------------------------------------------------------------

_STATUS_MAP = {
    "LIVE": "In Stock",
    "IN_TRANSIT": "In Transit",
    "IN_TRANSIT_AT_FACTORY": "In Production",
}


def _normalize_ddc_status(raw: str) -> str | None:
    """Map a DDC status string to a human-readable label."""
    if not raw:
        return None
    return _STATUS_MAP.get(raw.upper().replace(" ", "_"), raw.replace("_", " ").title())
