"""Spider for Team Velocity (Apollo®) dealership websites.

Team Velocity is a dealership website platform powering franchise dealerships
across the US.  Inventory search pages live at paths like ``/inventory/New/``
and render vehicle cards as server-rendered HTML with CSS class
``.standard-inventory``.

This spider:

1. Fetches the search-results page and extracts ``accountId`` and per-vehicle
   VINs from the ``.standard-inventory`` card ``data-itemid`` attributes.
2. Calls the JSON API ``/api/Inventory/vehicle?vin=…&accountid=…`` for each
   vehicle to obtain full details, including package names with prices.
3. Follows pagination links (``.inventory-pagination a[rel='next']``).

Example usage::

    car-inventory-scraper crawl teamvelocity \\
        --url "https://www.toyotaofseattle.com/inventory/New/Toyota/RAV4?paymenttype=cash&years=2026"
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

import scrapy

from car_inventory_scraper.spiders import log_request_failure
from scrapy.http import HtmlResponse, JsonResponse

from car_inventory_scraper.parsing_helpers import (
    EXCLUDED_PACKAGES,
    normalize_color,
    normalize_drivetrain,
    normalize_pkg_name,
    parse_price,
)
from car_inventory_scraper.items import CarItem


# ---------------------------------------------------------------------------
# Regex for extracting ``var accountId = '<digits>'`` from inline <script>s.
# ---------------------------------------------------------------------------
_ACCOUNT_ID_RE = re.compile(r"var\s+accountId\s*=\s*'(\d+)'")


class TeamVelocitySpider(scrapy.Spider):
    """Scrape vehicle inventory from a Team Velocity-powered dealership site."""

    name = "teamvelocity"

    # Passed via ``-a url=…`` or the CLI wrapper.
    def __init__(self, url: str | None = None, dealer_name: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if url is None:
            raise ValueError(
                "A starting URL is required. "
                "Pass it with: -a url=https://example.com/inventory/New/"
            )
        self.start_url = url
        self._dealer_name_override = dealer_name
        self._domain = urlparse(url).netloc

    # ------------------------------------------------------------------
    # Search results page — collect VINs, then use the Vehicle API
    # ------------------------------------------------------------------

    async def start(self):
        yield scrapy.Request(
            self.start_url,
            callback=self.parse_search,
            errback=self.errback,
        )

    async def parse_search(self, response: HtmlResponse):
        """Extract VINs and accountId from the search page, then request the API."""
        base_url = response.url
        dealer_name = (
            self._dealer_name_override
            or response.css("title::text").get("").split("|")[-1].strip()
        )

        # --- Extract accountId from inline JS ---
        account_id = None
        for script in response.css("script::text").getall():
            m = _ACCOUNT_ID_RE.search(script)
            if m:
                account_id = m.group(1)
                break
        if not account_id:
            self.logger.error("[%s] Could not find accountId on %s", self._domain, response.url)
            return

        # --- Collect VINs from vehicle cards ---
        vehicle_cards = response.css(".standard-inventory")
        self.logger.info("[%s] Found %d vehicles on %s", self._domain, len(vehicle_cards), response.url)

        for card in vehicle_cards:
            # data-itemid looks like "Toyota-RAV4-SE-JTM6CRAV5TD304101"
            # — the VIN is the last segment.
            data_item = card.attrib.get("data-itemid", "")
            vin = data_item.rsplit("-", 1)[-1] if data_item else ""
            if not vin:
                self.logger.warning("[%s] Could not extract VIN from card with data-itemid=%r", self._domain, data_item)
                continue

            # Build the detail-page URL for the ``detail_url`` field.
            href = card.css(
                "a.si-vehicle-box::attr(href), "
                "a[href*='/viewdetails/']::attr(href)"
            ).get("")
            detail_url = urljoin(base_url, href.split("#")[0]) if href else ""

            api_url = urljoin(
                base_url,
                f"/api/Inventory/vehicle?vin={vin}&accountid={account_id}",
            )
            yield scrapy.Request(
                api_url,
                callback=self.parse_api,
                errback=self.errback,
                meta={
                    "dealer_name": dealer_name,
                    "dealer_url": base_url,
                    "detail_url": detail_url,
                },
            )

        # --- Pagination ---
        next_page = response.css(
            ".inventory-pagination a[rel='next']::attr(href)"
        ).get()
        if next_page:
            yield scrapy.Request(
                urljoin(base_url, next_page),
                callback=self.parse_search,
                errback=self.errback,
            )

    # ------------------------------------------------------------------
    # Vehicle API response — extract all information
    # ------------------------------------------------------------------

    async def parse_api(self, response: JsonResponse):
        """Build a CarItem from the ``/api/Inventory/vehicle`` JSON response."""
        data = json.loads(response.text)

        item = CarItem()
        item["detail_url"] = response.meta.get("detail_url", "")
        item["dealer_name"] = (
            response.meta.get("dealer_name")
            or data.get("clientName", "")
        )
        item["dealer_url"] = response.meta.get("dealer_url", "")

        # --- Core vehicle identifiers ---
        item["vin"] = (data.get("vin") or "").upper() or None
        stock = data.get("stock")
        vin_upper = (item["vin"] or "")
        if stock and vin_upper and vin_upper.startswith(stock.upper()):
            stock = None
        item["stock_number"] = stock or None
        item["model_code"] = data.get("modelNumber") or None

        # --- Vehicle info ---
        item["year"] = data.get("year")
        item["trim"] = data.get("trim") or None

        # --- Colors ---
        item["exterior_color"] = normalize_color(data.get("exteriorColor"))
        item["interior_color"] = normalize_color(data.get("interiorColor"))

        # --- Drivetrain ---
        item["drivetrain"] = normalize_drivetrain(data.get("drivetrain", ""))

        # --- Pricing ---
        msrp = parse_price(data.get("msrp"))
        item["msrp"] = msrp

        selling = parse_price(data.get("sellingPrice"))
        buy_fors = data.get("buyFors") or []
        buy_for_price = parse_price(buy_fors[0].get("buyForPrice")) if buy_fors else None
        item["total_price"] = buy_for_price or selling or msrp

        # --- Packages from oeM_InstalledPackages ---
        item["packages"] = _parse_oem_packages(data.get("oeM_InstalledPackages", "")) or None

        # --- Status ---
        item["status"] = _status_from_api(data)

        # --- Availability date ---
        item["availability_date"] = _format_eta(data.get("eta"))

        yield item

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def errback(self, failure):
        log_request_failure(failure, self._domain, self.logger)


# ---------------------------------------------------------------------------
# Helpers — OEM installed packages parsing
# ---------------------------------------------------------------------------

def _parse_oem_packages(raw: str) -> list[dict[str, str | int | None]]:
    """Parse the pipe-delimited ``oeM_InstalledPackages`` string from the API.

    Format::

        marketingName:Weather Package~@marketingLongName:…~@msrp:375~@ImageUrl:|
        marketingName:Door Sill Protectors~@marketingLongName:…~@msrp:199~@ImageUrl:|…

    Each package is separated by ``|``.  Within a package the fields are
    separated by ``~@``.  Returns a list of ``{"name": …, "price": …}`` dicts.
    """
    if not raw:
        return []

    packages: list[dict[str, str | int | None]] = []
    for entry in raw.split("|"):
        entry = entry.strip()
        if not entry:
            continue
        fields: dict[str, str] = {}
        for part in entry.split("~@"):
            if ":" in part:
                key, _, value = part.partition(":")
                fields[key.strip()] = value.strip()

        name = fields.get("marketingName", "").strip()
        if not name:
            continue
        name = normalize_pkg_name(name)
        if name.lower() in EXCLUDED_PACKAGES:
            continue

        price = parse_price(fields.get("msrp"))
        packages.append({"name": name, "price": price})

    return packages


# ---------------------------------------------------------------------------
# Helpers — status from API booleans
# ---------------------------------------------------------------------------

def _status_from_api(data: dict) -> str | None:
    """Determine vehicle status from API boolean fields.

    Uses ``reserved``, ``inTransit``, ``inProduction``, and the implicit
    "in stock" state (none of the transit/production flags set).
    """
    is_reserved = data.get("reserved", False)

    availability = None
    if data.get("inTransit"):
        availability = "In Transit"
    elif data.get("inProduction"):
        availability = "In Production"
    elif not data.get("inTransit") and not data.get("inProduction"):
        # If neither in-transit nor in-production, it's in stock
        # (provided dateInStock is set or we simply infer it).
        availability = "In Stock"

    if is_reserved and availability:
        return f"Sale Pending - {availability}"
    if is_reserved:
        return "Sale Pending"
    return availability


# ---------------------------------------------------------------------------
# Helpers — ETA formatting
# ---------------------------------------------------------------------------

def _format_eta(eta: str | None) -> str | None:
    """Format the API ``eta`` field to ``"MM/DD/YY - MM/DD/YY"``.

    The raw value looks like ``"02/16/2026 and 02/28/2026"``.
    """
    if not eta:
        return None

    parts = [p.strip() for p in eta.split(" and ")]
    formatted: list[str] = []
    for part in parts:
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", part)
        if m:
            month, day, year = m.group(1), m.group(2), m.group(3)
            formatted.append(f"{month}/{day}/{year[2:]}")
        else:
            formatted.append(part)
    return " - ".join(formatted) if formatted else None

