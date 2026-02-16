"""Spider for Dealer eProcess (DEP) / DealerInspire dealership websites.

Dealer eProcess is a dealership website platform that serves many Toyota
(and other brand) dealerships.  Inventory search pages live at paths like
``/search/new-…`` and server-side render vehicle cards.  Vehicle detail
pages embed JSON-LD ``Vehicle`` schema data as well as a ``data-vehicle``
attribute with URL-encoded JSON metadata.

Cloudflare protection is bypassed via the ``use_cloudscraper`` request
meta key (using the ``cloudscraper`` library) — no browser automation required.

Example usage::

    car-inventory-scraper crawl dealereprocess \\
        --url "https://www.doxontoyota.com/search/new-2026-toyota-rav4-auburn-wa/"
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import scrapy

from car_inventory_scraper.spiders import log_request_failure
from scrapy.http import HtmlResponse

from car_inventory_scraper.items import CarItem
from car_inventory_scraper.parsing_helpers import (
    EXCLUDED_PACKAGES,
    normalize_color,
    normalize_drivetrain,
    normalize_pkg_name,
    parse_price,
)


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
        self._domain = urlparse(url).netloc

    # ------------------------------------------------------------------
    # Search results page — collect detail links
    # ------------------------------------------------------------------

    async def start(self):
        yield scrapy.Request(
            self.start_url,
            meta={"use_cloudscraper": True},
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
        self.logger.info("[%s] Found %d vehicles on %s", self._domain, len(vehicle_cards), response.url)

        for card in vehicle_cards:
            # Primary: dedicated vehicle link element
            href = card.css(".vehicle_item__vehicle_link::attr(href)").get("")
            if not href:
                self.logger.warning("[%s] No vehicle link found in card: %s", self._domain, card.css(".vehicle_item__title::text").get(""))
                continue

            detail_url = urljoin(base_url, href)

            yield scrapy.Request(
                detail_url,
                meta={
                    "use_cloudscraper": True,
                    "dealer_name": dealer_name,
                    "dealer_url": base_url,
                },
                callback=self.parse_detail,
                errback=self.errback,
            )

        # --- Pagination ---
        next_url = _build_next_page_url(response)
        if next_url:
            yield scrapy.Request(
                next_url,
                meta={"use_cloudscraper": True},
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
        item["vin"] = json_ld.get("vehicleIdentificationNumber")
        item["stock_number"] = json_ld.get("sku") or None
        item["model_code"] = data_vehicle.get("modelCd") or None
        item["year"] = json_ld.get("vehicleModelDate") or None
        item["trim"] = json_ld.get("vehicleConfiguration") or None

        # --- Colors ---
        item["exterior_color"] = normalize_color(json_ld.get("color"))
        item["interior_color"] = normalize_color(json_ld.get("vehicleInteriorColor"))

        # --- Drivetrain ---
        # DEP stores drivetrain in the name field rather than
        # a dedicated JSON-LD property.
        name = json_ld.get("name", "")
        item["drivetrain"] = normalize_drivetrain(name)

        # --- Packages / installed options ---
        packages = []
        dealer_acc_packages: list[dict[str, str | int]] = []
        for opt in response.css(".installed_options__item"):
            opt_name = opt.css(".installed_options__title::text").get("").strip()
            opt_price_raw = opt.css(".installed_options__cost::text").get("").strip()
            if not opt_name:
                continue
            if opt_name.lower() in EXCLUDED_PACKAGES:
                continue
            pkg = {"name": normalize_pkg_name(opt_name), "price": parse_price(opt_price_raw)}
            if _is_dealer_accessory(opt_name):
                dealer_acc_packages.append(pkg)
            else:
                packages.append(pkg)
        item["packages"] = packages or None
        item["dealer_accessories"] = dealer_acc_packages or None

        # --- Pricing ---
        # TSRP / MSRP from the pricing container
        msrp = _extract_pricing_label(response, "TSRP") or None
        item["msrp"] = msrp

        # Total / advertised price — look for an advertised price label first, then fall back to MSRP.
        total_price = _extract_pricing_label(response, "ADVERTISED PRICE")
        item["total_price"] = total_price if total_price else msrp

        # --- Status ---
        item["status"] = _extract_status(response, json_ld)

        # --- Availability date ---
        item["availability_date"] = _extract_availability_date(response)

        yield item

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def errback(self, failure):
        log_request_failure(failure, self._domain, self.logger)


# ---------------------------------------------------------------------------
# Helpers — dealer-installed accessories detection
# ---------------------------------------------------------------------------

# Package names (normalised to lowercase) that are dealer-installed
# accessories rather than factory packages.  These are excluded from the
# packages list / total and counted under dealer_accessories_price & adjustments.
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


# ---------------------------------------------------------------------------
# Helpers — pricing
# ---------------------------------------------------------------------------

def _extract_pricing_label(response: HtmlResponse, label: str) -> int | None:
    """Extract a price value from the pricing container by its ``<dt>`` label.

    DEP VDPs display pricing as a single ``<dl>`` with paired ``<dt>``/``<dd>``
    elements inside ``.veh_pricing_container``::

        <dl>
          <dt>TSRP</dt>
          <dd>$45,703</dd>
          <dt>ADVERTISED PRICE</dt>
          <dd>$44,200</dd>
        </dl>
    """
    for dt in response.css(".veh_pricing_container dl dt"):
        dt_text = dt.css("::text").get("").strip().upper()
        if dt_text.startswith(label.upper()):
            dd = dt.xpath("following-sibling::dd[1]")
            dd_text = dd.css("::text").get("").strip()
            return parse_price(dd_text)
    return None


# ---------------------------------------------------------------------------
# Helpers — status
# ---------------------------------------------------------------------------

def _extract_status(response: HtmlResponse, json_ld: dict) -> str:
    """Determine vehicle availability status.

    Checks for the ``.intransit`` CSS class (used by DEP for status badges).
    Returns a status string like ``"In Stock"``, ``"In Transit"``,
    ``"In Production"``, optionally prefixed with ``"Sale Pending - "``.
    """
    intransit_el = response.css(".intransit")
    if intransit_el:
        text = " ".join(
            t.strip() for t in intransit_el.css("*::text").getall() if t.strip()
        ).lower()

        sale_pending = "sale pending" in text

        if "build phase" in text or "in production" in text:
            status = "In Production"
        elif "in transit" in text:
            status = "In Transit"
        else:
            status = "In Stock"

        if sale_pending:
            return f"Sale Pending - {status}"
        return status

    return "In Stock"


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
