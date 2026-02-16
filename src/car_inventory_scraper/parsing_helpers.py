"""Shared parsing and normalisation utilities for car-inventory-scraper spiders.

This module consolidates data-extraction, price-parsing, and normalisation
functions that are used across multiple platform-specific spiders, avoiding
duplication and ensuring consistent behaviour.
"""

from __future__ import annotations

import json
import re

from scrapy.http import HtmlResponse


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------

def parse_price(s) -> int | None:
    """Extract integer price from a string like ``$48,714`` or ``48714``.

    Returns ``None`` for falsy input or zero values.
    """
    if not s:
        return None
    digits = re.sub(r"[^\d]", "", str(s))
    return int(digits) if digits and int(digits) != 0 else None


def safe_int(val) -> int | None:
    """Convert a value to ``int``, returning ``None`` on failure or zero."""
    if val is None:
        return None
    try:
        result = int(val)
        return result if result != 0 else None
    except (ValueError, TypeError):
        return parse_price(val)


def format_pkg_price(price) -> str | None:
    """Format a package/option price as a dollar string (e.g. ``$1,299``)."""
    if price is None:
        return None
    parsed = parse_price(price)
    if parsed is not None:
        return f"${parsed:,}"
    return str(price)


# ---------------------------------------------------------------------------
# Package helpers
# ---------------------------------------------------------------------------

def normalize_pkg_name(name: str) -> str:
    """Strip whitespace and trailing period from a package/option name."""
    return name.strip().rstrip(".")


# Packages to exclude from scraped results — these are free and/or legally
# required, so they aren't meaningful pricing add-ons.
EXCLUDED_PACKAGES: frozenset[str] = frozenset({
    "50 state emissions",
})


# ---------------------------------------------------------------------------
# JSON-LD extraction
# ---------------------------------------------------------------------------

def extract_json_ld_car(response: HtmlResponse) -> dict:
    """Return the first JSON-LD block whose ``@type`` includes ``Car``, or ``{}``.

    Handles ``@type`` as either a plain string (``"Car"``) or a list
    (``["Product", "Car"]``).
    """
    for script in response.css('script[type="application/ld+json"]::text').getall():
        try:
            data = json.loads(script)
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            obj_type = obj.get("@type", "")
            if isinstance(obj_type, list):
                if "Car" in obj_type:
                    return obj
            elif obj_type == "Car":
                return obj
    return {}


def json_ld_price(json_ld: dict) -> int | None:
    """Extract the numeric price from a JSON-LD ``offers`` block.

    Handles ``offers`` as either a single dict or a list of dicts.
    """
    offers = json_ld.get("offers")
    if isinstance(offers, dict):
        return parse_price(offers.get("price"))
    if isinstance(offers, list) and offers:
        return parse_price(offers[0].get("price"))
    return None


# ---------------------------------------------------------------------------
# Drivetrain normalisation
# ---------------------------------------------------------------------------

_DRIVETRAIN_MAP: dict[str, str] = {
    "ALL WHEEL DRIVE": "AWD",
    "ALL-WHEEL DRIVE": "AWD",
    "AWD": "AWD",
    "FOUR WHEEL DRIVE": "4WD",
    "FOUR-WHEEL DRIVE": "4WD",
    "4WD": "4WD",
    "4X4": "4X4",
    "FRONT WHEEL DRIVE": "FWD",
    "FRONT-WHEEL DRIVE": "FWD",
    "FWD": "FWD",
    "REAR WHEEL DRIVE": "RWD",
    "REAR-WHEEL DRIVE": "RWD",
    "RWD": "RWD",
}

_DRIVETRAIN_TOKENS = ("AWD", "4WD", "FWD", "RWD", "4X4", "4X2")


def normalize_drivetrain(*sources: str) -> str | None:
    """Normalise a drivetrain string to a short token (AWD, FWD, …).

    Accepts one or more source strings (e.g. raw drivetrain field, trim,
    body style).  Tries an exact map lookup first, then falls back to
    scanning for known verbose names and short tokens.

    Returns ``None`` if no drivetrain can be determined.
    """
    for raw in sources:
        if not raw:
            continue
        upper = raw.strip().upper()
        # Exact match against the verbose-name map
        if upper in _DRIVETRAIN_MAP:
            return _DRIVETRAIN_MAP[upper]
        # Scan for verbose drivetrain names embedded in longer strings
        # (e.g. "New 2026 Toyota RAV4 LE 2.5L Engine Front-Wheel Drive")
        for verbose, short in _DRIVETRAIN_MAP.items():
            if verbose in upper:
                return short
        # Fallback: scan for known short tokens
        for token in _DRIVETRAIN_TOKENS:
            if token in upper:
                return token
    return None


# ---------------------------------------------------------------------------
# Color normalisation
# ---------------------------------------------------------------------------

def normalize_color(raw: str | None) -> str | None:
    """Clean up a color string.

    Removes bracketed substrings (e.g. ``[extra]``), registered-trademark
    symbols, and excess whitespace.
    """
    if not raw:
        return None
    cleaned = re.sub(r"\[.*?\]", "", raw)
    cleaned = cleaned.replace("&#xAE;", "")
    cleaned = cleaned.replace("\xae", "")
    cleaned = cleaned.replace("®", "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or None
