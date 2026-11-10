"""Command line: ``website-change-monitor run | preview | status | test-destinations``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from scrapeunblocker import Client

from . import __version__, messages
from .collect import FATAL, FetchError, MarkdownClient, collect, fetch_page
from .config import Config, ConfigError, Page, load_config, page_id
from .process import (
    BASELINE,
    SectionNotFound,
    Verdict,
    evaluate,
    heading,
    normalize,
    record_failure,
    text_size,
    unique_prices,
)
from .sinks import ENV_HELP, DeliveryError, Sink, build_sinks, new_http_client
from .state import StateError, Store

API_KEY_ENV = "SCRAPEUNBLOCKER_KEY"
SIGNUP_URL = (
    "https://scrapeunblocker.com/?utm_source=github&utm_medium=integration"
    "&utm_campaign=example-repos"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="website-change-monitor",
        description=(
            "Watch web pages (pricing, plans, changelogs, terms) and deliver real changes "
            "as GitHub issues with the diff and as Slack, Discord or webhook messages. Pages "
            "are read as Markdown through the ScrapeUnblocker getPageSource API."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", default="config.yml", help="config file (default: config.yml)"
    )
    common.add_argument(
        "--env-file",
        default=".env",
        help="read environment variables from this file when it exists (default: .env)",
    )
    common.add_argument("-v", "--verbose", action="store_true", help="log every API call")

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    run = sub.add_parser("run", parents=[common], help="check every page now, deliver changes")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print exactly what would be delivered; deliver nothing and save no state",
    )
    run.add_argument(
        "--page",
        action="append",
        metavar="ID",
        help="only check this page (id, name or URL); repeat for more",
    )
    preview = sub.add_parser(
        "preview",
        parents=[common],
        help="fetch one page and print the text that is compared (1 API call)",
    )
    preview.add_argument("target", help="a page id or name from config.yml, or any URL")
    preview.add_argument(
        "--raw", action="store_true", help="print the Markdown as returned, before filtering"
    )
    sub.add_parser(
        "status", parents=[common], help="show what is stored for each page (no API calls)"
    )
    sub.add_parser(
        "test-destinations",
        parents=[common],
        help="send a test message to each chat destination, check GitHub access (no API calls)",
    )
    return parser


def load_env_file(path: str | Path, environ: MutableMapping[str, str]) -> None:
    """Minimal ``.env`` reader: ``KEY=value`` lines; real environment variables win."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in environ:
            environ[key] = value


def annotation(level: str, title: str, message: str) -> str:
    """A GitHub Actions workflow command (``::warning title=...::...``), safely escaped."""

    def data(text: str) -> str:
        return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    prop = data(title).replace(":", "%3A").replace(",", "%2C")
    return f"::{level} title={prop}::{data(message)}"


def check_pages(
    config: Config,
    client: MarkdownClient,
    pages: Sequence[Page],
    store: Store,
    now: datetime,
    *,
    out: TextIO,
    annotate: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> tuple[dict[str, Verdict], int]:
    """Fetch and evaluate ``pages``. Returns ``(verdicts by page id, pages read)``."""
    index = store.load()
    kwargs: dict[str, Any] = {"sleep": sleep} if sleep else {}
    result = collect(client, pages, **kwargs)
    verdicts: dict[str, Verdict] = {}
    print(
        f"Checked {len(result.pages)} of {len(pages)} pages at {messages.fmt_time(now)}:",
        file=out,
    )
    width = max(len(page.id) for page in pages)
    for page in pages:
        entry = index.get(page.id)
        error = result.failures.get(page.id)
        lines: list[str] = []
        if error is None:
            try:
                lines = normalize(result.pages[page.id], page)
            except SectionNotFound as exc:
                error = str(exc)
            else:
                if not lines:
                    error = "nothing left to compare after the section and ignore filters"
        if error is not None:
            verdict = record_failure(page, error, entry, now)
            if annotate:
                print(annotation("warning", f"Can't read {page.name}", error), file=out)
        else:
            verdict = evaluate(
                page,
                lines,
                entry,
                store.read_snapshot(page.id),
                now,
                baseline_message=config.delivery.baseline_message,
            )
            if annotate and verdict.event is not None and verdict.event.kind != BASELINE:
                print(annotation("notice", f"{page.name} changed", verdict.note), file=out)
        verdicts[page.id] = verdict
        print(f"  {page.id:<{width}}  {verdict.note}", file=out)
    return verdicts, len(result.pages)


def deliver(
    events: Sequence[Any], sinks: Sequence[Sink], *, err: TextIO
) -> tuple[set[int], set[int], list[str]]:
    """Send events to every sink that accepts them, GitHub Issues first.

    Returns ``(attempted, delivered, failed_sinks)``, the first two as sets of ``id(event)``.
    """
    groups: list[list[Any]] = []
    baselines = [e for e in events if e.kind == BASELINE]
    if baselines:
        groups.append(baselines)  # one "now watching" message for all new pages
    groups.extend([e] for e in events if e.kind != BASELINE)
    attempted: set[int] = set()
    delivered: set[int] = set()
    failed: list[str] = []
    for sink in sinks:
        for group in groups:
            batch = [e for e in group if sink.accepts(e)]
            if not batch:
                continue
            attempted.update(id(e) for e in batch)
            try:
                sink.send(batch)
            except DeliveryError as exc:
                print(f"Delivery failed: {exc}", file=err)
                if sink.name not in failed:
                    failed.append(sink.name)
            else:
                delivered.update(id(e) for e in batch)
    return attempted, delivered, failed


def run_check(
    config: Config,
    client: MarkdownClient,
    sinks: Sequence[Sink],
    *,
    dry_run: bool,
    now: datetime,
    pages: Sequence[Page] | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    annotate: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """One scheduled check: collect, compare, deliver, save. Returns the exit code."""
    pages = list(pages or config.pages)
    store = Store(config.state_dir)
    index = store.load()
    verdicts, read = check_pages(
        config, client, pages, store, now, out=out, annotate=annotate, sleep=sleep
    )
    events = [v.event for v in verdicts.values() if v.event is not None]

    if dry_run:
        print(file=out)
        if not events:
            print("Dry run: nothing new to deliver. Nothing was sent or saved.", file=out)
            return 0
        print(f"Dry run: {len(events)} event(s). Nothing was sent or saved.", file=out)
        if not sinks:
            print("No destination secrets are set; the message(s) would be:", file=out)
            for text in messages.render(events, messages.PLAIN):
                print(f"\n{text}", file=out)
        for sink in sinks:
            accepted = [e for e in events if sink.accepts(e)]
            for text in sink.preview(accepted) if accepted else []:
                print(f"\n--- {sink.name} ---\n{text}", file=out)
        return 0

    attempted, delivered, failed = deliver(events, sinks, err=err)
    new_index = dict(index)
    snapshots: dict[str, list[str]] = {}
    retry = 0
    for pid, verdict in verdicts.items():
        event = verdict.event
        if event is not None and id(event) in attempted and id(event) not in delivered:
            retry += 1  # no destination took it: keep the old state so the next run retries
            continue
        new_index[pid] = verdict.entry
        if verdict.snapshot is not None:
            snapshots[pid] = verdict.snapshot

    if events:
        names = [s.name for s in sinks if s.name not in failed]
        sent = len(events) - retry
        if names and sent:
            print(f"\nDelivered {sent} event(s) to {', '.join(names)}.", file=out)
        if retry:
            print(f"{retry} event(s) were not delivered; they will be retried next run.", file=out)
    else:
        print("\nNothing new to deliver.", file=out)

    changed = store.save(new_index, snapshots)
    if changed:
        print(f"State saved: {len(changed)} file(s) in {config.state_dir}", file=out)
    else:
        print(f"State unchanged: {config.state_dir}", file=out)
    if failed or read == 0:
        return 1
    return 0


def preview_page(
    config: Config, client: MarkdownClient, target: str, *, raw: bool, out: TextIO
) -> int:
    page = config.find_page(target)
    if page is None:
        if not target.startswith(("http://", "https://")):
            ids = ", ".join(p.id for p in config.pages)
            print(
                f"error: {target!r} is not a page in the config ({ids}) or a URL", file=sys.stderr
            )
            return 2
        page = Page(id=page_id(target), name=target, url=target, rules=config.defaults)
    try:
        markdown = fetch_page(client, page)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if raw:
        print(markdown, file=out)
        return 0
    try:
        lines = normalize(markdown, page)
    except SectionNotFound as exc:
        headings = [
            line for line in normalize(markdown, replace(page, section=None)) if heading(line)
        ]
        print(f"error: {exc}. Headings on the page:", file=sys.stderr)
        for line in headings[:60]:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("\n".join(lines), file=out)
    prices = unique_prices(lines)
    summary = f"{len(lines)} lines, {text_size(lines):,} characters compared for {page.id}"
    if prices:
        summary += f"; prices: {', '.join(prices[:12])}"
    print(f"\n--- {summary} ---", file=out)
    return 0


def show_status(config: Config, out: TextIO) -> int:
    store = Store(config.state_dir)
    index = store.load()
    print(f"State folder: {config.state_dir}", file=out)
    for page in config.pages:
        entry = index.get(page.id) or {}
        print(f"\n{page.id}  {page.name}\n  {page.url}", file=out)
        if not entry.get("sha256"):
            print("  not checked yet", file=out)
        else:
            rows = [
                ("watching since", messages.fmt_time(entry.get("first_seen"))),
                ("last change", messages.fmt_time(entry.get("last_changed"))),
                ("changes sent", str(entry.get("changes", 0))),
                ("stored text", f"{entry.get('lines', 0)} lines in {store.snapshot_file(page.id)}"),
            ]
            for label, value in rows:
                print(f"  {label:<16}{value}", file=out)
        pending = entry.get("pending")
        if pending:
            needed = max(page.rules.confirm_runs, 2)
            print(f"  {'pending':<16}a new version seen {pending.get('count')}/{needed}", file=out)
        if entry.get("failed_runs"):
            print(f"  {'failing':<16}{entry['failed_runs']} failed check(s) in a row", file=out)
    return 0


def test_destinations(sinks: Sequence[Sink], skipped: Sequence[str], out: TextIO) -> int:
    for name in skipped:
        print(f"  {name:<14} skipped ({ENV_HELP[name]} not set)", file=out)
    failed = 0
    for sink in sinks:
        try:
            result = sink.test()
        except DeliveryError as exc:
            failed += 1
            print(f"  {sink.name:<14} FAILED {exc}", file=out)
        else:
            print(f"  {sink.name:<14} ok, {result}", file=out)
    return 1 if failed else 0


def _no_destination_error(skipped: Sequence[str]) -> None:
    wanted = "; ".join(f"{ENV_HELP[name]} ({name})" for name in skipped)
    print(f"error: no destination is set up. Set at least one of: {wanted}", file=sys.stderr)


def _make_client(environ: Mapping[str, str]) -> Client:
    return Client(api_key=environ[API_KEY_ENV].strip())


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    client_factory: Callable[[Mapping[str, str]], MarkdownClient] = _make_client,
    http_factory: Callable[[], Any] = new_http_client,
    now: datetime | None = None,
    out: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    if hasattr(out, "reconfigure"):
        # Page text is full of non-ASCII; never crash on a console that cannot show it.
        out.reconfigure(errors="replace")
    now = now or datetime.now(UTC).replace(microsecond=0)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO, and webhook URLs are secrets.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    load_env_file(args.env_file, environ)
    try:
        config = load_config(args.config)
        if args.command == "status":
            return show_status(config, out)
        if args.command in ("run", "preview") and not (environ.get(API_KEY_ENV) or "").strip():
            print(
                f"error: set {API_KEY_ENV} to your ScrapeUnblocker API key "
                f"(get one at {SIGNUP_URL})",
                file=sys.stderr,
            )
            return 2
        if args.command == "preview":
            return preview_page(config, client_factory(environ), args.target, raw=args.raw, out=out)

        http = http_factory()
        try:
            sinks, skipped = build_sinks(config.delivery, environ, http)
            if args.command == "test-destinations":
                if not sinks:
                    _no_destination_error(skipped)
                    return 2
                return test_destinations(sinks, skipped, out)
            return _run_command(config, sinks, skipped, args, environ, client_factory, now, out)
        finally:
            close = getattr(http, "close", None)
            if callable(close):
                close()
    except (ConfigError, StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FATAL as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _run_command(
    config: Config,
    sinks: Sequence[Sink],
    skipped: Sequence[str],
    args: argparse.Namespace,
    environ: Mapping[str, str],
    client_factory: Callable[[Mapping[str, str]], MarkdownClient],
    now: datetime,
    out: TextIO,
) -> int:
    pages = list(config.pages)
    if args.page:
        pages = []
        for ref in args.page:
            page = config.find_page(ref)
            if page is None:
                raise ConfigError(f"--page {ref!r} is not a page id, name or URL in the config")
            pages.append(page)
    if not sinks and not args.dry_run:
        _no_destination_error(skipped)
        return 2
    for name in skipped:
        print(f"({name} skipped: {ENV_HELP[name]} not set)", file=out)
    return run_check(
        config,
        client_factory(environ),
        sinks,
        dry_run=args.dry_run,
        now=now,
        pages=pages,
        out=out,
        annotate=environ.get("GITHUB_ACTIONS") == "true",
    )


if __name__ == "__main__":
    raise SystemExit(main())
