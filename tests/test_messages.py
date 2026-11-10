from conftest import LATER, NOW, config

from website_change_monitor import messages
from website_change_monitor.process import BASELINE, FAILING, Event, evaluate

OLD = ["# Pricing", "## Team", "Advanced collaboration", "$4 per user / month", "- Support"]
NEW = ["# Pricing", "## Team", "Advanced collaboration", "$5 per user / month", "- Support"]


def change_event(old=OLD, new=NEW, issue_url=None):
    page = config().pages[0]
    entry = evaluate(page, old, None, None, NOW).entry
    event = evaluate(page, new, entry, old, LATER).event
    event.issue_url = issue_url
    return event


def test_plain_change_message():
    text = messages.render([change_event()], messages.PLAIN)[0]
    assert text == (
        "Example pricing changed\n"
        "https://example.com/pricing\n"
        "+1 / -1 lines · prices $4 → $5\n"
        "\n"
        "@@ ## Team @@\n"
        " Advanced collaboration\n"
        "-$4 per user / month\n"
        "+$5 per user / month\n"
        " - Support\n"
        "\n"
        "Compared with the version from 2026-11-10 06:17 UTC."
    )


def test_slack_message_links_the_issue_and_escapes():
    event = change_event(
        new=[*OLD[:3], "$5 per user / month <billed yearly> & more", "- Support"],
        issue_url="https://github.com/o/r/issues/7",
    )
    text = messages.render([event], messages.SLACK)[0]
    assert text.startswith(
        "*Example pricing changed* <https://example.com/pricing|example.com/pricing>"
    )
    assert "&lt;billed yearly&gt; &amp; more" in text
    assert "```\n@@ ## Team @@" in text
    assert "Full diff: <https://github.com/o/r/issues/7|GitHub issue>" in text


def test_discord_message_uses_diff_highlighting():
    text = messages.render([change_event()], messages.DISCORD)[0]
    assert text.startswith(
        "**Example pricing changed** [example.com/pricing](<https://example.com/pricing>)"
    )
    assert "```diff\n@@ ## Team @@" in text


def test_long_diffs_are_cut_to_fit_the_limit():
    old = ["# Page"] + [f"old line {i} " + "x" * 80 for i in range(200)]
    new = ["# Page"] + [f"new line {i} " + "y" * 80 for i in range(200)]
    text = messages.render([change_event(old, new)], messages.DISCORD, diff_lines=200, limit=1900)[
        0
    ]
    assert len(text) <= 1900
    assert "more diff lines" in text
    assert text.rstrip().endswith("UTC.")


def test_baseline_events_share_one_message():
    page_a, page_b = config().pages
    events = [
        Event(BASELINE, page_a, NOW, lines=12, prices=("$0", "$4", "$21")),
        Event(BASELINE, page_b, NOW, lines=9),
    ]
    texts = messages.render(events, messages.PLAIN)
    assert texts == [
        "Now watching 2 pages. Changes will be posted here with a diff.\n"
        "- Example pricing (https://example.com/pricing): 12 lines, prices on the page: "
        "$0, $4, $21\n"
        "- Example changelog (https://example.org/changelog): 9 lines"
    ]


def test_failing_message():
    page = config().pages[0]
    event = Event(FAILING, page, NOW, error="BlockedError: blocked", failed_runs=3)
    text = messages.render([event], messages.PLAIN)[0]
    assert text.startswith("Can't read Example pricing https://example.com/pricing\nBlockedError")
    assert "preview example.com-pricing" in text


def test_issue_title_and_body():
    event = change_event()
    assert messages.issue_title(event) == "Example pricing changed: $4 → $5"
    body = messages.issue_body(event)
    assert body.startswith("**[Example pricing](https://example.com/pricing)** changed.")
    assert "- **Lines:** +1 added, -1 removed" in body
    assert "- **Prices:** $4 → $5" in body
    assert "```diff\n@@ ## Team @@\n" in body
    assert "main content, links removed, as seen from US" in body
    assert "`state/pages/example.com-pricing.md`" in body
    assert "utm_source=github" in body


def test_issue_title_without_prices():
    event = change_event(new=[*OLD[:4], "- Priority support"])
    assert messages.issue_title(event) == "Example pricing changed (+1 / -1 lines)"


def test_issue_body_fence_survives_backticks():
    event = change_event(new=[*OLD[:4], "```code```"])
    body = messages.issue_body(event)
    assert "````diff\n" in body and body.count("````") == 2


def test_failing_issue():
    page = config().pages[0]
    event = Event(
        FAILING, page, NOW, error="no heading on the page contains 'Plans'", failed_runs=3
    )
    assert messages.issue_title(event) == "Can't read Example pricing (3 checks in a row)"
    assert "update `section`" in messages.issue_body(event)


def test_fmt_time():
    assert messages.fmt_time("2026-11-10T06:17:00Z") == "2026-11-10 06:17 UTC"
    assert messages.fmt_time(NOW) == "2026-11-10 06:17 UTC"
    assert messages.fmt_time(None) == "unknown"
