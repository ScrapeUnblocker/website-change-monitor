"""Shared fakes: no test calls the ScrapeUnblocker API, GitHub or a real webhook."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from website_change_monitor.config import Config, parse_config

NOW = datetime(2026, 11, 10, 6, 17, tzinfo=UTC)
LATER = datetime(2026, 11, 10, 12, 17, tzinfo=UTC)
PRICING = "https://example.com/pricing"
CHANGELOG = "https://example.org/changelog"

# A trimmed pricing page as getPageSource returns it with markdown=true.
PRICING_MD = """# Pricing

Plans for teams of every size.

## Free

$0 per user / month

- 3 projects
- Community support

[Get started](https://example.com/signup?ref_cta=free&exp=1)

## Team

$4 per user / month

- Unlimited projects
- Email support

[Start a trial](https://example.com/signup?plan=team&ref_cta=card)

## Enterprise

$21 per user / month

- SAML single sign-on
- Audit log

Trusted by more than **50,000** companies

## Questions & answers

Can I cancel any time? Yes, from the billing page.
"""

CHANGELOG_MD = """# Changelog

## November 2026

- Faster search
- Dark mode for the editor

## October 2026

- New billing page
"""


def config(**overrides: Any) -> Config:
    """A parsed config with two pages; keyword arguments replace top-level sections."""
    raw: dict[str, Any] = {
        "pages": [
            {"name": "Example pricing", "url": PRICING},
            {"name": "Example changelog", "url": CHANGELOG},
        ],
        "delivery": {"destinations": ["github_issues", "slack", "discord", "webhook"]},
    }
    raw.update(overrides)
    return parse_config(raw)


class FakeClient:
    """Stands in for ``scrapeunblocker.Client``: one Markdown text (or error) per URL."""

    def __init__(self, pages: dict[str, Any]) -> None:
        self.pages = dict(pages)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_markdown(self, url: str, **kwargs: Any) -> str:
        self.calls.append((url, kwargs))
        value = self.pages[url]
        if isinstance(value, list):  # a sequence of answers, one per call
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, BaseException):
            raise value
        return value


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: Any = None
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        if self.payload is None:
            raise ValueError("no JSON")
        return self.payload


class FakeHttp:
    """Records every request. GitHub issue POSTs get an ``html_url`` back."""

    def __init__(self, status: int = 200, fail_hosts: tuple[str, ...] = ()) -> None:
        self.status = status
        self.fail_hosts = fail_hosts
        self.posts: list[tuple[str, Any, Any]] = []
        self.gets: list[tuple[str, Any]] = []
        self.issues = 0

    def post(self, url: str, *, json: Any, headers: Any = None, timeout: float) -> FakeResponse:
        self.posts.append((url, json, headers))
        if any(host in url for host in self.fail_hosts):
            return FakeResponse(500, text="boom")
        if url.endswith("/issues"):
            self.issues += 1
            number = self.issues
            return FakeResponse(
                201, {"number": number, "html_url": f"https://github.com/o/r/issues/{number}"}
            )
        return FakeResponse(self.status, text="ok")

    def get(self, url: str, *, headers: Any = None, timeout: float) -> FakeResponse:
        self.gets.append((url, headers))
        return FakeResponse(200, {"full_name": "o/r", "has_issues": True})

    def to(self, fragment: str) -> list[Any]:
        """JSON bodies posted to URLs containing ``fragment``."""
        return [body for url, body, _ in self.posts if fragment in url]


ENV = {
    "SCRAPEUNBLOCKER_KEY": "test-key",
    "GITHUB_TOKEN": "gh-test-token",
    "GITHUB_REPOSITORY": "o/r",
    "GITHUB_API_URL": "https://api.github.test",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.test/services/T/B/x",
    "DISCORD_WEBHOOK_URL": "https://discord.test/api/webhooks/1/x",
    "ALERT_WEBHOOK_URL": "https://alerts.test/hook",
}


@pytest.fixture
def env() -> dict[str, str]:
    return dict(ENV)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Retry pauses (API and webhook) return at once in tests."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
