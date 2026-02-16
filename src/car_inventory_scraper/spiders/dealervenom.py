"""Spider for DealerVenom-powered dealership websites.

DealerVenom is a dealership website platform that uses Typesense (via
InstantSearch.js) for its inventory search pages.  URLs typically follow
the pattern ``/new-vehicles/`` with query parameters for filters
(``model``, ``yr``, etc.).

**This spider fetches the following:**

1. **SRP page** — fetched via plain HTTP to extract the Typesense
   connection details (API key, host, collection/index name) embedded
   in the page's JavaScript.
2. **Typesense API** — queried directly over HTTPS to retrieve the full
   vehicle inventory in JSON.  Each document contains VIN, stock number,
   model code, year, make, model, trim, colors, drivetrain, MSRP,
   displayed price, status, and availability information.
3. **VDP pages** — fetched via plain HTTP only to scrape the
   server-rendered packages (``.vdp-package-item`` elements), which are
   not included in the Typesense response.

Example usage::

    car-inventory-scraper crawl dealervenom \\
        --url "https://www.burientoyota.com/new-vehicles/?model=RAV4&yr=2026"
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse, parse_qs

import scrapy
from scrapy.http import HtmlResponse, TextResponse

from car_inventory_scraper.parsing_helpers import (
    EXCLUDED_PACKAGES,
    normalize_color,
    normalize_drivetrain,
    parse_price,
    safe_int,
)
from car_inventory_scraper.items import CarItem


# Results per Typesense page — matches the default DealerVenom SRP page size.
_TYPESENSE_PAGE_SIZE = 24


class DealerVenomSpider(scrapy.Spider):
    """Scrape vehicle inventory from a DealerVenom-powered dealership site."""

    name = "dealervenom"

    # Passed via ``-a url=…`` or the CLI wrapper.
    def __init__(self, url: str | None = None, dealer_name: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if url is None:
            raise ValueError(
                "A starting URL is required. "
                "Pass it with: -a url=https://example.com/new-vehicles/"
            )
        self.start_url = url
        self._dealer_name_override = dealer_name
        self._domain = urlparse(url).netloc

    # ------------------------------------------------------------------
    # Step 1 — Fetch the SRP page to extract Typesense credentials
    # ------------------------------------------------------------------

    async def start(self):
        yield scrapy.Request(
            self.start_url,
            callback=self.parse_srp_for_typesense,
            errback=self.errback,
        )

    async def parse_srp_for_typesense(self, response: HtmlResponse):
        """Extract Typesense connection details from embedded JS and query the API."""
        ts = _extract_typesense_config(response)
        if not ts:
            self.logger.error(
                "[%s] Could not extract Typesense config on %s", self._domain, response.url,
            )
            return

        self.logger.info(
            "[%s] Typesense config: host=%s, index=%s", self._domain, ts["host"], ts["index"],
        )

        # Derive the dealer name from the page title if not overridden.
        dealer_name = (
            self._dealer_name_override
            or response.css("title::text").get("").split("|")[-1].strip()
        )

        # Build Typesense filter from query-string parameters.
        filters = _build_typesense_filter(self.start_url)

        # Request page 1 from Typesense.
        api_url = _typesense_search_url(ts, filters, page=1)
        yield scrapy.Request(
            api_url,
            headers={"X-TYPESENSE-API-KEY": ts["api_key"]},
            callback=self.parse_typesense_results,
            errback=self.errback,
            meta={
                "typesense": ts,
                "filters": filters,
                "dealer_name": dealer_name,
                "ts_page": 1,
            },
        )

    # ------------------------------------------------------------------
    # Step 2 — Parse Typesense JSON results → CarItems + VDP requests
    # ------------------------------------------------------------------

    async def parse_typesense_results(self, response: TextResponse):
        """Parse a page of Typesense search results.

        Yields a partially-filled ``CarItem`` for each vehicle, then
        follows with a plain-HTTP request to the VDP to scrape packages.
        Handles pagination automatically.
        """
        data = json.loads(response.text)
        hits = data.get("hits", [])
        found = data.get("found", 0)
        ts_page = response.meta["ts_page"]
        dealer_name = response.meta["dealer_name"]
        ts = response.meta["typesense"]
        filters = response.meta["filters"]

        self.logger.info(
            "[%s] Found %d vehicles (page %d, total: %d)",
            self._domain, len(hits), ts_page, found,
        )

        base_url = f"https://{urlparse(self.start_url).netloc}"

        for hit in hits:
            doc = hit.get("document", {})
            item = _typesense_doc_to_item(doc, dealer_name, self.start_url, base_url)

            # Request the VDP to scrape packages.
            detail_url = item["detail_url"]
            yield scrapy.Request(
                detail_url,
                callback=self.parse_vdp_packages,
                errback=self.errback,
                meta={"item": item},
            )

        # --- Pagination ---
        if ts_page * _TYPESENSE_PAGE_SIZE < found:
            next_page = ts_page + 1
            api_url = _typesense_search_url(ts, filters, page=next_page)
            yield scrapy.Request(
                api_url,
                headers={"X-TYPESENSE-API-KEY": ts["api_key"]},
                callback=self.parse_typesense_results,
                errback=self.errback,
                meta={
                    "typesense": ts,
                    "filters": filters,
                    "dealer_name": dealer_name,
                    "ts_page": next_page,
                },
            )

    # ------------------------------------------------------------------
    # Step 3 — Scrape packages from the server-rendered VDP
    # ------------------------------------------------------------------

    async def parse_vdp_packages(self, response: HtmlResponse):
        """Scrape packages from the VDP and yield the completed CarItem."""
        item: CarItem = response.meta["item"]

        packages: list[dict[str, str | int]] = []
        for pkg_el in response.css(".vdp-package-item"):
            name = pkg_el.css(".vdp-package-name::text").get("").strip()
            price_str = pkg_el.css(".vdp-package-price::text").get("").strip()
            if name and name.lower() not in EXCLUDED_PACKAGES:
                packages.append({
                    "name": name,
                    "price": parse_price(price_str),
                })
        item["packages"] = packages or None

        yield item

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def errback(self, failure):
        self.logger.error("[%s] Request failed: %s", self._domain, failure.value)


# ---------------------------------------------------------------------------
# Helpers — Typesense config extraction
# ---------------------------------------------------------------------------

def _extract_typesense_config(response: HtmlResponse) -> dict | None:
    """Extract Typesense connection details from the SRP page's JavaScript.

    Returns a dict with ``host``, ``port``, ``protocol``, ``api_key``,
    and ``index`` keys, or ``None`` if extraction fails.
    """
    text = response.text

    # Index / collection name:  var indexName = "vehicles-TOY46076";
    m_index = re.search(r'var\s+indexName\s*=\s*"([^"]+)"', text)
    if not m_index:
        return None
    index = m_index.group(1)

    # API key:  apiKey: "eQUa8iq30l8Tu908Drz9WKqar6tCJGd4",
    m_key = re.search(r'apiKey:\s*"([^"]+)"', text)
    if not m_key:
        return None
    api_key = m_key.group(1)

    # Host:  host: 'hjnrb3s21408ezpfp.a1.typesense.net',
    m_host = re.search(r"host:\s*['\"]([^'\"]+)['\"]", text)
    if not m_host:
        return None
    host = m_host.group(1)

    # Port (default 443):  port: 443,
    m_port = re.search(r"port:\s*(\d+)", text)
    port = int(m_port.group(1)) if m_port else 443

    # Protocol (default https):  protocol: 'https'
    m_proto = re.search(r"protocol:\s*['\"]([^'\"]+)['\"]", text)
    protocol = m_proto.group(1) if m_proto else "https"

    return {
        "host": host,
        "port": port,
        "protocol": protocol,
        "api_key": api_key,
        "index": index,
    }


# ---------------------------------------------------------------------------
# Helpers — Typesense query building
# ---------------------------------------------------------------------------

# Map SRP query-string parameters to Typesense field names.
_QS_TO_TYPESENSE: dict[str, str] = {
    "model": "model",
    "yr": "yr",
    "year": "yr",
    "make": "make",
    "body": "body",
    "condition": "condition",
}


def _build_typesense_filter(srp_url: str) -> str:
    """Translate the SRP query-string into a Typesense ``filter_by`` string.

    Always includes ``condition:New`` (DealerVenom new-vehicle pages
    imply this).  Additional filters are derived from recognised
    query-string parameters (``model``, ``yr``, etc.).
    """
    qs = parse_qs(urlparse(srp_url).query)
    parts: list[str] = []

    for qs_key, ts_field in _QS_TO_TYPESENSE.items():
        vals = qs.get(qs_key)
        if vals:
            parts.append(f"{ts_field}:={vals[0]}")

    # Ensure condition:New is always present.
    if not any(p.startswith("condition:") for p in parts):
        parts.insert(0, "condition:=New")

    return " && ".join(parts)


def _typesense_search_url(ts: dict, filter_by: str, page: int = 1) -> str:
    """Build a Typesense multi-search URL."""
    base = f"{ts['protocol']}://{ts['host']}:{ts['port']}"
    collection = ts["index"]
    # Typesense uses 1-based pagination.
    return (
        f"{base}/collections/{collection}/documents/search"
        f"?q=*&query_by=model&filter_by={filter_by}"
        f"&per_page={_TYPESENSE_PAGE_SIZE}&page={page}"
    )


# ---------------------------------------------------------------------------
# Helpers — Typesense document → CarItem
# ---------------------------------------------------------------------------

def _typesense_doc_to_item(
    doc: dict,
    dealer_name: str,
    dealer_url: str,
    base_url: str,
) -> CarItem:
    """Convert a Typesense document (hit) into a partially-filled CarItem.

    Packages are *not* populated here — they require a follow-up VDP
    request.
    """
    item = CarItem()

    # --- Identifiers ---
    item["vin"] = doc.get("vin")
    item["stock_number"] = doc.get("stockNumber")
    item["model_code"] = doc.get("modelCode")

    # --- Vehicle info ---
    item["year"] = str(doc["year"]) if doc.get("year") else doc.get("yr") and str(doc["yr"])
    item["trim"] = doc.get("trim")
    item["drivetrain"] = normalize_drivetrain(doc.get("drivetrain", ""))

    # --- Colors ---
    item["exterior_color"] = normalize_color(doc.get("exteriorColor"))
    item["interior_color"] = normalize_color(doc.get("interiorColor"))

    # --- Pricing ---
    item["msrp"] = parse_price(doc.get("msrp"))
    item["total_price"] = (
        safe_int(doc.get("finalPriceInt"))
        or parse_price(doc.get("finalPrice"))
        or item["msrp"]
    )

    # --- Detail URL ---
    vdp_path = doc.get("vdpUrl", "")
    item["detail_url"] = urljoin(base_url, vdp_path) if vdp_path else ""

    # --- Dealer info ---
    item["dealer_name"] = dealer_name
    item["dealer_url"] = dealer_url

    # --- Status ---
    status = doc.get("status", "")

    # Check smartpathDisclaimer for "Sale Pending" and availability date.
    disclaimer = doc.get("smartpathDisclaimer", "")
    if re.search(r"(?i)sale\s+pending", disclaimer):
        status = f"Sale Pending - {status}" if status else "Sale Pending"

    item["status"] = status

    # --- Availability date ---
    avail_match = re.search(
        r"(?i)(?:Estimated\s+)?availability\s+(\d{2}/\d{2}/\d{2,4})",
        disclaimer,
    )
    item["availability_date"] = avail_match.group(1) if avail_match else None

    return item
