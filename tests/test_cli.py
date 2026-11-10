import io
import json

import pytest
import yaml
from conftest import (
    CHANGELOG,
    CHANGELOG_MD,
    ENV,
    LATER,
    NOW,
    PRICING,
    PRICING_MD,
    FakeClient,
    FakeHttp,
)
from scrapeunblocker import ServerError

from website_change_monitor.cli import annotation, build_parser, main

PRICING_V2 = PRICING_MD.replace("$4 per user / month", "$5 per user / month")


@pytest.fixture
def setup(tmp_path):
    raw = {
        "pages": [
            {"name": "Example pricing", "url": PRICING, "ignore": ["^Trusted by"]},
            {"name": "Example changelog", "url": CHANGELOG},
        ],
    }
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def run(config_path, client, http, *args, env=None, now=NOW):
    out = io.StringIO()
    code = main(
        ["run", "-c", str(config_path), "--env-file", "none", *args],
        environ=dict(ENV if env is None else env),
        client_factory=lambda environ: client,
        http_factory=lambda: http,
        now=now,
        out=out,
    )
    return code, out.getvalue()


def command(config_path, *args, client=None, http=None, env=None):
    out = io.StringIO()
    code = main(
        [args[0], "-c", str(config_path), "--env-file", "none", *args[1:]],
        environ=dict(ENV if env is None else env),
        client_factory=lambda environ: client,
        http_factory=lambda: http or FakeHttp(),
        now=LATER,
        out=out,
    )
    return code, out.getvalue()


def test_parser():
    parser = build_parser()
    args = parser.parse_args(["run", "--dry-run", "--page", "a", "--page", "b"])
    assert args.command == "run" and args.dry_run and args.page == ["a", "b"]
    assert args.config == "config.yml" and args.env_file == ".env"
    args = parser.parse_args(["preview", "https://x.example", "--raw"])
    assert args.target == "https://x.example" and args.raw
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["test-destinations", "-c", "other.yml"]).config == "other.yml"
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_first_run_baseline_then_nothing(setup):
    client = FakeClient({PRICING: PRICING_MD, CHANGELOG: CHANGELOG_MD})
    http = FakeHttp()
    code, out = run(setup, client, http)
    assert code == 0
    assert "first check (baseline)" in out
    # One "now watching" message per chat destination, no GitHub issue for a baseline.
    assert http.issues == 0
    assert len(http.to("hooks.slack.test")) == 1
    assert "Now watching 2 pages" in http.to("hooks.slack.test")[0]["text"]
    assert "$0, $4, $21" in http.to("discord.test")[0]["content"]
    (hook,) = http.to("alerts.test")
    assert hook["kind"] == "baseline" and len(hook["pages"]) == 2
    state = setup.parent / "state"
    assert (state / "pages" / "example.com-pricing.md").exists()
    index = json.loads((state / "pages.json").read_text(encoding="utf-8"))
    assert set(index["pages"]) == {"example.com-pricing", "example.org-changelog"}

    # Second run, same pages: nothing is sent and no state file changes.
    http2 = FakeHttp()
    code, out = run(setup, client, http2, now=LATER)
    assert code == 0
    assert http2.posts == []
    assert "Nothing new to deliver." in out
    assert "State unchanged" in out


def test_change_opens_an_issue_and_pings_chat_with_its_link(setup):
    client = FakeClient({PRICING: PRICING_MD, CHANGELOG: CHANGELOG_MD})
    run(setup, client, FakeHttp())
    client.pages[PRICING] = PRICING_V2
    http = FakeHttp()
    code, out = run(setup, client, http, now=LATER)
    assert code == 0
    assert "CHANGED +1 -1 lines, prices $4 → $5" in out
    issue = http.to("api.github.test")[0]
    assert issue["title"] == "Example pricing changed: $4 → $5"
    slack = http.to("hooks.slack.test")[0]["text"]
    assert "<https://github.com/o/r/issues/1|GitHub issue>" in slack
    assert "-$4 per user / month" in slack and "+$5 per user / month" in slack
    hook = http.to("alerts.test")[0]
    assert hook["issue_url"] == "https://github.com/o/r/issues/1"
    snapshot = (setup.parent / "state" / "pages" / "example.com-pricing.md").read_text("utf-8")
    assert "$5 per user / month" in snapshot
    # The issue went first: GitHub is posted to before any chat destination.
    assert "api.github.test" in http.posts[0][0]

    http2 = FakeHttp()
    run(setup, client, http2, now=LATER)
    assert http2.posts == []


def test_dry_run_sends_and_saves_nothing(setup):
    client = FakeClient({PRICING: PRICING_MD, CHANGELOG: CHANGELOG_MD})
    run(setup, client, FakeHttp())
    index_before = (setup.parent / "state" / "pages.json").read_text("utf-8")
    client.pages[PRICING] = PRICING_V2
    http = FakeHttp()
    code, out = run(setup, client, http, "--dry-run", now=LATER)
    assert code == 0
    assert http.posts == []
    assert "Dry run: 1 event(s). Nothing was sent or saved." in out
    assert "--- github_issues ---\nNew issue in o/r: Example pricing changed: $4 → $5" in out
    assert "--- slack ---" in out and "--- webhook ---" in out
    assert (setup.parent / "state" / "pages.json").read_text("utf-8") == index_before


def test_dry_run_works_without_any_destination(setup):
    client = FakeClient({PRICING: PRICING_MD, CHANGELOG: CHANGELOG_MD})
    code, out = run(setup, client, FakeHttp(), "--dry-run", env={"SCRAPEUNBLOCKER_KEY": "k"})
    assert code == 0
    assert "No destination secrets are set" in out
    assert "Now watching 2 pages" in out
    assert not (setup.parent / "state").exists()


def test_failed_delivery_keeps_state_for_a_retry(setup):
    client = FakeClient({PRICING: PRICING_MD, CHANGELOG: CHANGELOG_MD})
    run(setup, client, FakeHttp())
    client.pages[PRICING] = PRICING_V2
    down = FakeHttp(fail_hosts=("api.github.test", "slack", "discord", "alerts"))
    code, out = run(setup, client, down, now=LATER)
    assert code == 1
    assert "will be retried next run" in out
    snapshot = (setup.parent / "state" / "pages" / "example.com-pricing.md").read_text("utf-8")
    assert "$4 per user / month" in snapshot

    http = FakeHttp()
    run(setup, client, http, now=LATER)
    assert http.issues == 1


def test_one_failing_destination_does_not_block_the_others(setup):
    client = FakeClient({PRICING: PRICING_MD, CHANGELOG: CHANGELOG_MD})
    run(setup, client, FakeHttp())
    client.pages[PRICING] = PRICING_V2
    http = FakeHttp(fail_hosts=("discord",))
    code, out = run(setup, client, http, now=LATER)
    assert code == 1  # a destination failed: the workflow run shows red
    assert http.issues == 1 and http.to("hooks.slack.test")
    snapshot = (setup.parent / "state" / "pages" / "example.com-pricing.md").read_text("utf-8")
    assert "$5 per user / month" in snapshot  # delivered somewhere, so not sent again


def test_page_failures_are_annotated_and_alerted_once(setup):
    client = FakeClient({PRICING: [ServerError("down", status_code=502)], CHANGELOG: CHANGELOG_MD})
    env = {**ENV, "GITHUB_ACTIONS": "true"}
    sent = []
    for _ in range(4):
        http = FakeHttp()
        code, out = run(setup, client, http, env=env)
        sent.append(http.issues)
    assert "::warning title=Can't read Example pricing::ServerError" in out
    assert sent == [0, 0, 1, 0]
    assert code == 0  # the other page was read


def test_every_page_failing_exits_1(setup):
    err = ServerError("down", status_code=502)
    client = FakeClient({PRICING: [err], CHANGELOG: [err]})
    code, _ = run(setup, client, FakeHttp())
    assert code == 1


def test_missing_key_and_missing_destinations(setup, capsys):
    code, _ = run(setup, FakeClient({}), FakeHttp(), env={"SLACK_WEBHOOK_URL": "x"})
    assert code == 2
    assert "SCRAPEUNBLOCKER_KEY" in capsys.readouterr().err
    code, _ = run(setup, FakeClient({}), FakeHttp(), env={"SCRAPEUNBLOCKER_KEY": "k"})
    assert code == 2
    assert "no destination is set up" in capsys.readouterr().err


def test_only_selected_pages(setup):
    client = FakeClient({PRICING: PRICING_MD, CHANGELOG: CHANGELOG_MD})
    code, out = run(setup, client, FakeHttp(), "--page", "Example changelog")
    assert code == 0
    assert [url for url, _ in client.calls] == [CHANGELOG]
    code, _ = run(setup, client, FakeHttp(), "--page", "nope")
    assert code == 2


def test_status(setup):
    client = FakeClient({PRICING: PRICING_MD, CHANGELOG: CHANGELOG_MD})
    run(setup, client, FakeHttp())
    code, out = command(setup, "status")
    assert code == 0
    assert "example.com-pricing  Example pricing" in out
    assert "watching since  2026-11-10 06:17 UTC" in out
    assert "changes sent    0" in out


def test_preview_prints_the_compared_text(setup):
    client = FakeClient({PRICING: PRICING_MD})
    code, out = command(setup, "preview", "example.com-pricing", client=client)
    assert code == 0
    assert "Get started" in out and "https://example.com/signup" not in out
    assert "Trusted by" not in out  # the page's ignore pattern applies
    assert "prices: $0, $4, $21" in out


def test_preview_lists_headings_when_the_section_is_missing(tmp_path, capsys):
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump({"pages": [{"url": PRICING, "section": "Business"}]}), "utf-8")
    code, _ = command(path, "preview", PRICING, client=FakeClient({PRICING: PRICING_MD}))
    assert code == 1
    err = capsys.readouterr().err
    assert "no heading on the page contains 'Business'" in err
    assert "## Enterprise" in err


def test_preview_any_url_and_unknown_target(setup):
    client = FakeClient({"https://other.example/": "# Hello\n\n[x](https://y.example)"})
    code, out = command(setup, "preview", "https://other.example/", "--raw", client=client)
    assert code == 0 and "[x](https://y.example)" in out
    code, _ = command(setup, "preview", "not-a-page", client=client)
    assert code == 2


def test_test_destinations(setup):
    http = FakeHttp()
    code, out = command(setup, "test-destinations", http=http)
    assert code == 0
    assert "github_issues  ok" in out and "slack          ok" in out
    assert http.issues == 0
    assert len(http.posts) == 3  # slack, discord, webhook


def test_annotations_are_escaped():
    line = annotation("warning", "Acme: pricing, EU", "50% off\nnow")
    assert line == "::warning title=Acme%3A pricing%2C EU::50%25 off%0Anow"
