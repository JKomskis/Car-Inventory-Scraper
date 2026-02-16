"""Item pipelines for car inventory scraper."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path


class CleanTextPipeline:
    """Strip whitespace and normalise text fields."""

    TEXT_FIELDS = {
        "vin",
        "stock_number",
        "model_code",
        "trim",
        "drivetrain",
        "exterior_color",
        "interior_color",
        "status",
        "dealer_name",
    }

    _DRIVETRAIN_ALIASES = {
        "4WD": "AWD",
        "4X4": "AWD",
        "4WD/AWD": "AWD",
    }

    _TRIM_ALIASES = {
        "WOODLAND": "Woodland",
        "XLE": "XLE Premium",
    }

    def process_item(self, item):
        for field in self.TEXT_FIELDS:
            value = item.get(field)
            if isinstance(value, str):
                # collapse whitespace and strip
                item[field] = re.sub(r"\s+", " ", value).strip()

        # Normalise drivetrain values
        dt = item.get("drivetrain")
        if isinstance(dt, str):
            item["drivetrain"] = self._DRIVETRAIN_ALIASES.get(dt.upper(), dt)

        # Normalise trim values
        trim = item.get("trim")
        if isinstance(trim, str):
            item["trim"] = self._TRIM_ALIASES.get(trim, trim)

        # Normalise prices to plain integers
        for price_field in ("msrp", "base_price", "total_packages_price", "dealer_accessories_price", "total_price"):
            raw = item.get(price_field)
            if isinstance(raw, str):
                digits = re.sub(r"[^\d]", "", raw)
                item[price_field] = int(digits) if digits else None

        # adjustments can be negative
        adj = item.get("adjustments")
        if isinstance(adj, str):
            negative = "-" in adj
            digits = re.sub(r"[^\d]", "", adj)
            if digits:
                item["adjustments"] = -int(digits) if negative else int(digits)
            else:
                item["adjustments"] = None

        return item


class PackageFilterPipeline:
    """Exclude unwanted packages and classify dealer accessories.

    Runs after text cleaning but before price calculations so that
    ``CalculatedPricesPipeline`` sees the final package lists.

    1. Drops any package whose normalised name is in ``EXCLUDED_PACKAGES``.
    2. Moves packages whose normalised name is in ``DEALER_ACCESSORY_NAMES``
       from ``packages`` into ``dealer_accessories``.
    """

    def process_item(self, item, spider):
        from car_inventory_scraper.parsing_helpers import (
            DEALER_ACCESSORY_NAMES,
            EXCLUDED_PACKAGES,
            normalize_pkg_name,
        )

        raw_packages = item.get("packages") or []
        if not raw_packages:
            return item

        kept: list[dict] = []
        dealer_acc: list[dict] = list(item.get("dealer_accessories") or [])

        for pkg in raw_packages:
            name = normalize_pkg_name(pkg.get("name", "")).lower()
            if name in EXCLUDED_PACKAGES:
                continue
            if name in DEALER_ACCESSORY_NAMES:
                dealer_acc.append(pkg)
            else:
                kept.append(pkg)

        item["packages"] = kept or None
        item["dealer_accessories"] = dealer_acc or None
        return item


class CalculatedPricesPipeline:
    """Compute derived pricing fields from raw item data.

    Calculates fields that follow the same formula across all spiders:

    - ``total_packages_price`` — sum of package prices from the *packages* list.
    - ``dealer_accessories_price`` — sum of prices from the
      *dealer_accessories* list.
    - ``base_price`` — ``msrp − total_packages_price``.
    - ``adjustments`` — ``total_price − msrp − dealer_accessories_price``
      (dealer discounts / markups not explained by accessories).

    Spiders may pre-set any of these fields to override the default
    calculation (e.g. when a platform-specific data source provides a
    more accurate value).
    """

    def process_item(self, item, spider):
        # --- total_packages_price ---
        if item.get("total_packages_price") is None:
            packages = item.get("packages") or []
            total = sum(p.get("price") or 0 for p in packages)
            item["total_packages_price"] = total or None

        # --- dealer_accessories_price ---
        if item.get("dealer_accessories_price") is None:
            accessories = item.get("dealer_accessories") or []
            total = sum(p.get("price") or 0 for p in accessories)
            item["dealer_accessories_price"] = total or None

        # --- base_price ---
        if item.get("base_price") is None:
            msrp = item.get("msrp")
            total_pkg = item.get("total_packages_price") or 0
            item["base_price"] = (msrp - total_pkg) if msrp else None

        # --- adjustments ---
        if item.get("adjustments") is None:
            msrp = item.get("msrp")
            total_price = item.get("total_price")
            if msrp and total_price and total_price != msrp:
                dealer_acc = item.get("dealer_accessories_price") or 0
                adj = total_price - msrp - dealer_acc
                item["adjustments"] = adj if adj else None
            else:
                item["adjustments"] = None

        return item


class JsonReportPipeline:
    """Collect items and write them to a JSON file on spider close."""

    # Class-level shared state so items from multiple spiders (multi-dealer
    # mode) are accumulated into a single file rather than overwriting
    # each other.
    _all_items: list[dict] = []
    _total_expected: int = 1
    _completed_count: int = 0

    def __init__(self, output_path: str = "inventory.json"):
        self.output_path = output_path
        self.logger = logging.getLogger(__name__)

    @classmethod
    def from_crawler(cls, crawler):
        output = crawler.settings.get("JSON_REPORT_PATH", "inventory.json")
        cls._total_expected = crawler.settings.getint("TOTAL_SPIDER_COUNT", 1)
        return cls(output_path=output)

    @staticmethod
    def _normalise(obj):
        """Recursively sort arrays of objects for deterministic output."""
        if isinstance(obj, dict):
            return {k: JsonReportPipeline._normalise(v) for k, v in obj.items()}
        if isinstance(obj, list):
            normalised = [JsonReportPipeline._normalise(item) for item in obj]
            try:
                normalised.sort(key=lambda x: json.dumps(x, sort_keys=True, default=str))
            except TypeError:
                pass
            return normalised
        return obj

    def open_spider(self, spider):
        pass

    def process_item(self, item, spider):
        JsonReportPipeline._all_items.append(dict(item))
        return item

    def close_spider(self, spider):
        JsonReportPipeline._completed_count += 1
        if JsonReportPipeline._completed_count < JsonReportPipeline._total_expected:
            self.logger.info(
                "Spider '%s' finished (%d items so far, %d/%d spiders done).",
                spider.name,
                len(JsonReportPipeline._all_items),
                JsonReportPipeline._completed_count,
                JsonReportPipeline._total_expected,
            )
            return

        items = JsonReportPipeline._all_items
        # Reset class-level state for a clean slate if the process is reused.
        JsonReportPipeline._all_items = []
        JsonReportPipeline._completed_count = 0

        if not items:
            self.logger.info("No items scraped \u2014 skipping JSON output.")
            return

        # Sort by VIN for consistent ordering
        items.sort(key=lambda x: x.get("vin") or "")

        # Normalise for deterministic output: sort object keys and
        # nested arrays so diffs stay minimal across runs.
        items = [self._normalise(item) for item in items]

        Path(self.output_path).write_text(
            json.dumps(items, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        self.logger.info(
            "JSON report written to %s (%d vehicles)",
            self.output_path,
            len(items),
        )
