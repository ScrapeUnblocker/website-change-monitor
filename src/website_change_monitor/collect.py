"""Fetch watched pages as Markdown through the ScrapeUnblocker getPageSource API.

One request per page: ``client.get_markdown(url, main_content_only=..., proxy_country=...)``
from the official Python SDK, which calls ``POST /getPageSource?markdown=true``. The page
is rendered in a real browser and comes back as clean Markdown (headings, lists, tables,
links), which diffs far better than raw HTML.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from scrapeunblocker import (
    AuthenticationError,
    BlockedError,
    BrowserTimeoutError,
    PaymentRequiredError,
    RateLimitError,
    ScrapeTimeoutError,
    ScrapeUnblockerError,
    ServerError,
    TargetNotFoundError,
    UpstreamOutageError,
)
from scrapeunblocker import ConnectionError as SUConnectionError

from .config import Page

log = logging.getLogger(__name__)

# Errors that end the whole run: retrying or moving on to the next page cannot help.
FATAL = (AuthenticationError, PaymentRequiredError)
# Errors worth another try after a pause.
RETRYABLE = (
    ServerError,
    UpstreamOutageError,
    RateLimitError,
    BlockedError,
    BrowserTimeoutError,
    ScrapeTimeoutError,
    SUConnectionError,
)
RETRY_DELAYS = (5.0, 20.0)

# Phrases of bot checks and error pages. Only short pages are checked, so a long page
# that happens to mention "access denied" is not mistaken for one.
_BLOCK_HINTS = (
    "verify you are human",
    "verifying you are human",
    "checking your browser",
    "checking if the site connection is secure",
    "enable javascript and cookies",
    "are you a robot",
    "unusual traffic",
    "attention required",
    "access denied",
    "request blocked",
)
_BLOCK_CHECK_MAX_CHARS = 1500


class MarkdownClient(Protocol):
    """The part of ``scrapeunblocker.Client`` this package uses."""

    def get_markdown(
        self,
        url: str,
        *,
        main_content_only: bool = True,
        proxy_country: str | None = None,
    ) -> str: ...


class FetchError(RuntimeError):
    """A page could not be read this run. The run goes on with the other pages."""


@dataclass
class CollectResult:
    pages: dict[str, str] = field(default_factory=dict)  # page id -> Markdown
    failures: dict[str, str] = field(default_factory=dict)  # page id -> reason


def looks_blocked(markdown: str) -> bool:
    """True when the text reads like a bot check or an error page, not the real page."""
    if len(markdown) > _BLOCK_CHECK_MAX_CHARS:
        return False
    text = markdown.casefold()
    return any(hint in text for hint in _BLOCK_HINTS)


def fetch_page(
    client: MarkdownClient,
    page: Page,
    *,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """Return the page as Markdown, retrying transient errors. Raises :class:`FetchError`."""
    sleep = sleep or time.sleep
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            log.info("getPageSource markdown %s (attempt %d)", page.url, attempt)
            markdown = client.get_markdown(
                page.url,
                main_content_only=page.rules.main_content_only,
                proxy_country=page.rules.country,
            )
        except FATAL:
            raise
        except TargetNotFoundError as exc:
            status = getattr(exc, "origin_status", None) or 404
            raise FetchError(f"the page answered HTTP {status} (moved or removed?)") from None
        except RETRYABLE as exc:
            if attempt == attempts:
                raise FetchError(f"{type(exc).__name__}: {exc}") from None
            delay = RETRY_DELAYS[attempt - 1]
            log.info("%s on %s, retrying in %.0fs", type(exc).__name__, page.url, delay)
            sleep(delay)
            continue
        except ScrapeUnblockerError as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from None
        if not markdown or not markdown.strip():
            raise FetchError("the page came back empty")
        if looks_blocked(markdown):
            raise FetchError("got a bot check or error page instead of the real page")
        return markdown
    raise AssertionError("unreachable")


def collect(
    client: MarkdownClient,
    pages: Sequence[Page],
    *,
    sleep: Callable[[float], None] | None = None,
) -> CollectResult:
    """Fetch every page once. A failed page is recorded and skipped."""
    result = CollectResult()
    for page in pages:
        try:
            result.pages[page.id] = fetch_page(client, page, sleep=sleep)
        except FetchError as exc:
            log.warning("%s: %s", page.url, exc)
            result.failures[page.id] = str(exc)
    return result
