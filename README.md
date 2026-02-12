# Car Inventory Scraper

Scrape car dealership websites to build an inventory database. Uses [Scrapy](https://scrapy.org/) with [Playwright](https://playwright.dev/) for JavaScript-heavy sites.

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
headless = true
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

# Install Playwright browsers (required once):
uv run playwright install chromium

# Run a crawl:
uv run car-inventory-scraper crawl dealeron \
    --url "https://www.toyotaofbellevue.com/searchnew.aspx?Make=Toyota&ModelAndTrim=RAV4"
```

## CLI Reference

```
car-inventory-scraper [OPTIONS] COMMAND [ARGS]...

Commands:
  crawl   Scrape dealership inventory (single URL or config file).
  list    List available spiders.
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
  --headless / --no-headless
                           Run browser headless (default: headless)
```

## Output

By default, results are written to `inventory.jsonl` — one JSON object per vehicle:

```json
{
  "vin": "JTM6CRAV0TD002084",
  "stock_number": null,
  "year": "2026",
  "make": "Toyota",
  "model": "RAV4",
  "trim": "SE",
  "exterior_color": "Storm Cloud",
  "interior_color": null,
  "msrp": 38264,
  "price": 38264,
  "status": "In Transit",
  "availability_date": "03/05/26",
  "dealer_name": "Toyota of Bellevue",
  "dealer_url": "https://www.toyotaofbellevue.com/searchnew.aspx?...",
  "detail_url": "https://www.toyotaofbellevue.com/new-Bellevue-2026-Toyota-RAV4-SE-JTM6CRAV0TD002084",
  "scraped_at": "2026-02-07T12:00:00+00:00"
}
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
├── items.py             # CarItem definition
├── pipelines.py         # Text cleaning, timestamps
├── settings.py          # Scrapy + Playwright config
└── spiders/
    ├── __init__.py
    └── dealeron.py      # Spider for DealerOn-powered sites
```

## License

MIT
