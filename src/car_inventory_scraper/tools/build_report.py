"""Convert a scraped-inventory JSON file into a styled HTML report.

Usage as a CLI (via the main entry-point)::

    car-inventory-scraper report inventory.json -o inventory.html

Usage from Python::

    from car_inventory_scraper.tools.build_report import build_report
    build_report("inventory.json", "inventory.html")

Or run directly::

    uv run python -m car_inventory_scraper.tools.build_report inventory.json
"""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

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
    ("Dlr Accessories", "dealer_accessories_price"),
    ("Adjustments", "adjustments"),
    ("Advertised Price", "total_price"),
    ("Status", "status"),
    ("Avail. Date", "availability_date"),
    ("Packages", None),
    ("VIN", None),
]


def build_report(
    input_path: str,
    output_path: str = "inventory.html",
    *,
    hide_dealer: bool = False,
) -> None:
    """Read *input_path* (JSON) and write a styled HTML table to *output_path*."""
    data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    if not data:
        print("No items in JSON file — skipping HTML report.")
        return

    # Sort by total price ascending; vehicles without a price go last.
    data.sort(key=lambda it: (it.get("total_price") is None, it.get("total_price") or 0))

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    columns = [
        (h, k) for h, k in COLUMNS
        if not (hide_dealer and k == "dealer_name")
    ]

    rows = []
    for item in data:
        cells = []
        for header, key in columns:
            if header == "Packages":
                pkgs = item.get("packages") or []
                parts = []
                for p in pkgs:
                    name = html.escape(p.get("name", ""))
                    price = html.escape(str(p.get("price", "") or ""))
                    parts.append(f"{name} ({price})" if price else name)
                cells.append(
                    f'      <td class="packages">{"<br>".join(parts)}</td>'
                )
            elif header == "VIN":
                vin = html.escape(item.get("vin") or "")
                detail = item.get("detail_url", "")
                if detail:
                    cells.append(
                        f'      <td><a href="{html.escape(detail)}" target="_blank"'
                        f' rel="noopener">{vin}</a></td>'
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
                cells.append(
                    f'      <td{css} data-sort-value="{sort_val}">'
                    f"{html.escape(val)}</td>"
                )
            elif key in (
                "base_price", "total_packages_price", "msrp",
                "dealer_accessories_price", "total_price",
            ):
                raw = item.get(key)
                val = f"${raw:,}" if isinstance(raw, (int, float)) else ""
                if key == "total_price":
                    cls = ' class="price"'
                elif key == "dealer_accessories_price" and raw:
                    cls = ' class="adj-pos"'
                else:
                    cls = ""
                sort_val = raw if isinstance(raw, (int, float)) else 0
                cells.append(
                    f'      <td{cls} data-sort-value="{sort_val}">'
                    f"{html.escape(val)}</td>"
                )
            else:
                val = item.get(key) or ""
                cells.append(f"      <td>{html.escape(str(val))}</td>")
        rows.append("    <tr>\n" + "\n".join(cells) + "\n    </tr>")

    headers = "".join(f"<th>{h}</th>" for h, _ in columns)

    trim_table = _build_trim_by_dealer_table(data)

    page = _HTML_TEMPLATE.format(
        timestamp=ts,
        count=len(data),
        headers=headers,
        rows="\n".join(rows),
        trim_by_dealer_table=trim_table,
    )

    Path(output_path).write_text(page, encoding="utf-8")
    print(f"HTML report written to {output_path} ({len(data)} vehicles)")


def _build_trim_by_dealer_table(data: list[dict]) -> str:
    """Return an HTML table aggregating vehicle counts by dealer and trim."""
    counts: Counter[tuple[str, str]] = Counter()
    dealers_set: set[str] = set()
    trim_min_price: dict[str, float] = {}

    for item in data:
        dealer = item.get("dealer_name") or "Unknown"
        trim = item.get("trim") or "Unknown"
        counts[(dealer, trim)] += 1
        dealers_set.add(dealer)
        bp = item.get("base_price")
        if isinstance(bp, (int, float)):
            if trim not in trim_min_price or bp < trim_min_price[trim]:
                trim_min_price[trim] = bp

    dealers = sorted(dealers_set)
    # Order trims by their minimum base price; trims without a price go last.
    trims = sorted(
        trim_min_price.keys() | (set(counts.keys()) and {t for _, t in counts}),
        key=lambda t: (t not in trim_min_price, trim_min_price.get(t, 0), t),
    )

    if not dealers or not trims:
        return ""

    # Header row
    hdr = "<th>Dealer</th>" + "".join(f"<th>{html.escape(t)}</th>" for t in trims) + "<th>Total</th>"

    # Body rows
    body_rows: list[str] = []
    trim_totals = {t: 0 for t in trims}
    for dealer in dealers:
        cells = [f"      <td>{html.escape(dealer)}</td>"]
        row_total = 0
        for trim in trims:
            n = counts.get((dealer, trim), 0)
            row_total += n
            trim_totals[trim] += n
            cells.append(f"      <td>{n if n else ''}</td>")
        cells.append(f'      <td class="price">{row_total}</td>')
        body_rows.append("    <tr>\n" + "\n".join(cells) + "\n    </tr>")

    # Footer totals row
    grand_total = sum(trim_totals.values())
    footer_cells = ['      <td class="price">Total</td>']
    for trim in trims:
        footer_cells.append(f'      <td class="price">{trim_totals[trim]}</td>')
    footer_cells.append(f'      <td class="price">{grand_total}</td>')
    footer_row = "    <tr>\n" + "\n".join(footer_cells) + "\n    </tr>"

    return (
        '<table id="trim-table">\n'
        f"    <thead><tr>{hdr}</tr></thead>\n"
        "    <tbody>\n"
        + "\n".join(body_rows) + "\n"
        + footer_row + "\n"
        "    </tbody>\n"
        "</table>"
    )


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

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

  <h2>Vehicles by Trim &amp; Dealer</h2>
  <div style="overflow-x: auto;">
  {trim_by_dealer_table}
  </div>

  <h2 style="margin-top:2.5rem;">Full Inventory</h2>
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
      arrow.textContent = '\\u25B2';
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
        th.querySelector('.sort-arrow').textContent = asc ? '\\u25B2' : '\\u25BC';
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


# ---------------------------------------------------------------------------
# Allow running as ``python -m car_inventory_scraper.tools.build_report``
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a scraped-inventory JSON file to an HTML report.",
    )
    parser.add_argument("input", help="Path to the JSON inventory file.")
    parser.add_argument(
        "-o", "--output",
        default="inventory.html",
        help="Output HTML file path (default: inventory.html).",
    )
    parser.add_argument(
        "--hide-dealer",
        action="store_true",
        help="Hide the Dealer column in the report.",
    )
    args = parser.parse_args()
    build_report(args.input, args.output, hide_dealer=args.hide_dealer)


if __name__ == "__main__":
    _cli()
