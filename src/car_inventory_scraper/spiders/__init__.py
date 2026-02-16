"""Shared utilities for all inventory spiders."""

from __future__ import annotations

import logging

from scrapy.spidermiddlewares.httperror import HttpError


def log_request_failure(
    failure,
    domain: str,
    logger: logging.Logger | None = None,
) -> None:
    """Log a Scrapy request failure with useful detail.

    For HTTP errors the status code, URL, and first 500 characters of the
    response body are included.  For all other failures (DNS, timeout, …)
    the URL and exception message are logged.

    Parameters
    ----------
    failure:
        The ``twisted.python.failure.Failure`` passed to an errback.
    domain:
        Site domain used as a log prefix (e.g. ``self._domain``).
    logger:
        Logger instance — typically ``self.logger`` from the spider.
        Falls back to the module-level logger when ``None``.
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    request = failure.request

    if failure.check(HttpError):
        response = failure.value.response
        logger.error(
            "[%s] HTTP %d on %s — body: %.500s",
            domain,
            response.status,
            request.url,
            response.text,
        )
    else:
        logger.error(
            "[%s] Request failed on %s: %s",
            domain,
            request.url,
            failure.value,
        )
