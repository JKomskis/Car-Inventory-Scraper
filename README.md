# Car Inventory Scraper

Scrape car dealership websites to build an inventory database, track inventory over time with daily snapshots, and visualize trends on a static dashboard.

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
output = "inventory/inventory.json"

[[dealers]]
name = "Toyota of Bellevue"
spider = "dealeron"
url = "https://www.toyotaofbellevue.com/searchnew.aspx?year=2026&make=toyota&model=rav4"

[[dealers]]
name = "Toyota of Kirkland"
spider = "dealercom"
url = "https://www.toyotaofkirkland.com/new-inventory/index.htm?year=2026&model=RAV4"

[[dealers]]
name = "Toyota of Seattle"
spider = "teamvelocity"
url = "https://www.toyotaofseattle.com/inventory/New/Toyota/RAV4?years=2026"
```

Then run all dealers at once:

```bash
uvx car-inventory-scraper crawl --config dealers.toml
```

## Makefile

```bash
make crawl        # run all spiders from dealers.toml
make build        # build the static dashboard site
make dev          # start the Eleventy dev server with hot-reload
make all          # full pipeline — crawl then build
```

## Development Setup

```bash
# Clone and install in a local venv:
cd Car-Inventory-Scraper
uv sync

# Run a single-dealer crawl:
uv run car-inventory-scraper crawl dealeron \
    --url "https://www.toyotaofbellevue.com/searchnew.aspx?Make=Toyota&ModelAndTrim=RAV4"

# Install the site's Node dependencies:
cd site && npm install
```

## CLI Reference

```
car-inventory-scraper [OPTIONS] COMMAND [ARGS]...

Commands:
  crawl    Run spiders to scrape dealership inventory.
  list     List available spiders.
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
  -o, --output TEXT        Output JSON file path (default: inventory.json)
```

## Output

Results are written to `inventory/inventory.json` (configurable via `dealers.toml` or `--output`) — a JSON array of vehicle objects:

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

### Snapshots

Daily snapshots are stored as compressed files under `inventory/<year>/<month>/`:

```
inventory/
├── inventory.json                         # latest crawl (uncompressed)
└── 2026/
    └── 02/
        ├── inventory_2026_02_16.json.gz
        └── inventory_2026_02_17.json.gz
```

The static site reads all `*.json.gz` snapshots to render historical charts and per-date inventory pages.

## Static Dashboard Site

### Building

```bash
cd site
npm install
npm run build     # outputs to site/dist/
npm run dev       # dev server with hot-reload
```

Or use the Makefile from the project root:

```bash
make build        # build the static site
make dev          # start the dev server
```

## Project Structure

```
├── dealers.toml                 # Multi-dealer scraping configuration
├── Makefile                     # crawl / build / dev / all targets
├── pyproject.toml               # Python project metadata & dependencies
├── scrapy.cfg                   # Scrapy project settings reference
├── inventory/                   # Scraped data & compressed snapshots
│   ├── inventory.json           # Latest crawl output
│   └── <year>/<month>/          # Daily .json.gz snapshots
├── site/                        # Eleventy static dashboard
│   ├── eleventy.config.js
│   ├── package.json
│   └── src/
│       ├── _data/inventory.js   # Loads snapshots for templates
│       ├── _includes/layouts/   # Nunjucks base layout
│       ├── pages/               # Dashboard & snapshot pages
│       ├── scripts/             # Chart.js setup & rendering
│       └── styles/              # CSS
└── src/car_inventory_scraper/
    ├── __init__.py
    ├── __main__.py              # python -m support
    ├── cli.py                   # Click CLI entry-point
    ├── handler.py               # CloudScraper download handler
    ├── items.py                 # CarItem definition
    ├── parsing_helpers.py       # Shared parsing & normalization utilities
    ├── pipelines.py             # Item processing pipelines
    ├── settings.py              # Scrapy configuration
    └── spiders/
        ├── dealercom.py         # Dealer.com
        ├── dealereprocess.py    # DealerEProcess
        ├── dealerinspire.py     # DealerInspire (+ Algolia)
        ├── dealeron.py          # DealerOn
        ├── dealervenom.py       # DealerVenom
        └── teamvelocity.py      # TeamVelocity
```

## License

MIT
