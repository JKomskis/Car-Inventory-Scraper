"""Item pipelines for car inventory scraper."""

from __future__ import annotations

import html
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


class HtmlReportPipeline:
    """Collect items and write a styled HTML table on spider close."""

    COLUMNS = [
        ("Dealer", "dealer_name"),
        ("Stock #", "stock_number"),
        ("Year", "year"),
        ("Trim", "trim"),
        ("Drivetrain", "drivetrain"),
        ("Ext. Color", "exterior_color"),
        ("Int. Color", "interior_color"),
        ("Base Price", "base_price"),
        ("Pkgs Total", "total_packages_price"),
        ("MSRP", "msrp"),
        ("Dlr Accessories", "dealer_accessories"),
        ("Adjustments", "adjustments"),
        ("Advertised Price", "total_price"),
        ("Status", "status"),
        ("Avail. Date", "availability_date"),
        ("Packages", None),
        ("VIN", None),
    ]

    # Class-level shared state so items from multiple spiders (multi-dealer
    # mode) are accumulated into a single report rather than overwriting
    # each other.
    _all_items: list[dict] = []
    _total_expected: int = 1
    _completed_count: int = 0

    def __init__(self, output_path: str = "inventory.html", hide_dealer: bool = False):
        self.output_path = output_path
        self.hide_dealer = hide_dealer
        self.logger = logging.getLogger(__name__)

    @classmethod
    def from_crawler(cls, crawler):
        output = crawler.settings.get("HTML_REPORT_PATH", "inventory.html")
        hide_dealer = crawler.settings.getbool("HIDE_DEALER_COLUMN", False)
        cls._total_expected = crawler.settings.getint("TOTAL_SPIDER_COUNT", 1)
        return cls(output_path=output, hide_dealer=hide_dealer)

    def open_spider(self, spider):
        pass

    def process_item(self, item, spider):
        HtmlReportPipeline._all_items.append(dict(item))
        return item

    def close_spider(self, spider):
        HtmlReportPipeline._completed_count += 1
        if HtmlReportPipeline._completed_count < HtmlReportPipeline._total_expected:
            self.logger.info(
                "Spider '%s' finished (%d items so far, %d/%d spiders done).",
                spider.name,
                len(HtmlReportPipeline._all_items),
                HtmlReportPipeline._completed_count,
                HtmlReportPipeline._total_expected,
            )
            return

        items = HtmlReportPipeline._all_items
        # Reset class-level state for a clean slate if the process is reused.
        HtmlReportPipeline._all_items = []
        HtmlReportPipeline._completed_count = 0

        if not items:
            self.logger.info("No items scraped \u2014 skipping HTML report.")
            return

        self._write_report(items)

    def _write_report(self, items: list[dict]) -> None:
        """Render *items* into an HTML table and write it to disk."""
        # Sort by total price ascending; vehicles without a price go last.
        items.sort(key=lambda it: (it.get("total_price") is None, it.get("total_price") or 0))

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        columns = [
            (h, k) for h, k in self.COLUMNS
            if not (self.hide_dealer and k == "dealer_name")
        ]

        rows = []
        for item in items:
            cells = []
            for header, key in columns:
                if header == "Packages":
                    pkgs = item.get("packages") or []
                    parts = []
                    for p in pkgs:
                        name = html.escape(p.get("name", ""))
                        price = html.escape(p.get("price", "") or "")
                        parts.append(f"{name} ({price})" if price else name)
                    cells.append(
                        f'      <td class="packages">{"<br>".join(parts)}</td>'
                    )
                elif header == "VIN":
                    vin = html.escape(item.get("vin") or "")
                    detail = item.get("detail_url", "")
                    if detail:
                        cells.append(
                            f'      <td><a href="{html.escape(detail)}" target="_blank" rel="noopener">{vin}</a></td>'
                        )
                    else:
                        cells.append(f"      <td>{vin}</td>")
                elif key == "adjustments":
                    raw = item.get(key)
                    if raw is None or raw == 0:
                        val = ""
                    elif raw < 0:
                        val = f"-${abs(raw):,}"
                    else:
                        val = f"+${raw:,}"
                    css = (
                        ' class="adj-neg"' if raw and raw < 0
                        else ' class="adj-pos"' if raw and raw > 0
                        else ""
                    )
                    sort_val = raw if raw else 0
                    cells.append(f'      <td{css} data-sort-value="{sort_val}">{html.escape(val)}</td>')
                elif key in ("base_price", "total_packages_price", "msrp", "dealer_accessories", "total_price"):
                    raw = item.get(key)
                    val = f"${raw:,}" if isinstance(raw, (int, float)) else ""
                    if key == "total_price":
                        cls = ' class="price"'
                    elif key == "dealer_accessories" and raw:
                        cls = ' class="adj-pos"'
                    else:
                        cls = ""
                    sort_val = raw if isinstance(raw, (int, float)) else 0
                    cells.append(f'      <td{cls} data-sort-value="{sort_val}">{html.escape(val)}</td>')
                else:
                    val = item.get(key) or ""
                    cells.append(f"      <td>{html.escape(str(val))}</td>")
            rows.append("    <tr>\n" + "\n".join(cells) + "\n    </tr>")

        headers = "".join(f"<th>{h}</th>" for h, _ in columns)

        page = _HTML_TEMPLATE.format(
            timestamp=ts,
            count=len(items),
            headers=headers,
            rows="\n".join(rows),
        )

        Path(self.output_path).write_text(page, encoding="utf-8")
        self.logger.info(
            "HTML report written to %s (%d vehicles)",
            self.output_path,
            len(items),
        )


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Vehicle Inventory</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      margin: 2rem;
      color: #1a1a1a;
      background: #f8f9fa;
    }}
    h1 {{ margin-bottom: 0.25rem; }}
    .meta {{ color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }}
    table {{
      border-collapse: collapse;
      width: 100%;
      background: #fff;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1);
      font-size: 0.85rem;
    }}
    th, td {{
      padding: 0.6rem 0.75rem;
      text-align: left;
      border-bottom: 1px solid #e9ecef;
      white-space: nowrap;
    }}
    th {{
      background: #343a40;
      color: #fff;
      font-weight: 600;
      position: sticky;
      top: 0;
      cursor: pointer;
      user-select: none;
    }}
    th:hover {{ background: #495057; }}
    th .sort-arrow {{ font-size: 0.65em; margin-left: 0.3em; opacity: 0.4; }}
    th.sort-asc .sort-arrow,
    th.sort-desc .sort-arrow {{ opacity: 1; }}
    tr:hover {{ background: #f1f3f5; }}
    .price {{ font-weight: 600; }}
    .adj-neg {{ color: #28a745; font-weight: 600; }}
    .adj-pos {{ color: #dc3545; font-weight: 600; }}
    a {{ color: #0d6efd; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .packages {{ white-space: nowrap; font-size: 0.8rem; line-height: 1.6; }}
  </style>
</head>
<body>
  <h1>Vehicle Inventory</h1>
  <p class="meta">{count} vehicles &middot; scraped {timestamp}</p>
  <div style="overflow-x: auto;">
  <table id="inv-table">
    <thead><tr>{headers}</tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
  </div>
  <script>
  (function () {{
    var table = document.getElementById('inv-table');
    var thead = table.querySelector('thead');
    var tbody = table.querySelector('tbody');
    var ths = thead.querySelectorAll('th');
    ths.forEach(function (th) {{
      var arrow = document.createElement('span');
      arrow.className = 'sort-arrow';
      arrow.textContent = '\u25B2';
      th.appendChild(arrow);
    }});
    var curCol = -1, asc = true;
    ths.forEach(function (th, idx) {{
      th.addEventListener('click', function () {{
        if (curCol === idx) {{ asc = !asc; }}
        else {{
          ths.forEach(function (h) {{ h.classList.remove('sort-asc', 'sort-desc'); }});
          curCol = idx; asc = true;
        }}
        th.classList.toggle('sort-asc', asc);
        th.classList.toggle('sort-desc', !asc);
        th.querySelector('.sort-arrow').textContent = asc ? '\u25B2' : '\u25BC';
        var rows = Array.from(tbody.querySelectorAll('tr'));
        rows.sort(function (a, b) {{
          var cA = a.children[idx], cB = b.children[idx];
          var sA = cA.getAttribute('data-sort-value');
          var sB = cB.getAttribute('data-sort-value');
          if (sA !== null && sB !== null) {{
            var nA = parseFloat(sA) || 0, nB = parseFloat(sB) || 0;
            return asc ? nA - nB : nB - nA;
          }}
          var tA = (cA.textContent || '').trim().toLowerCase();
          var tB = (cB.textContent || '').trim().toLowerCase();
          if (tA < tB) return asc ? -1 : 1;
          if (tA > tB) return asc ? 1 : -1;
          return 0;
        }});
        rows.forEach(function (r) {{ tbody.appendChild(r); }});
      }});
    }});
  }})();
  </script>
</body>
</html>
"""
