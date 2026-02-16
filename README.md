# Car Inventory Scraper

Scrape car dealership websites to build an inventory database.

## Quick Start with `uvx`

```bash
# Run directly without installing (uvx will create an ephemeral environment):
uvx car-inventory-scraper crawl dealeron \
    --url "https://www.toyotaofbellevue.com/searchnew.aspx?Make=Toyota&ModelAndTrim=RAV4"
```

## Multi-Dealer Config

Create a `dealers.toml` file listing all the dealerships you want to scrape:

```toml
[settings]
output = "inventory.html"

[[dealers]]
name = "Toyota of Bellevue"
spider = "dealeron"
url = "https://www.toyotaofbellevue.com/searchnew.aspx?Make=Toyota"

[[dealers]]
name = "Toyota of Kirkland"
spider = "dealeron"
url = "https://www.toyotaofkirkland.com/searchnew.aspx?Make=Toyota"
```

Then run all dealers at once:

```bash
uvx car-inventory-scraper crawl --config dealers.toml
# or specify a different config path:
uvx car-inventory-scraper crawl --config my-dealers.toml
```

## Development Setup

```bash
# Clone and install in a local venv:
cd Car-Inventory-Scraper
uv sync

# Run a crawl:
uv run car-inventory-scraper crawl dealeron \
    --url "https://www.toyotaofbellevue.com/searchnew.aspx?Make=Toyota&ModelAndTrim=RAV4"
```

## CLI Reference

```
car-inventory-scraper [OPTIONS] COMMAND [ARGS]...

Commands:
  crawl    Scrape dealership inventory (single URL or config file).
  list     List available spiders.
  report   Generate an HTML inventory report from a scraped JSON file.
```

### `crawl`

Single-dealer mode — provide a spider name and URL:

```
car-inventory-scraper crawl SPIDER_NAME --url URL [OPTIONS]
```

Multi-dealer mode — provide a TOML config file:

```
car-inventory-scraper crawl --config dealers.toml [OPTIONS]
```

```
Arguments:
  SPIDER_NAME              Name of the spider (e.g. "dealeron"); required in
                           single-dealer mode, omitted with --config.

Options:
  -u, --url TEXT           Starting URL (single-dealer mode)
  -c, --config PATH        TOML config file (multi-dealer mode)
  -o, --output TEXT        Output file path (default: inventory.html)
```

## Output

By default, results are written to `inventory.json` — a JSON array of vehicle objects:

```json
{
  "detail_url": "https://www.toyotaoflakecity.com/new-Seattle-2026-Toyota-RAV4-XLE+Premium-2T16CRAV7TC06D725",
  "dealer_name": "Toyota of Lake City",
  "dealer_url": "https://www.toyotaoflakecity.com/searchnew.aspx?...",
  "vin": "2T16CRAV7TC06D725",
  "stock_number": null,
  "model_code": "4444",
  "year": "2026",
  "trim": "XLE Premium",
  "exterior_color": "Meteor Shower",
  "interior_color": "Black Softex",
  "drivetrain": "AWD",
  "packages": [
    { "name": "Weather Package", "price": 375 },
    { "name": "All-Weather Liner Package", "price": 339 }
  ],
  "dealer_accessories": null,
  "msrp": 39664,
  "total_price": 39664,
  "status": "In Production",
  "availability_date": "03/19/26",
  "total_packages_price": 714,
  "dealer_accessories_price": null,
  "base_price": 38950,
  "adjustments": null,
  "scraped_at": "2026-02-16T06:47:11.528762+00:00"
}
```

### `report`

Generate a styled, sortable HTML report from a previously scraped JSON file:

```
car-inventory-scraper report INPUT_FILE [OPTIONS]
```

```
Arguments:
  INPUT_FILE               Path to the JSON inventory file.

Options:
  -o, --output TEXT        Output HTML file path (default: inventory.html)
  --hide-dealer            Hide the Dealer column in the report.
```

Example:

```bash
uvx car-inventory-scraper report inventory/inventory.json -o inventory.html
```

## Adding New Spiders

Each dealership platform gets its own spider in `src/car_inventory_scraper/spiders/`.

1. Create a new file, e.g. `src/car_inventory_scraper/spiders/autonation.py`
2. Subclass `scrapy.Spider` and set a unique `name`
3. Yield `CarItem` instances from your `parse` method
4. The new spider will automatically appear in `car-inventory-scraper list`

## Project Structure

```
src/car_inventory_scraper/
├── __init__.py
├── __main__.py          # python -m support
├── cli.py               # Click CLI entry-point
├── handler.py           # Scrapy signal handlers
├── items.py             # CarItem definition
├── parsing_helpers.py   # Shared parsing utilities
├── pipelines.py         # Text cleaning, timestamps
├── settings.py          # Scrapy config
├── spiders/
│   ├── __init__.py
│   ├── dealercom.py     # Spider for Dealer.com sites
│   ├── dealereprocess.py
│   ├── dealerinspire.py # Spider for DealerInspire sites
│   ├── dealeron.py      # Spider for DealerOn sites
│   ├── dealervenom.py   # Spider for DealerVenom sites
│   └── teamvelocity.py  # Spider for TeamVelocity sites
└── tools/
    ├── __init__.py
    └── build_report.py  # HTML report generator
```

## License

MIT
