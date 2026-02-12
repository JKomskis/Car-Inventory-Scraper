"""Scrapy settings for the car inventory scraper."""

BOT_NAME = "car_inventory_scraper"

SPIDER_MODULES = ["car_inventory_scraper.spiders"]
NEWSPIDER_MODULE = "car_inventory_scraper.spiders"

# --- Playwright integration ---
DOWNLOAD_HANDLERS = {
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

PLAYWRIGHT_BROWSER_TYPE = "chromium"
PLAYWRIGHT_LAUNCH_OPTIONS = {
    "headless": False,
    "args": [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
    ],
}
# Block unnecessary resource types to speed up page loads
def PLAYWRIGHT_ABORT_REQUEST(req):
    return req.resource_type in ("image", "font", "media")
# Keep the browser context alive across requests for speed
PLAYWRIGHT_CONTEXTS = {
    "default": {
        "viewport": {"width": 1920, "height": 1080},
        "ignore_https_errors": True,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }
}

# Let the browser send its own headers instead of Scrapy-generated ones,
# which look obviously non-human to bot-detection services like Cloudflare.
PLAYWRIGHT_PROCESS_REQUEST_HEADERS = None

# --- Polite crawling ---
ROBOTSTXT_OBEY = False
CONCURRENT_REQUESTS = 4
DOWNLOAD_DELAY = 2  # seconds between requests to the same domain
RANDOMIZE_DOWNLOAD_DELAY = True

# --- Timeouts & retries ---
DOWNLOAD_TIMEOUT = 90  # Scrapy-level hard cap per request (seconds)
PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = 60_000  # ms — Playwright page.goto()
RETRY_TIMES = 2  # retry transient failures (timeouts, 5xx, etc.)
RETRY_HTTP_CODES = [500, 502, 503, 504, 408]

# --- Pipelines ---
ITEM_PIPELINES = {
    "car_inventory_scraper.pipelines.CleanTextPipeline": 100,
    "car_inventory_scraper.pipelines.TimestampPipeline": 200,
    "car_inventory_scraper.pipelines.HtmlReportPipeline": 900,
}

# Default output path for the HTML report (override via CLI --output)
HTML_REPORT_PATH = "inventory.html"

# --- Misc ---
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
LOG_LEVEL = "INFO"
