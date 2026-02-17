.PHONY: crawl build dev all

# Run the scrapy spiders using the dealers config
crawl:
	cd $(CURDIR) && uv run car-inventory-scraper crawl --config dealers.toml

# Build the static site from inventory data
build:
	cd $(CURDIR)/site && npm run build

# Start the Eleventy dev server with hot-reload
dev:
	cd $(CURDIR)/site && npm run dev

# Full pipeline: crawl then build
all: crawl build
