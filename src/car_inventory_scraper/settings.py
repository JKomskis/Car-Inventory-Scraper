"""Scrapy settings for the car inventory scraper."""

BOT_NAME = "car_inventory_scraper"

SPIDER_MODULES = ["car_inventory_scraper.spiders"]
NEWSPIDER_MODULE = "car_inventory_scraper.spiders"
TELNETCONSOLE_ENABLED = False

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

# --- Polite crawling ---
ROBOTSTXT_OBEY = False
CONCURRENT_REQUESTS = 16
CONCURRENT_REQUESTS_PER_DOMAIN = 1
DOWNLOAD_DELAY = 2  # seconds between requests to the same domain
RANDOMIZE_DOWNLOAD_DELAY = True

# --- Timeouts & retries ---
DOWNLOAD_TIMEOUT = 180  # Scrapy-level hard cap per request (seconds)
RETRY_TIMES = 2  # retry transient failures (timeouts, 5xx, etc.)
RETRY_HTTP_CODES = [500, 502, 503, 504, 408]

# --- Download handlers ---
DOWNLOAD_HANDLERS = {
    # CloudScraper-aware HTTPS handler — set meta["use_cloudscraper"]=True
    # on individual requests to route them through cloudscraper and bypass
    # Cloudflare bot-detection.  All other requests use the default HTTP client.
    "http": "car_inventory_scraper.handler.CloudScraperHandler",
    "https": "car_inventory_scraper.handler.CloudScraperHandler",
}

# --- Pipelines ---
ITEM_PIPELINES = {
    "car_inventory_scraper.pipelines.CleanTextPipeline": 100,
    "car_inventory_scraper.pipelines.CalculatedPricesPipeline": 150,
    "car_inventory_scraper.pipelines.JsonReportPipeline": 900,
}

# Default output path for the JSON data (override via CLI --output)
JSON_REPORT_PATH = "inventory.json"

# --- Misc ---
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
LOG_LEVEL = "INFO"
