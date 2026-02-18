"""CLI entry-point for car-inventory-scraper.

Designed for use with ``uvx``::

    uvx car-inventory-scraper crawl dealeron \
        --url "https://www.toyotaofbellevue.com/searchnew.aspx?Make=Toyota"

Or scrape multiple dealers from a config file::

    uvx car-inventory-scraper crawl --config dealers.toml
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import click
from scrapy.crawler import AsyncCrawlerProcess
from scrapy.utils.project import get_project_settings


@click.group()
def main():
    """Scrape car dealership websites to build an inventory database."""


def _load_config(config_path: str) -> dict:
    """Load and validate a dealers TOML config file."""
    path = Path(config_path)
    if not path.exists():
        click.echo(f"Error: config file not found: {path}", err=True)
        sys.exit(1)

    with open(path, "rb") as f:
        config = tomllib.load(f)

    dealers = config.get("dealers")
    if not dealers:
        click.echo("Error: config file must contain at least one [[dealers]] entry.", err=True)
        sys.exit(1)

    for i, dealer in enumerate(dealers):
        missing = [k for k in ("spider", "url") if k not in dealer]
        if missing:
            label = dealer.get("name", f"dealers[{i}]")
            click.echo(
                f"Error: dealer '{label}' is missing required keys: {', '.join(missing)}",
                err=True,
            )
            sys.exit(1)

    return config


@main.command()
@click.argument("spider_name", required=False, default=None)
@click.option(
    "--url", "-u",
    default=None,
    help="Starting URL for the inventory page to scrape (single-dealer mode).",
)
@click.option(
    "--config", "-c",
    "config_path",
    default=None,
    type=click.Path(exists=True),
    help="Path to a TOML config file listing dealers to scrape.",
)
@click.option(
    "--output", "-o",
    default=None,
    help="Output JSON file path (default: inventory.json).",
)
def crawl(
    spider_name: str | None,
    url: str | None,
    config_path: str | None,
    output: str | None,
):
    """Run spiders to scrape dealership inventory.

    Single-dealer mode (provide SPIDER_NAME and --url)::

        car-inventory-scraper crawl dealeron --url https://…

    Multi-dealer mode (provide --config)::

        car-inventory-scraper crawl --config dealers.toml
    """
    if config_path and (spider_name or url):
        raise click.UsageError(
            "Use either --config or SPIDER_NAME + --url, not both."
        )

    if not config_path and not (spider_name and url):
        raise click.UsageError(
            "Provide either --config <file> or both SPIDER_NAME and --url."
        )

    settings = get_project_settings()

    if config_path:
        # ---------- multi-dealer mode ----------
        config = _load_config(config_path)
        cfg_settings = config.get("settings", {})

        report_path = output or cfg_settings.get("output")
        if report_path:
            settings.set("JSON_REPORT_PATH", report_path)

        dealers = config["dealers"]
        settings.set("TOTAL_SPIDER_COUNT", len(dealers))
        click.echo(f"Loaded {len(dealers)} dealer(s) from {config_path}")

        process = AsyncCrawlerProcess(settings)

        for i, dealer in enumerate(dealers, 1):
            label = dealer.get("name", dealer["url"])
            click.echo(f"  ▸ [{i}/{len(dealers)}] {label} (spider={dealer['spider']})")
            # Collect optional spider-specific keyword arguments.
            extra_kwargs = {
                k: dealer[k]
                for k in ("algolia_app_id", "algolia_api_key", "algolia_index")
                if k in dealer
            }
            process.crawl(
                dealer["spider"],
                url=dealer["url"],
                dealer_name=dealer.get("name"),
                **extra_kwargs,
            )

        process.start()
    else:
        # ---------- single-dealer mode ----------
        if output:
            settings.set("JSON_REPORT_PATH", output)

        process = AsyncCrawlerProcess(settings)
        process.crawl(spider_name, url=url)
        process.start()


@main.command("list")
def list_spiders():
    """List available spiders."""
    settings = get_project_settings()
    process = AsyncCrawlerProcess(settings)

    click.echo("Available spiders:")
    for name in sorted(process.spider_loader.list()):
        spider_cls = process.spider_loader.load(name)
        click.echo(f"  {name:20s} {spider_cls.__doc__ or ''}")


if __name__ == "__main__":
    main()
