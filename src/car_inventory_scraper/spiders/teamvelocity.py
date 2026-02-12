"""Spider for Team Velocity (Apollo®) dealership websites.

Team Velocity is a dealership website platform powering franchise dealerships
across the US.  Inventory search pages live at paths like ``/inventory/New/``
and render vehicle cards as Vue.js components (``standard-inventory`` divs).
Vehicle detail pages (VDPs) embed rich structured data as JSON-LD, inline
JavaScript variables, and a ``vehicleBadgesInfo`` JSON block for status/ETA.

Example usage::

    car-inventory-scraper crawl teamvelocity \\
        --url "https://www.toyotaofseattle.com/inventory/New/Toyota/RAV4?paymenttype=cash&years=2026"
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

import scrapy
from scrapy.http import HtmlResponse
from scrapy_playwright.page import PageMethod

from car_inventory_scraper.parsing_helpers import (
    EXCLUDED_PACKAGES,
    extract_json_ld_car,
    json_ld_price,
    normalize_color,
    normalize_drivetrain,
    normalize_pkg_name,
    parse_price,
)
from car_inventory_scraper.items import CarItem
from car_inventory_scraper.stealth import apply_stealth


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

    # ------------------------------------------------------------------
    # Search results page — collect detail links
    # ------------------------------------------------------------------

    _SRP_SELECTOR = ".standard-inventory"

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
        vehicle_cards = response.css(".standard-inventory")
        self.logger.info("Found %d vehicle cards on %s", len(vehicle_cards), response.url)

        seen: set[str] = set()
        for card in vehicle_cards:
            # Each card wraps a single <a> with class ``si-vehicle-box``
            href = card.css(
                "a.si-vehicle-box::attr(href), "
                "a[href*='/viewdetails/']::attr(href)"
            ).get("")
            if not href or href == "#":
                continue
            # Strip fragment (#Transact, etc.) — we only need the base VDP URL
            detail_url = urljoin(base_url, href.split("#")[0])
            if detail_url in seen:
                continue
            seen.add(detail_url)

            yield scrapy.Request(
                detail_url,
                meta={
                    "playwright": True,
                    "playwright_include_page": True,
                    "playwright_page_init_callback": apply_stealth,
                    "dealer_name": dealer_name,
                    "dealer_url": base_url,
                },
                callback=self.parse_detail,
                errback=self.errback_close_page,
            )

        # --- Pagination ---
        next_page = response.css(
            ".inventory-pagination a[rel='next']::attr(href)"
        ).get()
        if next_page:
            yield scrapy.Request(
                urljoin(base_url, next_page),
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
        """Extract full vehicle details from a Team Velocity VDP.

        Team Velocity VDP pages embed structured vehicle data in several places:

        1. **JSON-LD** (``<script type="application/ld+json">``) — contains
           VIN, year, make, model, colors, price, body type, and transmission.
        2. **Inline JavaScript variables** — ``var vin``, ``var trim``,
           ``var driveTrain``, ``var model``, ``var stockNumber``, etc.
        3. **``vehicleBadgesInfo``** — a JSON string variable containing
           InTransit / InStock / InProduction status and ETA dates.
        4. **``.oem-specifications``** — Vue.js-rendered sections including
           "Packages and Options" with package/accessory names (no prices).

        JSON-LD and inline variables are server-rendered, but the OEM
        specifications section requires client-side JS to render.
        """
        page = response.meta.get("playwright_page")
        if page:
            # The packages/options section is Vue.js-rendered and may
            # not be in the initial DOM.  Wait for it on the live page,
            # then rebuild the Scrapy response from the updated HTML.
            # If the element never appears (some dealers omit it) the
            # TimeoutError is caught and we continue with what we have.
            try:
                await page.wait_for_selector(
                    ".oem-specifications", timeout=5_000,
                )
            except Exception:
                pass  # element absent — packages will be empty
            # Re-read the (now Vue-hydrated) page content.
            body = await page.content()
            response = response.replace(body=body.encode("utf-8"))
            await page.close()

        item = CarItem()
        item["detail_url"] = response.url
        item["dealer_name"] = response.meta.get("dealer_name", "")
        item["dealer_url"] = response.meta.get("dealer_url", "")

        # --- Structured data sources ---
        json_ld = extract_json_ld_car(response)
        js_vars = _extract_js_vars(response)
        badges = _extract_badges_info(response)

        # --- Core vehicle identifiers ---
        item["vin"] = (
            js_vars.get("vin", "").upper()
            or json_ld.get("vehicleIdentificationNumber")
        ) or None
        stock = js_vars.get("stockNumber") or json_ld.get("sku") or None
        # Some listings use the VIN (or a prefix of it) as a placeholder
        # stock number — discard those so we only keep real dealer stock #s.
        vin_upper = (item["vin"] or "").upper()
        if stock and vin_upper and vin_upper.startswith(stock.upper()):
            stock = None
        item["stock_number"] = stock
        item["model_code"] = None  # populated from DOM below

        # --- Vehicle info ---
        item["year"] = js_vars.get("year") or json_ld.get("vehicleModelDate")
        item["trim"] = js_vars.get("trim") or None

        # --- Colors ---
        item["exterior_color"] = normalize_color(json_ld.get("color"))
        item["interior_color"] = normalize_color(json_ld.get("vehicleInteriorColor"))

        # --- Drivetrain ---
        drive_train_raw = js_vars.get("driveTrain", "")
        item["drivetrain"] = normalize_drivetrain(drive_train_raw)

        # --- Model code from rendered DOM ---
        model_code_text = response.css("#modelcode span::text").getall()
        for text in model_code_text:
            m = re.match(r"Model Code:\s*(.+)", text.strip())
            if m:
                item["model_code"] = m.group(1).strip()
                break

        # --- MPG from rendered DOM ---
        mpg_text = response.css("#highwaymileage span::text").get("")
        mpg_city, mpg_highway = _parse_mpg(mpg_text)
        # CarItem doesn't have mpg fields currently, but we extract from
        # the DOM for potential future use.  (Not yielded.)

        # --- Pricing ---
        # TSRP / MSRP from the pricing section
        msrp_text = response.css("#msrp-strike-out-text::text").get("")
        msrp = parse_price(msrp_text)
        if not msrp:
            msrp = json_ld_price(json_ld)
        item["msrp"] = msrp

        # --- Packages from the "Packages and Options" OEM section ---
        packages = _extract_packages(response)
        item["packages"] = packages or None
        item["total_packages_price"] = None  # prices not listed on this platform
        item["base_price"] = msrp  # No per-package price breakdown available
        item["dealer_accessories"] = None

        # Total / advertised price — on this platform TSRP is the only
        # price shown; there is no separate "internet price" or
        # "advertised price" line.
        item["total_price"] = msrp
        item["adjustments"] = None

        # --- Status ---
        item["status"] = _extract_status(badges)

        # --- Availability date ---
        item["availability_date"] = _extract_availability_date(badges)

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
# Helpers — inline JavaScript variable extraction
# ---------------------------------------------------------------------------

# Matches ``var <name> = '<value>';`` or ``var <name> = "<value>";``
_JS_VAR_RE = re.compile(
    r"var\s+(\w+)\s*=\s*['\"]([^'\"]*?)['\"]"
)

# The specific variable names we care about on VDP pages
_INTERESTING_VARS = frozenset({
    "vin", "stockNumber", "year", "make", "model", "trim",
    "driveTrain", "bodyType", "colorCode", "inventoryVdpName",
    "vehicleType", "inventoryType", "styleId",
})


def _extract_js_vars(response: HtmlResponse) -> dict[str, str]:
    """Extract inline ``var <name> = '<value>'`` declarations from <script> tags.

    Team Velocity pages define vehicle attributes as top-level JavaScript
    variables inside ``<script>`` blocks.  This helper collects the ones we
    care about into a flat dict.
    """
    result: dict[str, str] = {}
    for script in response.css("script::text").getall():
        for m in _JS_VAR_RE.finditer(script):
            name, value = m.group(1), m.group(2)
            if name in _INTERESTING_VARS and value:
                result[name] = value
    return result


# ---------------------------------------------------------------------------
# Helpers — vehicleBadgesInfo extraction
# ---------------------------------------------------------------------------

_BADGES_RE = re.compile(
    r"var\s+vehicleBadgesInfo\s*=\s*'(.*?)'"
)


def _extract_badges_info(response: HtmlResponse) -> dict:
    """Extract and parse the ``vehicleBadgesInfo`` JSON variable.

    This variable is an HTML-entity-encoded JSON string containing vehicle
    status (In Transit, In Stock, In Production) and estimated arrival dates.
    """
    for script in response.css("script::text").getall():
        m = _BADGES_RE.search(script)
        if m:
            raw = m.group(1)
            # Decode HTML entities used in the inline JSON
            raw = raw.replace("&quot;", '"').replace("&amp;", "&")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                continue
    return {}


def _extract_status(badges: dict) -> str | None:
    """Determine vehicle status from the badges info dict.

    Checks ``IsReserved`` for sale-pending state and combines it with
    the availability status (In Stock / In Transit / In Production) when
    both are present, e.g. ``"Sale Pending - In Transit"``.
    """
    is_reserved = badges.get("IsReserved", {}).get("IsReservedStatus", False)

    availability = None
    if badges.get("InStock", {}).get("InStockStatus"):
        availability = "In Stock"
    elif badges.get("InTransit", {}).get("InTransitStatus"):
        availability = "In Transit"
    elif badges.get("InProduction", {}).get("InProductionStatus"):
        availability = "In Production"

    if is_reserved and availability:
        return f"Sale Pending - {availability}"
    if is_reserved:
        return "Sale Pending"
    return availability


def _extract_availability_date(badges: dict) -> str | None:
    """Extract estimated availability date range from badges info.

    The ``ETA`` field contains a string like ``"02/16/2026 and 02/28/2026"``.
    We normalise it to ``"MM/DD/YY - MM/DD/YY"`` format for consistency with
    other spiders.
    """
    eta = badges.get("InTransit", {}).get("ETA")
    if not eta:
        eta = badges.get("InProduction", {}).get("ETA")
    if not eta:
        return None

    # Parse "MM/DD/YYYY and MM/DD/YYYY" → "MM/DD/YY - MM/DD/YY"
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


# ---------------------------------------------------------------------------
# Helpers — packages & options extraction
# ---------------------------------------------------------------------------


def _extract_packages(response: HtmlResponse) -> list[dict[str, str | None]]:
    """Extract package/option names from the OEM specifications section.

    Team Velocity VDPs list packages under an ``.oem-specifications`` block
    titled "Packages and Options".  Each package is an ``<li>`` whose inner
    ``<span>`` elements contain the name.  Prices are **not** shown on this
    platform, so each entry has ``price: None``.

    Items whose normalised name appears in :data:`EXCLUDED_PACKAGES` (e.g.
    "50 State Emissions") are filtered out.
    """
    packages: list[dict[str, str | None]] = []

    for section in response.css(".oem-specifications"):
        title = section.css(".oem-specifications-title h3::text").get("")
        if "package" not in title.lower() and "option" not in title.lower():
            continue

        for li in section.css("ul li"):
            # The package name lives in nested <span> elements; grab all
            # text, strip whitespace, and ignore "Details" link text.
            texts = li.css("span::text").getall()
            name = ""
            for t in texts:
                t = t.strip()
                if t and t.lower() != "details":
                    name = t
                    break
            if not name:
                continue
            name = normalize_pkg_name(name)
            if name.upper() in EXCLUDED_PACKAGES:
                continue
            packages.append({"name": name, "price": None})

    return packages


# ---------------------------------------------------------------------------
# Helpers — pricing & parsing
# ---------------------------------------------------------------------------



def _parse_mpg(text: str) -> tuple[int | None, int | None]:
    """Parse MPG from a Team Velocity string like ``MPG/MPGe : 46/39/0``.

    The format is ``highway/city/electric`` (or similar).  Returns
    ``(city, highway)`` as integers, or ``(None, None)`` if unparseable.
    """
    m = re.search(r"(\d+)/(\d+)", text)
    if m:
        # The format appears to be highway/city based on observed data
        return int(m.group(2)), int(m.group(1))
    return None, None

