"""Scrapy items for car inventory data."""

import scrapy


class CarItem(scrapy.Item):
    """A single vehicle listing from a dealership."""

    # Identifiers
    vin = scrapy.Field()
    stock_number = scrapy.Field()
    model_code = scrapy.Field()

    # Vehicle info
    year = scrapy.Field()
    trim = scrapy.Field()
    drivetrain = scrapy.Field()

    # Appearance
    exterior_color = scrapy.Field()
    interior_color = scrapy.Field()

    # Pricing
    msrp = scrapy.Field()                  # TSRP / total sticker
    base_price = scrapy.Field()             # Base vehicle price (MSRP minus packages)
    total_packages_price = scrapy.Field()   # Sum of all package prices
    dealer_accessories = scrapy.Field()     # Dealer-installed accessories price
    adjustments = scrapy.Field()            # Dealer discounts / markups
    total_price = scrapy.Field()            # Final selling / advertised price

    # Status & availability
    status = scrapy.Field()  # "In Stock", "In Transit", etc.
    availability_date = scrapy.Field()

    # Packages
    packages = scrapy.Field()  # list of dicts: {name, price}

    # Dealership info
    dealer_name = scrapy.Field()
    dealer_url = scrapy.Field()
    detail_url = scrapy.Field()

    # Metadata
    scraped_at = scrapy.Field()
