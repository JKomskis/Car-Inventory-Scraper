"""Spider for DealerInspire-powered dealership websites.

DealerInspire is a dealership website platform that uses Algolia
InstantSearch for its inventory search pages.  The Algolia credentials
(application ID, search-only API key, and index name) are embedded in
the page's inline JavaScript, along with the active refinements
(make, model, year, type, etc.).

This spider:

1. **Fetches the SRP page** with ``use_cloudscraper`` meta
   to bypass Cloudflare bot-detection and extract the Algolia config from
   embedded ``<script>`` variables (``algoliaConfig``,
   ``inventoryLightningSettings``, ``PARAMS``).
2. **Queries the Algolia search API** directly over HTTPS to retrieve
   the full vehicle inventory in JSON.
3. **Maps each Algolia hit** to a :class:`CarItem`.

Unlike other platforms, packages are not available in the Algolia
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

# Default Algolia page size — overridden from the ``PARAMS`` JS variable
# when available.
_DEFAULT_HITS_PER_PAGE = 20


class DealerInspireSpider(scrapy.Spider):
    """Scrape vehicle inventory from a DealerInspire-powered dealership site."""

    name = "dealerinspire"

    # Passed via ``-a url=…`` or the CLI wrapper.
    def __init__(
        self,
        url: str | None = None,
        dealer_name: str | None = None,
        algolia_app_id: str | None = None,
        algolia_api_key: str | None = None,
        algolia_index: str | None = None,
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

        # Optional direct Algolia API credentials (bypass Cloudflare).
        self._algolia_app_id = algolia_app_id
        self._algolia_api_key = algolia_api_key
        self._algolia_index = algolia_index
        self._direct_api_mode = all(
            [algolia_app_id, algolia_api_key, algolia_index]
        )

    # ------------------------------------------------------------------
    # Step 1 — Fetch the SRP page to extract Algolia credentials
    # ------------------------------------------------------------------

    async def start(self):
        if self._direct_api_mode:
            # Mode B — skip the SRP page; query Algolia directly with
            # the credentials supplied via the dealer config.
            self.logger.info(
                "[%s] Using direct Algolia API mode (skipping SRP page)",
                self._domain,
            )
            algolia = {
                "app_id": self._algolia_app_id,
                "api_key": self._algolia_api_key,
                "index": self._algolia_index,
            }
            # Derive facet filters from _dFR query parameters in the URL.
            url_refinements = _extract_url_refinements(self.start_url)
            facet_filters = _build_facet_filters(url_refinements)

            dealer_name = self._dealer_name_override or self._domain

            yield self._algolia_request(
                algolia, facet_filters, _DEFAULT_HITS_PER_PAGE, dealer_name, page=0,
            )
        else:
            # Mode A — fetch the SRP page to extract Algolia credentials.
            yield scrapy.Request(
                self.start_url,
                meta={"use_cloudscraper": True},
                callback=self.parse_srp_for_algolia,
                errback=self.errback,
            )

    async def parse_srp_for_algolia(self, response: HtmlResponse):
        """Extract Algolia config from embedded JS and begin API queries."""
        algolia = _extract_algolia_config(response)
        if not algolia:
            self.logger.error(
                "[%s] Could not extract Algolia config on %s", self._domain, response.url,
            )
            return

        self.logger.info(
            "[%s] Algolia config: appId=%s, index=%s",
            self._domain,
            algolia["app_id"],
            algolia["index"],
        )

        # Derive dealer name from the page title if not overridden.
        dealer_name = (
            self._dealer_name_override
            or response.css("title::text").get("").split("|")[-1].strip()
        )

        # Build facet filters from the refinements embedded in
        # ``inventoryLightningSettings``.
        facet_filters = _build_facet_filters(algolia.get("refinements", {}))

        hits_per_page = algolia.get("hits_per_page", _DEFAULT_HITS_PER_PAGE)

        # Request page 0 from Algolia (zero-indexed).
        yield self._algolia_request(
            algolia, facet_filters, hits_per_page, dealer_name, page=0,
        )

    # ------------------------------------------------------------------
    # Step 2 — Parse Algolia JSON results → CarItems
    # ------------------------------------------------------------------

    async def parse_algolia_results(self, response: JsonResponse):
        """Parse a page of Algolia search results.

        Yields a :class:`CarItem` for each vehicle hit and follows with
        the next page if more results are available.
        """
        data = json.loads(response.text)
        hits = data.get("hits", [])
        page = data.get("page", 0)
        nb_pages = data.get("nbPages", 1)
        nb_hits = data.get("nbHits", 0)
        dealer_name = response.meta["dealer_name"]
        algolia = response.meta["algolia"]

        self.logger.info(
            "[%s] Found %d vehicles (page %d/%d, total: %d)",
            self._domain,
            len(hits),
            page + 1,
            nb_pages,
            nb_hits,
        )

        for hit in hits:
            item = _algolia_hit_to_item(hit, dealer_name, self.start_url)
            if item is not None:
                yield item

        # --- Pagination ---
        if page + 1 < nb_pages:
            yield self._algolia_request(
                algolia,
                response.meta["facet_filters"],
                response.meta["hits_per_page"],
                dealer_name,
                page=page + 1,
            )

    # ------------------------------------------------------------------
    # Algolia request builder
    # ------------------------------------------------------------------

    def _algolia_request(
        self,
        algolia: dict,
        facet_filters: list[list[str]],
        hits_per_page: int,
        dealer_name: str,
        page: int,
    ) -> scrapy.Request:
        """Build a Scrapy request to the Algolia search API."""
        url = (
            f"https://{algolia['app_id']}-dsn.algolia.net"
            f"/1/indexes/{algolia['index']}/query"
        )
        # Algolia expects the search parameters as a URL-encoded string
        # inside a JSON body.
        filters_json = json.dumps(facet_filters, separators=(",", ":"))
        params = f"facetFilters={filters_json}&hitsPerPage={hits_per_page}&page={page}"
        body = json.dumps({"params": params})

        return scrapy.Request(
            url,
            method="POST",
            headers={
                "X-Algolia-Application-Id": algolia["app_id"],
                "X-Algolia-API-Key": algolia["api_key"],
                "Content-Type": "application/json",
            },
            body=body,
            callback=self.parse_algolia_results,
            errback=self.errback,
            meta={
                "algolia": algolia,
                "facet_filters": facet_filters,
                "hits_per_page": hits_per_page,
                "dealer_name": dealer_name,
            },
        )

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def errback(self, failure):
        log_request_failure(failure, self._domain, self.logger)


# ---------------------------------------------------------------------------
# Helpers — Algolia config extraction
# ---------------------------------------------------------------------------

def _extract_algolia_config(response: HtmlResponse) -> dict | None:
    """Extract Algolia connection details from the SRP page's JavaScript.

    Looks for the ``algoliaConfig`` and ``inventoryLightningSettings``
    JS variables embedded in ``<script>`` blocks.

    Returns a dict with ``app_id``, ``api_key``, ``index``,
    ``refinements``, and ``hits_per_page`` keys, or ``None`` if
    extraction fails.
    """
    text = response.text

    # --- algoliaConfig ---
    # var algoliaConfig = {"appId":"…","apiKeySearch":"…","indexName":"…"};
    m = re.search(r"var\s+algoliaConfig\s*=\s*(\{[^}]+\})", text)
    if not m:
        return None
    try:
        cfg = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

    app_id = cfg.get("appId")
    api_key = cfg.get("apiKeySearch")
    base_index = cfg.get("indexName")
    if not (app_id and api_key and base_index):
        return None

    # --- inventoryLightningSettings ---
    # Contains the sort index (with a ``_status_price_low_high`` suffix)
    # and the pre-selected refinements.
    refinements: dict[str, list[str]] = {}
    sort_index = base_index  # default to the base index

    m_ils = re.search(
        r"var\s+inventoryLightningSettings\s*=\s*(\{.+?\})\s*;\s*\n",
        text,
        re.DOTALL,
    )
    if m_ils:
        try:
            ils = json.loads(m_ils.group(1))
            refinements = ils.get("refinements", {})
            sort_index = ils.get("inventoryIndex", base_index)
        except json.JSONDecodeError:
            pass

    # --- PARAMS (hitsPerPage) ---
    hits_per_page = _DEFAULT_HITS_PER_PAGE
    m_params = re.search(r"var\s+PARAMS\s*=\s*(\{.+?\})\s*;", text)
    if m_params:
        try:
            params = json.loads(m_params.group(1))
            hits_per_page = int(params.get("hitsPerPage", _DEFAULT_HITS_PER_PAGE))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # --- URL _dFR query parameters ---
    # The URL may carry additional refinements (e.g. year) as
    # ``_dFR[field][index]=value`` query parameters that are *not*
    # present in ``inventoryLightningSettings.refinements``.
    url_refinements = _extract_url_refinements(response.url)
    for field, vals in url_refinements.items():
        if field not in refinements:
            refinements[field] = vals
        else:
            # Merge without duplicates, preserving order.
            existing = set(refinements[field])
            for v in vals:
                if v not in existing:
                    refinements[field].append(v)

    return {
        "app_id": app_id,
        "api_key": api_key,
        "index": sort_index,
        "refinements": refinements,
        "hits_per_page": hits_per_page,
    }


def _extract_url_refinements(url: str) -> dict[str, list[str]]:
    """Parse ``_dFR[field][index]=value`` query parameters into a refinements dict.

    DealerInspire encodes active facet filters in the URL as
    ``_dFR[year][0]=2026&_dFR[model][0]=RAV4``, etc.  This function
    extracts them so they can be merged with the refinements from
    ``inventoryLightningSettings``.
    """
    refinements: dict[str, list[str]] = {}
    qs = parse_qs(urlparse(url).query)
    # Keys look like ``_dFR[year][0]``, ``_dFR[model][1]``, etc.
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
# Helpers — Facet filter building
# ---------------------------------------------------------------------------


def _build_facet_filters(
    refinements: dict[str, list[str]],
) -> list[list[str]]:
    """Build Algolia ``facetFilters`` from ``inventoryLightningSettings``.

    The ``refinements`` dict maps field names to lists of values, e.g.::

        {"type": ["New"], "make": ["Toyota"],
         "model": ["RAV4", "RAV4 Hybrid"]}

    This is translated to the Algolia ``facetFilters`` format::

        [["type:New"], ["make:Toyota"], ["model:RAV4","model:RAV4 Hybrid"]]

    Each field group is an OR array; the groups are AND'd together.
    """
    return [
        [f"{field}:{v}" for v in vals]
        for field, vals in refinements.items()
    ]


# ---------------------------------------------------------------------------
# Helpers — Algolia hit → CarItem
# ---------------------------------------------------------------------------

def _algolia_hit_to_item(
    hit: dict,
    dealer_name: str,
    dealer_url: str,
) -> CarItem | None:
    """Convert an Algolia hit into a :class:`CarItem`."""
    vin = hit.get("vin")
    if not vin:
        return None

    item = CarItem()

    # --- Identifiers ---
    item["vin"] = vin
    item["stock_number"] = hit.get("stock") or None
    item["model_code"] = hit.get("model_code") or hit.get("model_number") or None

    # --- Vehicle info ---
    item["year"] = str(hit["year"]) if hit.get("year") else None
    item["trim"] = hit.get("trim") or None
    item["drivetrain"] = normalize_drivetrain(hit.get("drivetrain", ""))

    # --- Colors ---
    item["exterior_color"] = normalize_color(hit.get("ext_color"))
    item["interior_color"] = normalize_color(hit.get("int_color"))

    # --- Pricing ---
    msrp = parse_price(hit.get("msrp"))
    our_price = (
        hit.get("our_price")
        if isinstance(hit.get("our_price"), int)
        else parse_price(hit.get("our_price"))
    )
    item["msrp"] = msrp
    item["total_price"] = our_price or msrp

    # --- Packages ---
    item["packages"] = None  # not available in Algolia

    # --- Status ---
    lightning = hit.get("lightning") or {}
    item["status"] = _extract_status(hit, lightning)

    # --- Availability date ---
    item["availability_date"] = lightning.get("statusETA") or None

    # --- Dealer / links ---
    item["dealer_name"] = dealer_name
    item["dealer_url"] = dealer_url
    item["detail_url"] = hit.get("link") or None

    return item


def _extract_status(hit: dict, lightning: dict) -> str | None:
    """Determine vehicle availability status from the Algolia hit.

    Prefers ``lightning.statusLabel`` (human-readable, e.g.
    "In Transit", "Build Phase") over ``vehicle_status`` (e.g.
    "In-Transit").
    """
    label = lightning.get("statusLabel", "")
    if label:
        return _normalize_status(label)

    raw = hit.get("vehicle_status", "")
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
