"""Turn page Markdown into comparable text, diff it against the last version, decide.

Nothing here does I/O. ``normalize`` makes the text stable (links, tracking parameters and
blank lines removed, sections scoped, ignore patterns applied), ``compare`` builds the diff
and the price summary, ``evaluate`` and ``record_failure`` decide what to deliver and what
the page's state becomes.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from .config import Page

# [text](url "title"), with escaped brackets in the text and one level of parentheses in
# the URL (Wikipedia-style links). Images are the same with a leading "!".
_LINK_TEXT = r"\[((?:\\.|[^\[\]\\])*)\]"
_LINK_TARGET = r"\((?:[^()\s]|\([^()\s]*\))*(?:\s+\"[^\"]*\")?\)"
_IMAGE_RE = re.compile("!" + _LINK_TEXT + _LINK_TARGET)
_LINK_RE = re.compile(_LINK_TEXT + _LINK_TARGET)
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
# Zero-width characters, soft hyphens and heading permalink marks.
_INVISIBLE = dict.fromkeys(
    map(
        ord,
        "\N{ZERO WIDTH SPACE}\N{ZERO WIDTH NON-JOINER}\N{ZERO WIDTH JOINER}"
        "\N{WORD JOINER}\N{ZERO WIDTH NO-BREAK SPACE}\N{SOFT HYPHEN}\N{PILCROW SIGN}",
    ),
    None,
)
_SPACES_RE = re.compile(r"\s+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")

_CODES = "USD|EUR|GBP|CAD|AUD|NZD|CHF|JPY|CNY|INR|BRL|MXN|SEK|NOK|DKK|PLN|SGD|HKD|ZAR"
_NUMBER = r"\d[\d,]*(?:\.\d+)?"
PRICE_RE = re.compile(
    rf"(?:(?:US|CA|AU|NZ|HK|SG|R|C|A|S)?\$|[€£¥₹₩₽]|\b(?:{_CODES})\s?)\s?{_NUMBER}"
    rf"|\b{_NUMBER}\s?(?:{_CODES})\b"
)

# A change that leaves less than this share of the previous text is held for one more
# run before it is believed: error pages and half-loaded pages are short.
SHRINK_RATIO = 0.5
SHRINK_MIN_CHARS = 500
# Character-level diffing of very large replaced blocks is slow; count them whole.
_CHAR_DIFF_MAX = 20000


class SectionNotFound(ValueError):
    """The page has no heading that matches the configured ``section``."""


# --------------------------------------------------------------------------- normalize


def normalize(markdown: str, page: Page) -> list[str]:
    """The lines that are stored and compared for ``page``."""
    rules = page.rules
    text = markdown
    if not rules.keep_links:
        text = _IMAGE_RE.sub(r"\1", text)
        text = _LINK_RE.sub(r"\1", text)
    text = _ESCAPE_RE.sub(r"\1", text.translate(_INVISIBLE))
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if page.section:
        lines = keep_section(lines, page.section)
    for title in page.skip_sections:
        lines = drop_section(lines, title)
    patterns = rules.ignore_patterns()
    if patterns:
        lines = [line for line in lines if not any(p.search(line) for p in patterns)]
    return lines


def heading(line: str) -> tuple[int, str] | None:
    """``(level, text)`` for a Markdown heading line, else None."""
    match = _HEADING_RE.match(line)
    if not match:
        return None
    return len(match.group(1)), match.group(2).replace("*", "").replace("_", " ").strip()


def _section_bounds(lines: Sequence[str], title: str) -> tuple[int, int] | None:
    wanted = title.casefold()
    for start, line in enumerate(lines):
        found = heading(line)
        if found and wanted in found[1].casefold():
            level = found[0]
            end = start + 1
            while end < len(lines):
                nxt = heading(lines[end])
                if nxt and nxt[0] <= level:
                    break
                end += 1
            return start, end
    return None


def keep_section(lines: Sequence[str], title: str) -> list[str]:
    """The heading that contains ``title`` and everything under it, up to the next heading
    of the same or a higher level."""
    bounds = _section_bounds(lines, title)
    if bounds is None:
        raise SectionNotFound(f"no heading on the page contains {title!r}")
    return list(lines[bounds[0] : bounds[1]])


def drop_section(lines: Sequence[str], title: str) -> list[str]:
    """``lines`` without the section ``keep_section`` would return. Missing is fine."""
    bounds = _section_bounds(lines, title)
    if bounds is None:
        return list(lines)
    return list(lines[: bounds[0]]) + list(lines[bounds[1] :])


def fingerprint(lines: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def text_size(lines: Sequence[str]) -> int:
    return sum(len(line) for line in lines)


# ----------------------------------------------------------------------------- prices


def prices_in(text: str) -> list[str]:
    """Money amounts in ``text``: ``$4``, ``€9.50``, ``1,200 USD``."""
    return [m.group(0).rstrip(",.") for m in PRICE_RE.finditer(text)]


def unique_prices(lines: Sequence[str]) -> list[str]:
    """Every distinct money amount in ``lines``, in order of first appearance."""
    seen: dict[str, None] = {}
    for line in lines:
        for price in prices_in(line):
            seen.setdefault(price, None)
    return list(seen)


def _minus(items: Sequence[str], remove: Sequence[str]) -> list[str]:
    """Multiset difference that keeps the order of ``items``."""
    left = Counter(remove)
    out = []
    for item in items:
        if left[item]:
            left[item] -= 1
        else:
            out.append(item)
    return out


@dataclass(frozen=True)
class PriceChange:
    """``before -> after``; one side is None for a price that was removed or added."""

    before: str | None
    after: str | None

    def __str__(self) -> str:
        if self.before and self.after:
            return f"{self.before} → {self.after}"
        if self.before:
            return f"removed {self.before}"
        return f"new {self.after}"


# ------------------------------------------------------------------------------- diff


@dataclass(frozen=True)
class Diff:
    added: int
    removed: int
    changed_chars: int
    lines: tuple[str, ...]  # "@@ <heading> @@", " context", "-old", "+new"
    prices: tuple[PriceChange, ...] = ()

    def to_text(self, max_lines: int | None = None) -> str:
        lines = list(self.lines)
        if max_lines is not None and len(lines) > max_lines:
            hidden = len(lines) - max_lines
            lines = lines[:max_lines] + [f"... {hidden} more diff line{'s' if hidden != 1 else ''}"]
        return "\n".join(lines)


def _nearest_heading(lines: Sequence[str], index: int) -> str | None:
    for i in range(min(index, len(lines)) - 1, -1, -1):
        if heading(lines[i]):
            return lines[i]
    return None


def _changed_chars(old: str, new: str) -> int:
    if not old or not new or len(old) + len(new) > _CHAR_DIFF_MAX:
        return max(len(old), len(new))
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    return sum(
        max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
    )


def compare(old: Sequence[str], new: Sequence[str], context: int = 1) -> Diff:
    """Line diff of two normalized versions, with the nearest heading above each hunk."""
    matcher = SequenceMatcher(None, list(old), list(new), autojunk=False)
    out: list[str] = []
    added = removed = changed = 0
    pairs: list[PriceChange] = []
    gone: list[str] = []
    fresh: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
        changed += _changed_chars("\n".join(old[i1:i2]), "\n".join(new[j1:j2]))
        before = [p for line in old[i1:i2] for p in prices_in(line)]
        after = [p for line in new[j1:j2] for p in prices_in(line)]
        before_only, after_only = _minus(before, after), _minus(after, before)
        pairs.extend(PriceChange(b, a) for b, a in zip(before_only, after_only, strict=False))
        gone.extend(before_only[len(after_only) :])
        fresh.extend(after_only[len(before_only) :])

    for group in matcher.get_grouped_opcodes(context):
        _, i1, _, j1, _ = group[0]
        title = _nearest_heading(new, j1) or _nearest_heading(old, i1)
        out.append(f"@@ {title} @@" if title else "@@")
        for tag, a1, a2, b1, b2 in group:
            if tag == "equal":
                out.extend(" " + line for line in old[a1:a2])
                continue
            out.extend("-" + line for line in old[a1:a2])
            out.extend("+" + line for line in new[b1:b2])

    # A price that left one place and showed up in another only moved.
    moved = list((Counter(gone) & Counter(fresh)).elements())
    gone, fresh = _minus(gone, moved), _minus(fresh, moved)
    changes = list(pairs) + [PriceChange(p, None) for p in gone]
    changes += [PriceChange(None, p) for p in fresh]
    return Diff(
        added=added,
        removed=removed,
        changed_chars=changed,
        lines=tuple(out),
        prices=tuple(dict.fromkeys(changes)),
    )


# ---------------------------------------------------------------------------- events

BASELINE = "baseline"
CHANGE = "change"
FAILING = "failing"


@dataclass
class Event:
    """Something to deliver about one page."""

    kind: str
    page: Page
    checked_at: datetime
    diff: Diff | None = None
    previous_at: str | None = None  # when the version compared against was first seen
    lines: int = 0
    prices: tuple[str, ...] = ()  # baseline: money amounts seen on the page
    error: str | None = None
    failed_runs: int = 0
    issue_url: str | None = None  # set by the GitHub Issues destination

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "page": {"id": self.page.id, "name": self.page.name, "url": self.page.url},
            "checked_at": _iso(self.checked_at),
        }
        if self.diff is not None:
            data.update(
                added_lines=self.diff.added,
                removed_lines=self.diff.removed,
                changed_chars=self.diff.changed_chars,
                prices=[{"before": p.before, "after": p.after} for p in self.diff.prices],
                diff=self.diff.to_text(),
                previous_version_at=self.previous_at,
            )
        if self.kind == BASELINE:
            data.update(lines=self.lines, prices_seen=list(self.prices))
        if self.kind == FAILING:
            data.update(error=self.error, failed_runs=self.failed_runs)
        if self.issue_url:
            data["issue_url"] = self.issue_url
        return data


@dataclass
class Verdict:
    """What one check of one page produced."""

    entry: dict[str, Any]  # the page's new state entry
    snapshot: list[str] | None = None  # new text to store, or None to keep the old one
    event: Event | None = None
    note: str = ""


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def evaluate(
    page: Page,
    lines: list[str],
    entry: dict[str, Any] | None,
    previous: list[str] | None,
    now: datetime,
    *,
    baseline_message: bool = True,
) -> Verdict:
    """Compare a fresh read of ``page`` with its stored version and decide what to send."""
    rules = page.rules
    sha = fingerprint(lines)
    size = text_size(lines)
    base = dict(entry or {})
    base.pop("failed_runs", None)
    base.update(url=page.url, name=page.name)

    if previous is None or not base.get("sha256"):
        base.update(
            sha256=sha,
            chars=size,
            lines=len(lines),
            first_seen=_iso(now),
            last_changed=_iso(now),
            changes=0,
        )
        base.pop("pending", None)
        event = None
        if baseline_message:
            event = Event(BASELINE, page, now, lines=len(lines), prices=tuple(unique_prices(lines)))
        return Verdict(base, snapshot=lines, event=event, note="first check (baseline)")

    if sha == base["sha256"]:
        base.pop("pending", None)
        return Verdict(base, note="unchanged")

    diff = compare(previous, lines)
    if diff.changed_chars < rules.min_changed_chars:
        base.pop("pending", None)
        return Verdict(
            base,
            note=f"{diff.changed_chars} changed char(s), below min_changed_chars "
            f"({rules.min_changed_chars})",
        )

    needed = rules.confirm_runs
    old_size = int(base.get("chars") or 0)
    shrunk = old_size >= SHRINK_MIN_CHARS and size < old_size * SHRINK_RATIO
    if shrunk:
        needed = max(needed, 2)
    if needed > 1:
        pending = base.get("pending") or {}
        seen = int(pending.get("count", 0)) + 1 if pending.get("sha256") == sha else 1
        if seen < needed:
            base["pending"] = {"sha256": sha, "count": seen}
            why = "text shrank by more than half, " if shrunk else ""
            return Verdict(base, note=f"changed, {why}waiting for confirmation ({seen}/{needed})")
    base.pop("pending", None)

    event = Event(CHANGE, page, now, diff=diff, previous_at=base.get("last_changed"))
    base.update(
        sha256=sha,
        chars=size,
        lines=len(lines),
        last_changed=_iso(now),
        changes=int(base.get("changes") or 0) + 1,
    )
    note = f"CHANGED +{diff.added} -{diff.removed} lines"
    if diff.prices:
        note += ", prices " + ", ".join(map(str, diff.prices[:4]))
    return Verdict(base, snapshot=lines, event=event, note=note)


def record_failure(page: Page, error: str, entry: dict[str, Any] | None, now: datetime) -> Verdict:
    """Count a failed read. One alert after ``alert_after_failures`` failures in a row."""
    base = dict(entry or {})
    base.update(url=page.url, name=page.name)
    limit = page.rules.alert_after_failures
    count = int(base.get("failed_runs") or 0)
    if not limit:
        return Verdict(base, note=f"FAILED {error}")
    if count >= limit:
        # Already alerted. The counter stops here so the state file stops changing.
        return Verdict(base, note=f"FAILED {error} (still failing)")
    count += 1
    base["failed_runs"] = count
    event = None
    if count == limit:
        event = Event(FAILING, page, now, error=error, failed_runs=count)
    return Verdict(base, event=event, note=f"FAILED {error}")
