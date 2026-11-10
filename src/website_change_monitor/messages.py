"""Render events as chat messages (Slack, Discord, plain text) and as GitHub issues."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from .process import BASELINE, CHANGE, FAILING, Event

PLAIN = "plain"
SLACK = "slack"
DISCORD = "discord"

REPO_URL = "https://github.com/ScrapeUnblocker/website-change-monitor"
SU_URL = (
    "https://scrapeunblocker.com/?utm_source=github&utm_medium=integration"
    "&utm_campaign=example-repos"
)
ISSUE_BODY_LIMIT = 60000  # GitHub allows 65,536 characters
_MAX_LINE = 240
_MAX_PRICES_SHOWN = 6


def fmt_time(value: str | datetime | None) -> str:
    """``2026-11-10 06:17 UTC`` from a datetime or the state file's ISO string."""
    if value is None:
        return "unknown"
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return value
    return value.strftime("%Y-%m-%d %H:%M UTC")


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def price_summary(event: Event, limit: int = _MAX_PRICES_SHOWN) -> str:
    """``$4 → $5, removed $99, new $129`` or an empty string."""
    if event.diff is None or not event.diff.prices:
        return ""
    shown = [str(p) for p in event.diff.prices[:limit]]
    hidden = len(event.diff.prices) - len(shown)
    return ", ".join(shown) + (f" (+{hidden} more)" if hidden > 0 else "")


def summary(event: Event) -> str:
    """``+3 / -2 lines · prices $4 → $5``."""
    assert event.diff is not None
    text = f"+{event.diff.added} / -{event.diff.removed} lines"
    prices = price_summary(event)
    if prices:
        text += f" · prices {prices}"
    return text


# ------------------------------------------------------------------------------ chat


def render(
    events: Sequence[Event],
    style: str = PLAIN,
    *,
    diff_lines: int = 30,
    limit: int | None = None,
) -> list[str]:
    """Chat messages for ``events``: one for all new baselines, one per change or failure."""
    messages: list[str] = []
    baselines = [e for e in events if e.kind == BASELINE]
    if baselines:
        messages.extend(_baseline(baselines, style, limit))
    for event in events:
        if event.kind == CHANGE:
            messages.append(_change(event, style, diff_lines, limit))
        elif event.kind == FAILING:
            messages.append(_failing(event, style))
    return messages


def _esc(text: str, style: str) -> str:
    if style == SLACK:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if style == DISCORD:
        return re.sub(r"([\\*_~|`>\[\]])", r"\\\1", text)
    return text


def _link(url: str, label: str, style: str) -> str:
    if style == SLACK:
        return f"<{url}|{_esc(label, style)}>"
    if style == DISCORD:
        return f"[{_esc(label, style)}](<{url}>)"
    return url


def _bold(text: str, style: str) -> str:
    if style == SLACK:
        return f"*{_esc(text, style)}*"
    if style == DISCORD:
        return f"**{_esc(text, style)}**"
    return text


def _code_block(text: str, style: str) -> str:
    if style == SLACK:
        return "```\n" + _esc(text, style) + "\n```"
    if style == DISCORD:
        return "```diff\n" + text.replace("```", "'''") + "\n```"
    return text


def _trim_line(line: str) -> str:
    return line if len(line) <= _MAX_LINE else line[: _MAX_LINE - 1] + "…"


def _change(event: Event, style: str, diff_lines: int, limit: int | None) -> str:
    assert event.diff is not None
    page = event.page
    if style == PLAIN:
        head = [f"{page.name} changed", page.url]
    else:
        head = [f"{_bold(page.name + ' changed', style)} {_link(page.url, page.short_url, style)}"]
    head.append(_esc(summary(event), style))
    tail = []
    if event.issue_url:
        tail.append(f"Full diff: {_link(event.issue_url, 'GitHub issue', style)}")
    tail.append(f"Compared with the version from {fmt_time(event.previous_at)}.")

    shown = diff_lines
    while True:
        diff = event.diff.to_text(shown)
        body = _code_block("\n".join(_trim_line(line) for line in diff.splitlines()), style)
        text = "\n".join(head) + "\n\n" + body + "\n\n" + "\n".join(tail)
        if limit is None or len(text) <= limit or shown <= 3:
            return text[:limit] if limit else text
        shown = max(3, shown - max(1, shown // 4))


def _baseline(events: Sequence[Event], style: str, limit: int | None) -> list[str]:
    bullet = "•" if style != PLAIN else "-"
    header = f"Now watching {plural(len(events), 'page')}. Changes will be posted here with a diff."
    rows = []
    for event in events:
        page = event.page
        if style == PLAIN:
            row = f"{bullet} {page.name} ({page.url}): {plural(event.lines, 'line')}"
        else:
            row = (
                f"{bullet} {_bold(page.name, style)} {_link(page.url, page.short_url, style)}: "
                f"{plural(event.lines, 'line')}"
            )
        if event.prices:
            shown = list(event.prices[:_MAX_PRICES_SHOWN])
            more = len(event.prices) - len(shown)
            row += f", prices on the page: {_esc(', '.join(shown), style)}"
            if more > 0:
                row += f" (+{more} more)"
        rows.append(row)

    messages = []
    current = header
    for row in rows:
        if limit and len(current) + 1 + len(row) > limit:
            messages.append(current)
            current = row
        else:
            current += "\n" + row
    messages.append(current)
    return messages


def _failing(event: Event, style: str) -> str:
    page = event.page
    name = _bold(f"Can't read {page.name}", style)
    where = _link(page.url, page.short_url, style)
    return (
        f"{name} {where}\n"
        f"{_esc(event.error or 'unknown error', style)}\n"
        f"It failed {event.failed_runs} checks in a row, so changes on this page cannot be "
        f"seen until it reads again. Run `website-change-monitor preview {page.id}` to see "
        "what comes back."
    )


# ---------------------------------------------------------------------------- issues


def issue_title(event: Event) -> str:
    page = event.page
    if event.kind == FAILING:
        return f"Can't read {page.name} ({event.failed_runs} checks in a row)"
    assert event.diff is not None
    prices = price_summary(event, limit=3)
    if prices:
        title = f"{page.name} changed: {prices}"
    else:
        title = f"{page.name} changed (+{event.diff.added} / -{event.diff.removed} lines)"
    return title if len(title) <= 200 else title[:199] + "…"


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _compared_text(event: Event) -> str:
    page = event.page
    rules = page.rules
    parts = ["main content" if rules.main_content_only else "the whole page"]
    if page.section:
        parts.append(f'section "{page.section}" only')
    if page.skip_sections:
        parts.append("without " + ", ".join(f'"{s}"' for s in page.skip_sections))
    if not rules.keep_links:
        parts.append("links removed")
    if rules.ignore:
        parts.append(f"{plural(len(rules.ignore), 'ignore pattern')} applied")
    country = rules.country.upper() if rules.country else "an automatically picked country"
    return f"{', '.join(parts)}, as seen from {country}"


def issue_body(event: Event) -> str:
    page = event.page
    footer = (
        f"<sub>Compared text: {_compared_text(event)}. Stored version: "
        f"`state/pages/{page.id}.md`. Sent by [website-change-monitor]({REPO_URL}) "
        f"via [ScrapeUnblocker]({SU_URL}).</sub>"
    )
    if event.kind == FAILING:
        error = (event.error or "unknown error").replace("`", "'")
        return (
            f"**[{page.name}]({page.url})** could not be read on "
            f"{event.failed_runs} checks in a row, the last at {fmt_time(event.checked_at)}.\n\n"
            f"Last error: `{error}`\n\n"
            "Until it reads again, changes on this page go unnoticed. Things to try:\n\n"
            f"- `website-change-monitor preview {page.id}` shows what the page returns now.\n"
            "- If the page moved, update its `url` in `config.yml`.\n"
            "- If a `section` heading was renamed, update `section` to match.\n\n"
            "This is the only alert for this failure. It resets once the page reads again.\n\n"
            + footer
        )

    assert event.diff is not None
    lines = [
        f"**[{page.name}]({page.url})** changed. Checked {fmt_time(event.checked_at)}, "
        f"compared with the version from {fmt_time(event.previous_at)}.",
        "",
        f"- **Lines:** +{event.diff.added} added, -{event.diff.removed} removed",
    ]
    prices = price_summary(event, limit=20)
    if prices:
        lines.append(f"- **Prices:** {prices}")
    head = "\n".join(lines) + "\n\n"
    tail = "\n\n" + footer

    diff_text = event.diff.to_text()
    room = ISSUE_BODY_LIMIT - len(head) - len(tail) - 20
    if len(diff_text) > room:
        kept = diff_text[:room].rsplit("\n", 1)[0]
        hidden = diff_text.count("\n") - kept.count("\n")
        diff_text = kept + f"\n... {plural(hidden, 'more diff line')}"
    fence = _fence(diff_text)
    return head + f"{fence}diff\n{diff_text}\n{fence}" + tail
