import pytest
from conftest import ENV, LATER, NOW, FakeHttp, FakeResponse, config

from website_change_monitor.config import Delivery
from website_change_monitor.process import BASELINE, Event, evaluate
from website_change_monitor.sinks import (
    DeliveryError,
    DiscordSink,
    GitHubIssuesSink,
    SlackSink,
    WebhookSink,
    build_sinks,
    request,
)

OLD = ["# Pricing", "## Team", "$4 per user / month"]
NEW = ["# Pricing", "## Team", "$5 per user / month"]


def change_event():
    page = config().pages[0]
    entry = evaluate(page, OLD, None, None, NOW).entry
    return evaluate(page, NEW, entry, OLD, LATER).event


def baseline_event():
    return Event(BASELINE, config().pages[0], NOW, lines=3, prices=("$4",))


def test_github_issue_payload_and_url_is_kept():
    http = FakeHttp()
    sink = GitHubIssuesSink("tok", "o/r", http, api_base="https://api.github.test")
    event = change_event()
    sink.send([event])
    url, body, headers = http.posts[0]
    assert url == "https://api.github.test/repos/o/r/issues"
    assert body["title"] == "Example pricing changed: $4 → $5"
    assert body["labels"] == ["page-change"]
    assert "```diff" in body["body"]
    assert headers["Authorization"] == "Bearer tok"
    assert event.issue_url == "https://github.com/o/r/issues/1"


def test_github_only_takes_changes_and_failures():
    sink = GitHubIssuesSink("tok", "o/r", FakeHttp())
    assert sink.accepts(change_event())
    assert not sink.accepts(baseline_event())


class LabelRejectingHttp(FakeHttp):
    def post(self, url, *, json, headers=None, timeout):
        if "labels" in json:
            self.posts.append((url, json, headers))
            return FakeResponse(422, text="Validation Failed")
        return super().post(url, json=json, headers=headers, timeout=timeout)


def test_github_retries_without_labels_on_422():
    http = LabelRejectingHttp()
    event = change_event()
    GitHubIssuesSink("tok", "o/r", http).send([event])
    assert "labels" in http.posts[0][1] and "labels" not in http.posts[1][1]
    assert event.issue_url


@pytest.mark.parametrize(
    ("status", "text", "hint"),
    [
        (401, "", "GITHUB_TOKEN was rejected"),
        (403, "Resource not accessible by integration", "issues: write"),
        (404, "", "not found"),
        (410, "", "issues are turned off"),
    ],
)
def test_github_errors_explain_the_fix(status, text, hint):
    class Http(FakeHttp):
        def post(self, url, *, json, headers=None, timeout):
            return FakeResponse(status, text=text)

    with pytest.raises(DeliveryError, match=hint):
        GitHubIssuesSink("tok", "o/r", Http()).send([change_event()])


def test_github_test_checks_access_without_creating_an_issue():
    http = FakeHttp()
    result = GitHubIssuesSink("tok", "o/r", http).test()
    assert "no issue created" in result
    assert http.gets and not http.posts


def test_slack_and_discord_payloads():
    http = FakeHttp()
    SlackSink("https://hooks.slack.test/x", http).send([change_event()])
    DiscordSink("https://discord.test/x", http).send([change_event()])
    slack, discord = http.posts[0][1], http.posts[1][1]
    assert slack["text"].startswith("*Example pricing changed*")
    assert slack["unfurl_links"] is False
    assert discord["content"].startswith("**Example pricing changed**")
    assert discord["allowed_mentions"] == {"parse": []}
    assert discord["flags"] == 4


def test_webhook_posts_text_and_fields_per_event():
    http = FakeHttp()
    WebhookSink("https://alerts.test/hook", http).send([change_event(), baseline_event()])
    baseline, change = (body for _, body, _ in http.posts)
    assert change["source"] == "website-change-monitor"
    assert change["kind"] == "change"
    assert change["prices"] == [{"before": "$4", "after": "$5"}]
    assert change["added_lines"] == 1 and change["removed_lines"] == 1
    assert "-$4 per user / month\n+$5 per user / month" in change["diff"]
    assert change["text"].startswith("Example pricing changed")
    assert baseline["kind"] == "baseline"
    assert baseline["text"].startswith("Now watching 1 page.")
    assert baseline["pages"][0]["prices_seen"] == ["$4"]


def test_http_errors_never_leak_the_webhook_url():
    http = FakeHttp(fail_hosts=("hooks.slack.test",))
    with pytest.raises(DeliveryError) as info:
        SlackSink("https://hooks.slack.test/services/SECRET", http).send([change_event()])
    assert "SECRET" not in str(info.value)
    assert "HTTP 500" in str(info.value)


def test_request_retries_once_on_429():
    class Http(FakeHttp):
        calls = 0

        def post(self, url, *, json, headers=None, timeout):
            Http.calls += 1
            return FakeResponse(429 if Http.calls == 1 else 200, headers={"Retry-After": "1"})

    slept = []
    resp = request(Http(), "POST", "https://x.test", sink="x", payload={}, sleep=slept.append)
    assert resp.status_code == 200 and slept == [1.0]


def test_build_sinks_skips_missing_secrets_and_keeps_order():
    env = {k: v for k, v in ENV.items() if k != "DISCORD_WEBHOOK_URL"}
    sinks, skipped = build_sinks(Delivery(), env, FakeHttp())
    assert [s.name for s in sinks] == ["github_issues", "slack", "webhook"]
    assert skipped == ["discord"]
    github = sinks[0]
    assert github.api_base == "https://api.github.test"


def test_github_needs_both_token_and_repository():
    env = {"GITHUB_TOKEN": "tok", "SLACK_WEBHOOK_URL": " "}
    sinks, skipped = build_sinks(Delivery(), env, FakeHttp())
    assert sinks == [] and skipped == ["github_issues", "slack", "discord", "webhook"]
