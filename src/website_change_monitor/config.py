"""Load and validate ``config.yml``, the one file a user edits."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

# Delivery order matters: GitHub Issues goes first so chat messages can link to the issue.
DESTINATIONS = ("github_issues", "slack", "discord", "webhook")

_COUNTRY_RE = re.compile(r"^[a-z]{2}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_RULE_KEYS = {
    "country",
    "main_content_only",
    "keep_links",
    "ignore",
    "min_changed_chars",
    "confirm_runs",
    "alert_after_failures",
}
_PAGE_KEYS = {"name", "url", "id", "section", "skip_sections"} | _RULE_KEYS
_DELIVERY_KEYS = {"destinations", "diff_lines", "baseline_message", "issue_labels"}
_TOP_KEYS = {"pages", "defaults", "delivery", "state_dir"}


class ConfigError(ValueError):
    """The config file is missing, unreadable or has an invalid value."""


@dataclass(frozen=True)
class Rules:
    """How a page is fetched and compared. ``defaults`` sets them, a page can override."""

    country: str | None = "us"
    main_content_only: bool = True
    keep_links: bool = False
    ignore: tuple[str, ...] = ()
    min_changed_chars: int = 1
    confirm_runs: int = 1
    alert_after_failures: int = 3

    def ignore_patterns(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(p) for p in self.ignore)


@dataclass(frozen=True)
class Page:
    """One watched page."""

    id: str
    name: str
    url: str
    rules: Rules
    section: str | None = None
    skip_sections: tuple[str, ...] = ()

    @property
    def short_url(self) -> str:
        """``github.com/pricing``: the URL without scheme, ``www.`` or a trailing slash."""
        parsed = urlparse(self.url)
        host = (parsed.hostname or "").removeprefix("www.")
        rest = parsed.path.rstrip("/")
        if parsed.query:
            rest += f"?{parsed.query}"
        return host + rest


@dataclass(frozen=True)
class Delivery:
    destinations: tuple[str, ...] = DESTINATIONS
    diff_lines: int = 30
    baseline_message: bool = True
    issue_labels: tuple[str, ...] = ("page-change",)


@dataclass(frozen=True)
class Config:
    pages: tuple[Page, ...]
    defaults: Rules
    delivery: Delivery
    state_dir: Path

    def find_page(self, ref: str) -> Page | None:
        """A page by id, name (case-insensitive) or exact URL."""
        wanted = ref.strip()
        for page in self.pages:
            if wanted in (page.id, page.url) or wanted.casefold() == page.name.casefold():
                return page
        return None


def page_id(url: str, section: str | None = None) -> str:
    """Stable id used for the state files, e.g. ``github.com-pricing``."""
    parsed = urlparse(url)
    raw = (parsed.hostname or "").removeprefix("www.") + parsed.path
    if parsed.query:
        raw += "-" + parsed.query
    if section:
        raw += "-" + section
    slug = re.sub(r"[^a-z0-9.]+", "-", raw.lower()).strip("-.")
    if len(slug) > 80:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        slug = slug[:71].rstrip("-.") + "-" + digest
    return slug or "page"


def load_config(path: str | Path) -> Config:
    """Read and validate a config file. A relative ``state_dir`` resolves next to it."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    config = parse_config(raw or {})
    if not config.state_dir.is_absolute():
        config = replace(config, state_dir=path.parent / config.state_dir)
    return config


def parse_config(raw: Any) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("the config must be a mapping with a 'pages' list")
    _reject_unknown(raw, _TOP_KEYS, "config")

    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise ConfigError("'defaults' must be a mapping")
    _reject_unknown(defaults_raw, _RULE_KEYS, "defaults")
    defaults = _parse_rules(defaults_raw, Rules(), "defaults")

    pages_raw = raw.get("pages")
    if not isinstance(pages_raw, list) or not pages_raw:
        raise ConfigError("'pages' must be a non-empty list of pages to watch")
    pages: list[Page] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(pages_raw, start=1):
        page = _parse_page(entry, index, defaults)
        if page.id in seen:
            raise ConfigError(
                f"page {index}: id {page.id!r} is already used by page {seen[page.id]}; "
                "give one of them its own 'id'"
            )
        seen[page.id] = index
        pages.append(page)

    state_dir = raw.get("state_dir", "state")
    if not isinstance(state_dir, str) or not state_dir.strip():
        raise ConfigError("'state_dir' must be a folder path")

    return Config(
        pages=tuple(pages),
        defaults=defaults,
        delivery=_parse_delivery(raw.get("delivery") or {}),
        state_dir=Path(state_dir.strip()),
    )


def _parse_page(raw: Any, index: int, defaults: Rules) -> Page:
    where = f"page {index}"
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping with at least a 'url'")
    _reject_unknown(raw, _PAGE_KEYS, where)

    url = str(raw.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"{where}: 'url' must be a full http(s):// address, got {url!r}")

    section = raw.get("section")
    if section is not None:
        if not isinstance(section, str) or not section.strip():
            raise ConfigError(f"{where}: 'section' must be the text of a heading on the page")
        section = section.strip()
    skip = _string_list(raw.get("skip_sections"), f"{where}: skip_sections")

    name = str(raw.get("name") or "").strip() or page_id(url)
    pid = raw.get("id")
    if pid is None:
        pid = page_id(url, section)
    elif not isinstance(pid, str) or not _ID_RE.match(pid):
        raise ConfigError(
            f"{where}: 'id' must be lowercase letters, digits, '.', '_' or '-' (max 80)"
        )

    # A page's own 'ignore' patterns add to the defaults instead of replacing them.
    rules = _parse_rules(raw, defaults, where)
    if "ignore" in raw:
        rules = replace(rules, ignore=defaults.ignore + rules.ignore)
    return Page(
        id=pid,
        name=name,
        url=url,
        rules=rules,
        section=section,
        skip_sections=skip,
    )


def _parse_rules(raw: dict, base: Rules, where: str) -> Rules:
    values: dict[str, Any] = {}
    if "country" in raw:
        country = raw["country"]
        if country is not None:
            country = str(country).strip().lower()
            if not _COUNTRY_RE.match(country):
                raise ConfigError(
                    f"{where}: country must be a two-letter code like 'us' or 'de', or null"
                )
        values["country"] = country
    for key in ("main_content_only", "keep_links"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ConfigError(f"{where}: {key} must be true or false")
            values[key] = raw[key]
    if "ignore" in raw:
        patterns = _string_list(raw["ignore"], f"{where}: ignore")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(
                    f"{where}: ignore pattern {pattern!r} is not a valid regex: {exc}"
                ) from None
        values["ignore"] = patterns
    limits = {
        "min_changed_chars": (1, 100000),
        "confirm_runs": (1, 10),
        "alert_after_failures": (0, 100),
    }
    for key, (low, high) in limits.items():
        if key in raw:
            values[key] = _int(raw[key], low, high, f"{where}: {key}")
    return replace(base, **values)


def _parse_delivery(raw: Any) -> Delivery:
    if not isinstance(raw, dict):
        raise ConfigError("'delivery' must be a mapping")
    _reject_unknown(raw, _DELIVERY_KEYS, "delivery")
    defaults = Delivery()

    destinations = raw.get("destinations", list(DESTINATIONS))
    if isinstance(destinations, str):
        destinations = [destinations]
    if not isinstance(destinations, list) or not destinations:
        raise ConfigError(
            f"delivery.destinations must be a list, any of: {', '.join(DESTINATIONS)}"
        )
    for name in destinations:
        if name not in DESTINATIONS:
            raise ConfigError(
                f"unknown destination {name!r}; use any of: {', '.join(DESTINATIONS)}"
            )
    ordered = tuple(name for name in DESTINATIONS if name in destinations)

    diff_lines = _int(raw.get("diff_lines", defaults.diff_lines), 3, 200, "delivery.diff_lines")
    baseline = raw.get("baseline_message", defaults.baseline_message)
    if not isinstance(baseline, bool):
        raise ConfigError("delivery.baseline_message must be true or false")
    labels = raw.get("issue_labels", list(defaults.issue_labels))
    labels = _string_list(labels, "delivery.issue_labels")
    if any(len(label) > 50 for label in labels):
        raise ConfigError("delivery.issue_labels: a GitHub label is at most 50 characters")
    return Delivery(
        destinations=ordered,
        diff_lines=diff_lines,
        baseline_message=baseline,
        issue_labels=labels,
    )


def _string_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return tuple(v.strip() for v in value)


def _int(value: Any, low: int, high: int, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{where} must be a whole number from {low} to {high}")
    return value


def _reject_unknown(raw: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {', '.join(map(repr, unknown))}; "
            f"allowed: {', '.join(sorted(allowed))}"
        )
