"""Item pipelines for car inventory scraper."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
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

        # Normalise prices to plain integers
        for price_field in ("msrp", "base_price", "total_packages_price", "dealer_accessories", "total_price"):
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


class TimestampPipeline:
    """Add a UTC timestamp to every item."""

    def process_item(self, item):
        item["scraped_at"] = datetime.now(timezone.utc).isoformat()
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

        Path(self.output_path).write_text(
            json.dumps(items, indent=2, default=str),
            encoding="utf-8",
        )
        self.logger.info(
            "JSON report written to %s (%d vehicles)",
            self.output_path,
            len(items),
        )
