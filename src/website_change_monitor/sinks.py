"""Destinations behind one small interface.

A sink has a ``name`` and four methods: ``accepts(event)`` says which events it delivers,
``send(events)`` delivers them, ``preview(events)`` returns what ``send`` would post (for
``--dry-run``) and ``test()`` checks the connection (for ``test-destinations``). ``send``
and ``test`` raise :class:`DeliveryError` on failure. Secrets come from environment
variables, never from ``config.yml``. To add a destination, write a class like the ones
below and register it in ``REGISTRY``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from . import messages
from .config import Delivery
from .process import BASELINE, CHANGE, FAILING, Event

USER_AGENT = "website-change-monitor (+https://github.com/ScrapeUnblocker/website-change-monitor)"
CONNECTED = "website-change-monitor is connected. Changes on your watched pages will arrive here."


class DeliveryError(RuntimeError):
    """A destination did not accept the message."""


class Sink(Protocol):
    name: str

    def accepts(self, event: Event) -> bool: ...

    def send(self, events: Sequence[Event]) -> None: ...

    def preview(self, events: Sequence[Event]) -> list[str]: ...

    def test(self) -> str: ...


class Http(Protocol):
    def post(self, url: str, *, json: Any, headers: Any = None, timeout: float) -> Any: ...

    def get(self, url: str, *, headers: Any = None, timeout: float) -> Any: ...


def request(
    http: Http,
    method: str,
    url: str,
    *,
    sink: str,
    payload: Any = None,
    headers: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """Send one request, retrying once on 429/502/503. Errors never include the URL,
    because webhook URLs are secrets."""
    for attempt in (1, 2):
        try:
            if method == "GET":
                resp = http.get(url, headers=headers, timeout=20.0)
            else:
                resp = http.post(url, json=payload, headers=headers, timeout=20.0)
        except httpx.HTTPError as exc:
            raise DeliveryError(f"{sink}: request failed ({type(exc).__name__})") from None
        if resp.status_code in (429, 502, 503) and attempt == 1:
            (sleep or time.sleep)(_retry_after(resp))
            continue
        return resp
    raise AssertionError("unreachable")


def _retry_after(resp: Any) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "2")), 10.0)
    except (TypeError, ValueError, AttributeError):
        return 2.0


def _check(resp: Any, sink: str) -> None:
    if resp.status_code >= 300:
        body = (resp.text or "")[:200].replace("\n", " ")
        raise DeliveryError(f"{sink}: HTTP {resp.status_code} {body}".rstrip())


class TextSink:
    """A chat destination that shows rendered text: Slack, Discord."""

    name = "text"
    style = messages.PLAIN
    limit = 3900

    def __init__(self, webhook_url: str, http: Http, diff_lines: int = 30) -> None:
        self.webhook_url = webhook_url
        self.http = http
        self.diff_lines = diff_lines

    def accepts(self, event: Event) -> bool:
        return True

    def preview(self, events: Sequence[Event]) -> list[str]:
        return messages.render(events, self.style, diff_lines=self.diff_lines, limit=self.limit)

    def send(self, events: Sequence[Event]) -> None:
        for text in self.preview(events):
            self.post(text)

    def test(self) -> str:
        self.post(CONNECTED)
        return "test message sent"

    def post(self, text: str) -> None:
        resp = request(
            self.http, "POST", self.webhook_url, sink=self.name, payload=self.payload(text)
        )
        _check(resp, self.name)

    def payload(self, text: str) -> dict[str, Any]:
        raise NotImplementedError


class SlackSink(TextSink):
    """Slack incoming webhook."""

    name = "slack"
    style = messages.SLACK
    limit = 3900

    def payload(self, text: str) -> dict[str, Any]:
        return {"text": text, "unfurl_links": False, "unfurl_media": False}


class DiscordSink(TextSink):
    """Discord channel webhook. Link previews are suppressed, mentions disabled."""

    name = "discord"
    style = messages.DISCORD
    limit = 1900

    def payload(self, text: str) -> dict[str, Any]:
        return {"content": text, "allowed_mentions": {"parse": []}, "flags": 4}


class WebhookSink:
    """Any URL that accepts a JSON POST: Zapier, Make, n8n, your own endpoint.

    One POST per change or failure: the plain-text message as ``text`` plus every field as
    JSON. New pages arrive together in one ``baseline`` POST with a ``pages`` list.
    """

    name = "webhook"

    def __init__(self, url: str, http: Http, diff_lines: int = 30) -> None:
        self.webhook_url = url
        self.http = http
        self.diff_lines = diff_lines

    def accepts(self, event: Event) -> bool:
        return True

    def payloads(self, events: Sequence[Event]) -> list[dict[str, Any]]:
        out = []
        baselines = [e for e in events if e.kind == BASELINE]
        if baselines:
            out.append(
                {
                    "source": "website-change-monitor",
                    "kind": BASELINE,
                    "text": messages.render(baselines, messages.PLAIN)[0],
                    "pages": [e.to_dict() for e in baselines],
                }
            )
        for event in events:
            if event.kind == BASELINE:
                continue
            text = messages.render([event], messages.PLAIN, diff_lines=self.diff_lines)[0]
            out.append({"source": "website-change-monitor", "text": text, **event.to_dict()})
        return out

    def preview(self, events: Sequence[Event]) -> list[str]:
        return [p["text"] for p in self.payloads(events)]

    def send(self, events: Sequence[Event]) -> None:
        for payload in self.payloads(events):
            resp = request(self.http, "POST", self.webhook_url, sink=self.name, payload=payload)
            _check(resp, self.name)

    def test(self) -> str:
        payload = {"source": "website-change-monitor", "kind": "test", "text": CONNECTED}
        resp = request(self.http, "POST", self.webhook_url, sink=self.name, payload=payload)
        _check(resp, self.name)
        return "test message sent"


class GitHubIssuesSink:
    """Open one issue per change (and per page that keeps failing) in a GitHub repo.

    In GitHub Actions this needs no secret: the workflow passes its own ``GITHUB_TOKEN``
    and ``GITHUB_REPOSITORY`` is always set. Chat messages link to the issue.
    """

    name = "github_issues"

    def __init__(
        self,
        token: str,
        repository: str,
        http: Http,
        *,
        labels: Sequence[str] = ("page-change",),
        api_base: str = "https://api.github.com",
    ) -> None:
        self.token = token
        self.repository = repository.strip().strip("/")
        self.http = http
        self.labels = list(labels)
        self.api_base = api_base.rstrip("/")

    def accepts(self, event: Event) -> bool:
        return event.kind in (CHANGE, FAILING)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def issue(self, event: Event) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": messages.issue_title(event),
            "body": messages.issue_body(event),
        }
        if self.labels:
            payload["labels"] = self.labels
        return payload

    def preview(self, events: Sequence[Event]) -> list[str]:
        out = []
        for event in events:
            issue = self.issue(event)
            labels = f"\nLabels: {', '.join(self.labels)}" if self.labels else ""
            out.append(
                f"New issue in {self.repository}: {issue['title']}{labels}\n\n{issue['body']}"
            )
        return out

    def send(self, events: Sequence[Event]) -> None:
        for event in events:
            event.issue_url = self._create(self.issue(event))

    def _create(self, payload: dict[str, Any]) -> str:
        url = f"{self.api_base}/repos/{self.repository}/issues"
        resp = request(
            self.http, "POST", url, sink=self.name, payload=payload, headers=self.headers
        )
        if resp.status_code == 422 and "labels" in payload:
            # Only collaborators with push access may label issues; retry without labels.
            payload = {k: v for k, v in payload.items() if k != "labels"}
            resp = request(
                self.http, "POST", url, sink=self.name, payload=payload, headers=self.headers
            )
        self._raise_for(resp)
        try:
            return str(resp.json()["html_url"])
        except (ValueError, KeyError, TypeError):
            raise DeliveryError(f"{self.name}: unexpected response from GitHub") from None

    def test(self) -> str:
        url = f"{self.api_base}/repos/{self.repository}"
        resp = request(self.http, "GET", url, sink=self.name, headers=self.headers)
        self._raise_for(resp)
        try:
            enabled = bool(resp.json().get("has_issues", True))
        except (ValueError, AttributeError):
            enabled = True
        if not enabled:
            raise DeliveryError(f"{self.name}: {self.repository} has issues turned off")
        return f"{self.repository} is reachable and has issues on (no issue created)"

    def _raise_for(self, resp: Any) -> None:
        status = resp.status_code
        if status < 300:
            return
        text = resp.text or ""
        hints = {
            401: "GITHUB_TOKEN was rejected",
            404: f"repository {self.repository} not found, or the token cannot see it",
            410: f"issues are turned off for {self.repository} (Settings > General > Features)",
        }
        if status == 403 and "not accessible by integration" in text:
            hint = "the workflow token cannot open issues; keep 'issues: write' in run.yml"
        elif status == 403:
            hint = "the token lacks permission to open issues (needs Issues: read and write)"
        else:
            hint = hints.get(status, text[:200].replace("\n", " "))
        raise DeliveryError(f"{self.name}: HTTP {status} {hint}".rstrip())


@dataclass(frozen=True)
class SinkSpec:
    env: tuple[str, ...]
    build: Callable[[Mapping[str, str], Http, Delivery], Sink]


REGISTRY: dict[str, SinkSpec] = {
    "github_issues": SinkSpec(
        env=("GITHUB_TOKEN", "GITHUB_REPOSITORY"),
        build=lambda env, http, d: GitHubIssuesSink(
            env["GITHUB_TOKEN"],
            env["GITHUB_REPOSITORY"],
            http,
            labels=d.issue_labels,
            api_base=env.get("GITHUB_API_URL") or "https://api.github.com",
        ),
    ),
    "slack": SinkSpec(
        env=("SLACK_WEBHOOK_URL",),
        build=lambda env, http, d: SlackSink(env["SLACK_WEBHOOK_URL"], http, d.diff_lines),
    ),
    "discord": SinkSpec(
        env=("DISCORD_WEBHOOK_URL",),
        build=lambda env, http, d: DiscordSink(env["DISCORD_WEBHOOK_URL"], http, d.diff_lines),
    ),
    "webhook": SinkSpec(
        env=("ALERT_WEBHOOK_URL",),
        build=lambda env, http, d: WebhookSink(env["ALERT_WEBHOOK_URL"], http, d.diff_lines),
    ),
}

ENV_HELP = {
    "github_issues": "GITHUB_TOKEN + GITHUB_REPOSITORY (set automatically in GitHub Actions)",
    "slack": "SLACK_WEBHOOK_URL",
    "discord": "DISCORD_WEBHOOK_URL",
    "webhook": "ALERT_WEBHOOK_URL",
}


def build_sinks(
    delivery: Delivery, env: Mapping[str, str], http: Http
) -> tuple[list[Sink], list[str]]:
    """Build every listed destination whose secrets are set, in delivery order.

    Returns ``(sinks, skipped)`` where ``skipped`` names destinations with missing secrets.
    """
    clean = {k: v.strip() for k, v in env.items() if isinstance(v, str)}
    sinks: list[Sink] = []
    skipped: list[str] = []
    for name in delivery.destinations:
        spec = REGISTRY[name]
        if all(clean.get(var) for var in spec.env):
            sinks.append(spec.build(clean, http, delivery))
        else:
            skipped.append(name)
    return sinks, skipped


def new_http_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=False)
