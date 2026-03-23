.PHONY: crawl build dev all

# Run the scrapy spiders using the dealers config
crawl:
	cd $(CURDIR) && xvfb-run --auto-servernum --server-args="-screen 0 1920x1080x24" uv run car-inventory-scraper crawl --config dealers.toml

# Build the static site from inventory data
build:
	cd $(CURDIR)/site && npm run build

# Start the Eleventy dev server with hot-reload
dev:
	cd $(CURDIR)/site && npm run dev

# Full pipeline: crawl then build
all: crawl build
