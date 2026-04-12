#!/usr/bin/env python3
"""Fetch a URL using nodriver in headed mode and print the page source.

Usage:
    uv run python scripts/fetch_page.py <URL> [--output FILE] [--wait-js EXPR]

This bypasses Cloudflare bot detection by running a real Chrome browser
in headed (non-headless) mode via the nodriver library.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import socket
import sys

import nodriver
from nodriver.core.config import temp_profile_dir


_BROWSER_CANDIDATES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
]

_CHALLENGE_TITLES = {"Just a moment...", ""}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_port(host: str, port: int, timeout: float = 30) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2,
            )
            writer.close()
            await writer.wait_closed()
            return
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.5)
    raise RuntimeError(f"Chrome debug port {host}:{port} not ready after {timeout}s")


async def _wait_for_real_page(tab, wait_js: str | None = None) -> None:
    while True:
        try:
            title = str(await tab.evaluate("document.title") or "")
            ready = str(await tab.evaluate("document.readyState") or "")
            if title in _CHALLENGE_TITLES or ready != "complete":
                await asyncio.sleep(0.5)
                continue
            if wait_js:
                result = await tab.evaluate(wait_js)
                if not result:
                    await asyncio.sleep(0.5)
                    continue
            return
        except Exception:
            await asyncio.sleep(0.5)


async def fetch(url: str, wait_js: str | None = None, timeout: float = 60) -> str:
    browser_path = None
    for name in _BROWSER_CANDIDATES:
        path = shutil.which(name)
        if path:
            browser_path = path
            break
    if not browser_path:
        raise FileNotFoundError("No Chrome/Chromium binary found on PATH")

    host = "127.0.0.1"
    port = _free_port()
    user_data_dir = temp_profile_dir()

    args = [
        f"--remote-debugging-host={host}",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-service-autorun",
        "--no-default-browser-check",
        "--homepage=about:blank",
        "--no-pings",
        "--password-store=basic",
        "--disable-infobars",
        "--disable-breakpad",
        "--disable-dev-shm-usage",
        "--disable-session-crashed-bubble",
        "--disable-search-engine-choice-screen",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-sandbox",
    ]

    chrome_proc = await asyncio.create_subprocess_exec(
        browser_path,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    print(f"Launched Chrome (pid {chrome_proc.pid}) on {host}:{port}", file=sys.stderr)

    try:
        await _wait_for_port(host, port)

        browser = await nodriver.Browser.create(
            headless=False,
            sandbox=False,
            host=host,
            port=port,
            browser_executable_path=browser_path,
        )

        tab = await browser.get(url, new_tab=True)

        try:
            await asyncio.wait_for(
                _wait_for_real_page(tab, wait_js),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            print(f"Warning: page did not finish loading within {timeout}s", file=sys.stderr)

        content = await tab.get_content()
        await tab.close()
        return content
    finally:
        chrome_proc.terminate()
        try:
            await asyncio.wait_for(chrome_proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            chrome_proc.kill()


def main():
    parser = argparse.ArgumentParser(description="Fetch a URL using nodriver (headed Chrome)")
    parser.add_argument("url", help="URL to fetch")
    parser.add_argument("--output", "-o", help="Write HTML to this file (default: stdout)")
    parser.add_argument("--wait-js", help="JS expression to wait for before capturing")
    parser.add_argument("--timeout", type=float, default=60, help="Page load timeout in seconds")
    args = parser.parse_args()

    content = asyncio.run(fetch(args.url, wait_js=args.wait_js, timeout=args.timeout))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Wrote {len(content)} bytes to {args.output}", file=sys.stderr)
    else:
        print(content)


if __name__ == "__main__":
    main()
