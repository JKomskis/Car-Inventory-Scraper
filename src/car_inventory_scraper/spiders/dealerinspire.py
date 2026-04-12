"""Spider for DealerInspire-powered dealership websites.

DealerInspire is a dealership website platform (now part of Cars Commerce)
that uses a Search Service API for its inventory search pages.  The API
credentials (base URL, CCID, and API key) are embedded in the page's
inline JavaScript via the ``SEARCH_SERVICE`` variable, along with a
``SEARCH_SERVICE_FIELD_MAP`` that maps legacy Algolia field names to the
new Search Service field names.  Refinements (make, model, year, type)
come from ``inventoryLightningSettings``.

This spider:

1. **Fetches the SRP page** with ``nodriver`` meta
   to bypass Cloudflare bot-detection and extract the Search Service
   config and refinements from embedded ``<script>`` variables.
2. **Queries the Search Service API** directly over HTTPS to retrieve
   the full vehicle inventory in JSON.
3. **Maps each listing** to a :class:`CarItem`.

Unlike other platforms, packages are not available in the API
response (they only appear on the factory window sticker), so the
``packages`` field is always ``None``.

Example usage::

    car-inventory-scraper crawl dealerinspire \\
        --url "https://www.marysvilletoyota.com/new-vehicles/rav4/…"
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import scrapy
from scrapy.http import HtmlResponse, JsonResponse

from car_inventory_scraper.spiders import log_request_failure

from car_inventory_scraper.items import CarItem
from car_inventory_scraper.parsing_helpers import (
    normalize_color,
    normalize_drivetrain,
    parse_price,
)

# Default page size for the Search Service API.
_DEFAULT_PER_PAGE = 20

# Fields we request from the Search Service API.
_REQUESTED_FIELDS = [
    "vin", "stock", "type", "year", "make", "model", "trim",
    "model_number", "date_in_stock", "mileage", "vdp_url", "source_id",
    "styles", "mechanical", "pricing", "dealer", "media",
    "extra_fields", "status", "manufacturer_model_code",
]

# The ``status`` filter ensures we only get published/visible listings.
_STATUS_FILTER = ["publish", "modified", "pend-sale"]

# Default field map from legacy Algolia names -> Search Service indexed names.
_DEFAULT_FIELD_MAP: dict[str, str] = {
    "type": "type_slug",
    "make": "make",
    "model": "model_slug",
    "model_slug": "model_slug",
    "year": "year",
    "trim": "trim_slug",
    "body": "body_type",
    "ext_color_generic": "exterior_color_generic",
    "drivetrain": "drivetrain",
}


class DealerInspireSpider(scrapy.Spider):
    """Scrape vehicle inventory from a DealerInspire-powered dealership site."""

    name = "dealerinspire"

    # Passed via ``-a url=…`` or the CLI wrapper.
    def __init__(
        self,
        url: str | None = None,
        dealer_name: str | None = None,
        search_api_url: str | None = None,
        search_ccid: str | None = None,
        search_api_key: str | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if url is None:
            raise ValueError(
                "A starting URL is required. "
                "Pass it with: -a url=https://example.com/new-vehicles/…"
            )
        self.start_url = url
        self._dealer_name_override = dealer_name
        self._domain = urlparse(url).netloc

        # Optional direct Search Service API credentials (bypass
        # Cloudflare without launching a browser).
        self._search_api_url = search_api_url
        self._search_ccid = search_ccid
        self._search_api_key = search_api_key
        self._direct_api_mode = all(
            [search_api_url, search_ccid, search_api_key]
        )

    # ------------------------------------------------------------------
    # Step 1 — Fetch the SRP page to extract Search Service config
    # ------------------------------------------------------------------

    async def start(self):
        if self._direct_api_mode:
            # Mode B — skip the SRP page; query the Search Service
            # directly with the credentials supplied via dealer config.
            self.logger.info(
                "[%s] Using direct Search Service API mode (skipping SRP page)",
                self._domain,
            )
            search_cfg = {
                "api_url": self._search_api_url,
                "ccid": self._search_ccid,
                "api_key": self._search_api_key,
            }
            # Derive facet filters from _dFR query parameters in the URL.
            url_refinements = _extract_url_refinements(self.start_url)
            facet_filters = _map_refinements(url_refinements, _DEFAULT_FIELD_MAP)

            dealer_name = self._dealer_name_override or self._domain

            yield self._search_request(
                search_cfg, facet_filters, _DEFAULT_PER_PAGE, dealer_name, page=1,
            )
        else:
            # Mode A — fetch the SRP page to extract Search Service config.
            yield scrapy.Request(
                self.start_url,
                meta={"nodriver": True, "nodriver_wait_js": "document.body && document.body.innerHTML.includes('SEARCH_SERVICE')"},
                callback=self.parse_srp_for_search_service,
                errback=self.errback,
            )

    async def parse_srp_for_search_service(self, response: HtmlResponse):
        """Extract Search Service config from embedded JS and begin API queries."""
        search_cfg = _extract_search_service_config(response)
        if not search_cfg:
            self.logger.error(
                "[%s] Could not extract Search Service config on %s",
                self._domain, response.url,
            )
            return

        self.logger.info(
            "[%s] Search Service config: apiUrl=%s, ccid=%s",
            self._domain,
            search_cfg["api_url"],
            search_cfg["ccid"],
        )

        # Extract the indexed field map for mapping refinement names.
        field_map = _extract_field_map(response)

        # Derive dealer name from the page title if not overridden.
        dealer_name = (
            self._dealer_name_override
            or response.css("title::text").get("").split("|")[-1].strip()
        )

        # Build facet filters from inventoryLightningSettings refinements
        # merged with URL _dFR parameters.
        refinements = _extract_refinements(response)
        facet_filters = _map_refinements(refinements, field_map)

        per_page = _extract_per_page(response)

        # Request page 1 from the Search Service (one-indexed).
        yield self._search_request(
            search_cfg, facet_filters, per_page, dealer_name, page=1,
        )

    # ------------------------------------------------------------------
    # Step 2 — Parse Search Service JSON results -> CarItems
    # ------------------------------------------------------------------

    async def parse_search_results(self, response: JsonResponse):
        """Parse a page of Search Service results.

        Yields a :class:`CarItem` for each vehicle listing and follows
        with the next page if more results are available.
        """
        data = json.loads(response.text)
        inner = data.get("data", data)
        listings = inner.get("listings", [])
        total = inner.get("total_vehicle_count", len(listings))
        page = response.meta["page"]
        per_page = response.meta["per_page"]
        dealer_name = response.meta["dealer_name"]
        search_cfg = response.meta["search_cfg"]
        facet_filters = response.meta["facet_filters"]

        nb_pages = (total + per_page - 1) // per_page if per_page else 1

        self.logger.info(
            "[%s] Found %d vehicles (page %d/%d, total: %d)",
            self._domain,
            len(listings),
            page,
            nb_pages,
            total,
        )

        for listing in listings:
            item = _listing_to_item(listing, dealer_name, self.start_url)
            if item is not None:
                yield item

        # --- Pagination ---
        if page < nb_pages:
            yield self._search_request(
                search_cfg, facet_filters, per_page, dealer_name,
                page=page + 1,
            )

    # ------------------------------------------------------------------
    # Search Service request builder
    # ------------------------------------------------------------------

    def _search_request(
        self,
        search_cfg: dict,
        facet_filters: dict[str, list],
        per_page: int,
        dealer_name: str,
        page: int,
    ) -> scrapy.Request:
        """Build a Scrapy request to the Search Service API."""
        url = (
            f"{search_cfg['api_url'].rstrip('/')}"
            f"/api/v1/listings/{search_cfg['ccid']}/search"
        )

        body = json.dumps({
            "page": page,
            "perPage": per_page,
            "filters": {"status": _STATUS_FILTER},
            "facetFilters": facet_filters,
            "requestedFields": _REQUESTED_FIELDS,
        })

        return scrapy.Request(
            url,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": search_cfg["api_key"],
            },
            body=body,
            callback=self.parse_search_results,
            errback=self.errback,
            meta={
                "search_cfg": search_cfg,
                "facet_filters": facet_filters,
                "per_page": per_page,
                "dealer_name": dealer_name,
                "page": page,
            },
        )

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def errback(self, failure):
        log_request_failure(failure, self._domain, self.logger)


# ---------------------------------------------------------------------------
# Helpers — Search Service config extraction
# ---------------------------------------------------------------------------

def _extract_search_service_config(response: HtmlResponse) -> dict | None:
    """Extract Search Service connection details from the SRP page's JavaScript.

    Looks for the ``SEARCH_SERVICE`` JS variable embedded in ``<script>``
    blocks.

    Returns a dict with ``api_url``, ``ccid``, and ``api_key`` keys,
    or ``None`` if extraction fails.
    """
    text = response.text

    m = re.search(
        r"var\s+SEARCH_SERVICE\s*=\s*(\{[^;]+\})\s*;",
        text,
    )
    if not m:
        return None
    try:
        cfg = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

    api_url = cfg.get("apiUrl")
    ccid = str(cfg.get("ccid", ""))
    api_key = cfg.get("apiKey")
    if not (api_url and ccid and api_key):
        return None

    return {
        "api_url": api_url,
        "ccid": ccid,
        "api_key": api_key,
    }


def _extract_field_map(response: HtmlResponse) -> dict[str, str]:
    """Extract the ``SEARCH_SERVICE_FIELD_MAP.indexed`` mapping.

    Falls back to :data:`_DEFAULT_FIELD_MAP` if not found.
    """
    text = response.text

    m = re.search(
        r"var\s+SEARCH_SERVICE_FIELD_MAP\s*=\s*(\{.+?\})\s*;\s*\n",
        text,
        re.DOTALL,
    )
    if not m:
        return dict(_DEFAULT_FIELD_MAP)
    try:
        fm = json.loads(m.group(1))
        indexed = fm.get("indexed", {})
        if indexed:
            return indexed
    except json.JSONDecodeError:
        pass
    return dict(_DEFAULT_FIELD_MAP)


def _extract_per_page(response: HtmlResponse) -> int:
    """Extract ``hitsPerPage`` from embedded JS."""
    text = response.text
    m = re.search(r"var\s+PARAMS\s*=\s*(\{.+?\})\s*;", text)
    if m:
        try:
            params = json.loads(m.group(1))
            return int(params.get("hitsPerPage", _DEFAULT_PER_PAGE))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return _DEFAULT_PER_PAGE


def _extract_refinements(response: HtmlResponse) -> dict[str, list[str]]:
    """Extract refinements from ``inventoryLightningSettings`` and URL params.

    Merges refinements from the JS variable with ``_dFR`` query parameters
    from the URL.
    """
    text = response.text
    refinements: dict[str, list[str]] = {}

    m = re.search(
        r"var\s+inventoryLightningSettings\s*=\s*(\{.+?\})\s*;\s*\n",
        text,
        re.DOTALL,
    )
    if m:
        try:
            ils = json.loads(m.group(1))
            refinements = ils.get("refinements", {})
        except json.JSONDecodeError:
            pass

    # Merge URL _dFR parameters.
    url_refinements = _extract_url_refinements(response.url)
    for field, vals in url_refinements.items():
        if field not in refinements:
            refinements[field] = vals
        else:
            existing = set(refinements[field])
            for v in vals:
                if v not in existing:
                    refinements[field].append(v)

    return refinements


def _extract_url_refinements(url: str) -> dict[str, list[str]]:
    """Parse ``_dFR[field][index]=value`` query parameters into a refinements dict.

    DealerInspire encodes active facet filters in the URL as
    ``_dFR[year][0]=2026&_dFR[model][0]=RAV4``, etc.  This function
    extracts them so they can be merged with the refinements from
    ``inventoryLightningSettings``.
    """
    refinements: dict[str, list[str]] = {}
    qs = parse_qs(urlparse(url).query)
    pattern = re.compile(r"^_dFR\[([^\]]+)\]\[\d+\]$")
    for key, values in qs.items():
        m = pattern.match(key)
        if m:
            field = m.group(1)
            refinements.setdefault(field, [])
            for v in values:
                if v not in refinements[field]:
                    refinements[field].append(v)
    return refinements


# ---------------------------------------------------------------------------
# Helpers — Refinements -> facetFilters
# ---------------------------------------------------------------------------


def _map_refinements(
    refinements: dict[str, list[str]],
    field_map: dict[str, str],
) -> dict[str, list]:
    """Map refinements from legacy field names to Search Service field names.

    The ``field_map`` is the ``SEARCH_SERVICE_FIELD_MAP.indexed`` dict
    that maps Algolia-era field names to the new Search Service names
    (e.g. ``type`` -> ``type_slug``, ``model`` -> ``model_slug``).

    Values for the ``year`` field are converted to integers.

    Returns a dict suitable for the ``facetFilters`` key in the API request.
    """
    result: dict[str, list] = {}
    for field, vals in refinements.items():
        mapped = field_map.get(field, field.lower())
        converted = []
        for v in vals:
            if mapped == "year" or field == "year":
                try:
                    converted.append(int(v))
                except (ValueError, TypeError):
                    converted.append(v)
            else:
                converted.append(v)
        if mapped in result:
            result[mapped].extend(converted)
        else:
            result[mapped] = converted
    return result


# ---------------------------------------------------------------------------
# Helpers — Search Service listing -> CarItem
# ---------------------------------------------------------------------------

def _listing_to_item(
    listing: dict,
    dealer_name: str,
    dealer_url: str,
) -> CarItem | None:
    """Convert a Search Service listing into a :class:`CarItem`."""
    vin = listing.get("vin")
    if not vin:
        return None

    item = CarItem()

    # --- Identifiers ---
    item["vin"] = vin
    item["stock_number"] = listing.get("stock") or None
    item["model_code"] = (
        listing.get("manufacturer_model_code")
        or listing.get("model_number")
        or None
    )

    # --- Vehicle info ---
    item["year"] = str(listing["year"]) if listing.get("year") else None
    item["trim"] = listing.get("trim") or None

    mechanical = listing.get("mechanical") or {}
    item["drivetrain"] = normalize_drivetrain(mechanical.get("drivetrain", ""))

    # --- Colors ---
    styles = listing.get("styles") or {}
    item["exterior_color"] = normalize_color(styles.get("exterior_color"))
    item["interior_color"] = normalize_color(styles.get("interior_color"))

    # --- Pricing ---
    pricing = listing.get("pricing") or {}
    msrp = parse_price(pricing.get("msrp"))
    our_price = (
        parse_price(pricing.get("our_price"))
        or parse_price(pricing.get("price"))
    )
    item["msrp"] = msrp
    item["total_price"] = our_price or msrp

    # --- Packages ---
    item["packages"] = None  # not available in Search Service

    # --- Status ---
    extra = listing.get("extra_fields") or {}
    lightning = extra.get("lightning") or {}
    item["status"] = _extract_status(listing, lightning)

    # --- Availability date ---
    item["availability_date"] = lightning.get("statusETA") or None

    # --- Dealer / links ---
    item["dealer_name"] = dealer_name
    item["dealer_url"] = dealer_url
    item["detail_url"] = listing.get("vdp_url") or None

    return item


def _extract_status(listing: dict, lightning: dict) -> str | None:
    """Determine vehicle availability status from the listing."""
    label = lightning.get("statusLabel", "")
    if label:
        return _normalize_status(label)

    raw = lightning.get("status", "")
    if raw:
        return _normalize_status(raw)

    return None


def _normalize_status(text: str) -> str:
    """Normalise common status labels to a consistent form."""
    lower = text.strip().lower().replace("-", " ")
    if "transit" in lower:
        return "In Transit"
    if "build" in lower:
        return "In Production"
    if "stock" in lower:
        return "In Stock"
    return text.strip()
