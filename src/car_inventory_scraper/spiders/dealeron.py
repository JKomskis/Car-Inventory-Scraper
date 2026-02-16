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

import json
import math
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import scrapy
from scrapy.http import HtmlResponse

from car_inventory_scraper.spiders import log_request_failure
from car_inventory_scraper.parsing_helpers import (
    EXCLUDED_PACKAGES,
    normalize_pkg_name,
    parse_price,
)
from car_inventory_scraper.items import CarItem

# Number of vehicles DealerOn displays per search-results page.
_VEHICLES_PER_PAGE = 12


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
        self._domain = urlparse(url).netloc

    # ------------------------------------------------------------------
    # Search results page — collect detail links
    # ------------------------------------------------------------------

    async def start(self):
        yield scrapy.Request(
            self.start_url,
            callback=self.parse_search,
            errback=self.errback,
        )

    async def parse_search(self, response: HtmlResponse):
        """Parse the search results page using embedded JSON script tags.

        DealerOn search pages embed two useful ``<script>`` blocks that
        are present in the static HTML (no JavaScript rendering needed):

        * ``#dealeron_tagging_data`` — contains ``itemCount`` (total
          number of vehicles matching the search) used for pagination.
        * ``#dlron-srp-model`` — contains ``ItemListJson``, a
          schema.org ``ItemList`` with the URL, name, and VIN of every
          vehicle on the current page.
        """
        base_url = response.url
        dealer_name = (
            self._dealer_name_override
            or response.css("title::text").get("").split("|")[-1].strip()
        )

        # --- Extract vehicle list from dlron-srp-model ---
        detail_urls = _extract_vehicle_urls(response, base_url)
        self.logger.info(
            "[%s] Found %d vehicles on %s",
            self._domain, len(detail_urls), response.url,
        )

        for detail_url in detail_urls:
            yield scrapy.Request(
                detail_url,
                meta={
                    "dealer_name": dealer_name,
                    "dealer_url": base_url,
                },
                callback=self.parse_detail,
                errback=self.errback,
            )

        # --- Pagination ---
        # Only the first page triggers pagination requests.  We read
        # ``itemCount`` from the tagging-data script and calculate how
        # many pages exist (12 vehicles per page).  Pages 2+ are
        # fetched by appending ``&pt=<page>`` to the start URL.
        current_page = _current_page_number(response.url)
        if current_page == 1:
            item_count = _extract_item_count(response)
            if item_count is not None:
                total_pages = math.ceil(item_count / _VEHICLES_PER_PAGE)
                for page in range(2, total_pages + 1):
                    next_url = _build_page_url(self.start_url, page)
                    yield scrapy.Request(
                        next_url,
                        callback=self.parse_search,
                        errback=self.errback,
                    )

    # ------------------------------------------------------------------
    # Vehicle detail page — extract all information
    # ------------------------------------------------------------------

    def parse_detail(self, response: HtmlResponse):
        """Extract full vehicle details from a VDP (Vehicle Detail Page).

        DealerOn VDP pages store all structured vehicle data as ``data-*``
        attributes on the main ``.vdp[data-vehicle-information]`` container,
        which is far more reliable than scraping rendered text.
        """
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
                    drivetrain = token
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
        packages: list[dict[str, str | int]] = []
        dealer_acc_packages: list[dict[str, str | int]] = []
        for pkg in response.css(".package-info"):
            pkg_name = pkg.css(".package-info__name::text").get("").strip()
            price_str = pkg.css(".package-info__price::text").get("").strip()
            if not pkg_name or pkg_name.lower() in EXCLUDED_PACKAGES:
                continue
            entry = {"name": pkg_name, "price": parse_price(price_str)}
            if _is_dealer_accessory(pkg_name):
                dealer_acc_packages.append(entry)
            else:
                packages.append(entry)
        item["packages"] = packages or None
        item["dealer_accessories"] = dealer_acc_packages or None

        # --- Pricing ---
        item["msrp"] = parse_price(vdp.attrib.get("data-msrp"))

        total_price = parse_price(vdp.attrib.get("data-price"))
        item["total_price"] = total_price if total_price else item["msrp"]

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

    def errback(self, failure):
        log_request_failure(failure, self._domain, self.logger)


# ---------------------------------------------------------------------------
# Helpers — dealer-installed accessories detection
# ---------------------------------------------------------------------------

# Package names (normalised to lowercase) that are dealer-installed
# accessories rather than factory packages.  These are excluded from the
# packages list / total and counted under dealer_accessories_price & adjustments.
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
_TRADEMARK_SUFFIX_RE = re.compile(r"\s*[®]", re.IGNORECASE)


def _strip_html(value: str) -> str | None:
    """Remove HTML tags and metadata annotations from a data-attribute value.

    DealerOn ``data-intcolor`` / ``data-extcolor`` attributes may contain:

    * Disclaimer anchor elements (``<a …><sup>60</sup></a>``)
    * Bracket-enclosed metadata like ``[Extra_Cost_Color]``
    * Copyright suffixes like ``©`` or ``© Mixed Media``

    This helper strips all of these, returning only the clean color name.
    """
    if not value:
        return None
    cleaned = _HTML_TAG_RE.sub("", value)
    cleaned = _BRACKET_TAG_RE.sub("", cleaned)
    cleaned = _TRADEMARK_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned or None


# ---------------------------------------------------------------------------
# Helpers — search-page JSON extraction
# ---------------------------------------------------------------------------

def _extract_vehicle_urls(response: HtmlResponse, base_url: str) -> list[str]:
    """Extract vehicle detail URLs from the ``#dlron-srp-model`` script tag.

    The tag contains a JSON object whose ``ItemListJson`` value is itself
    a JSON-encoded schema.org ``ItemList``.  Each ``ListItem`` has a
    ``url`` field pointing to the vehicle detail page.
    """
    raw = response.css("script#dlron-srp-model::text").get()
    if not raw:
        return []
    try:
        model = json.loads(raw)
        item_list = json.loads(model["ItemListJson"])
        return [
            item["url"]
            for item in item_list.get("itemListElement", [])
            if item.get("url")
        ]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        import logging
        logging.getLogger(__name__).warning(
            "[%s] Failed to parse dlron-srp-model: %s", urlparse(base_url).netloc, exc,
        )
        return []


def _extract_item_count(response: HtmlResponse) -> int | None:
    """Extract total vehicle count from ``#dealeron_tagging_data``.

    Returns ``None`` if the script tag is missing or unparseable.
    """
    raw = response.css("script#dealeron_tagging_data::text").get()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return int(data["itemCount"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _current_page_number(url: str) -> int:
    """Return the current page number from the ``pt`` query parameter.

    Defaults to 1 when ``pt`` is absent.
    """
    qs = parse_qs(urlparse(url).query)
    try:
        return int(qs["pt"][0])
    except (KeyError, IndexError, ValueError):
        return 1


def _build_page_url(base_url: str, page: int) -> str:
    """Return *base_url* with the ``pt`` query parameter set to *page*."""
    parsed = urlparse(base_url)
    qs = parse_qs(parsed.query)
    qs["pt"] = [str(page)]
    new_query = urlencode({k: v[0] for k, v in qs.items()})
    return urlunparse(parsed._replace(query=new_query))

