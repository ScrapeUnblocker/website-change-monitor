import pytest
from conftest import CHANGELOG, CHANGELOG_MD, PRICING, PRICING_MD, FakeClient, config
from scrapeunblocker import AuthenticationError, BlockedError, ServerError, TargetNotFoundError

from website_change_monitor.collect import FetchError, collect, fetch_page, looks_blocked


def no_sleep(_seconds):
    pass


def test_fetch_passes_country_and_main_content():
    client = FakeClient({PRICING: PRICING_MD})
    page = config().pages[0]
    assert fetch_page(client, page) == PRICING_MD
    assert client.calls == [(PRICING, {"main_content_only": True, "proxy_country": "us"})]


def test_transient_errors_are_retried():
    blocked = BlockedError("blocked", status_code=403)
    client = FakeClient({PRICING: [blocked, PRICING_MD]})
    slept = []
    assert fetch_page(client, config().pages[0], sleep=slept.append) == PRICING_MD
    assert len(client.calls) == 2
    assert slept == [5.0]


def test_gives_up_after_the_retries():
    client = FakeClient({PRICING: [ServerError("down", status_code=502)]})
    with pytest.raises(FetchError, match="ServerError"):
        fetch_page(client, config().pages[0], sleep=no_sleep)
    assert len(client.calls) == 3


def test_target_404_is_a_page_failure_not_a_retry():
    gone = TargetNotFoundError("not found", status_code=404, origin_status=404, html="# 404")
    client = FakeClient({PRICING: [gone]})
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch_page(client, config().pages[0], sleep=no_sleep)
    assert len(client.calls) == 1


def test_bot_check_pages_are_rejected():
    client = FakeClient(
        {PRICING: "# Just a moment...\n\nVerify you are human by completing the action"}
    )
    with pytest.raises(FetchError, match="bot check"):
        fetch_page(client, config().pages[0], sleep=no_sleep)


def test_long_page_mentioning_access_denied_is_fine():
    assert not looks_blocked(PRICING_MD * 10 + "\nAccess denied errors are explained below.")
    assert looks_blocked("Access denied")


def test_empty_page_is_a_failure():
    client = FakeClient({PRICING: "  \n"})
    with pytest.raises(FetchError, match="empty"):
        fetch_page(client, config().pages[0], sleep=no_sleep)


def test_collect_records_failures_and_goes_on():
    client = FakeClient({PRICING: [ServerError("down", status_code=502)], CHANGELOG: CHANGELOG_MD})
    result = collect(client, config().pages, sleep=no_sleep)
    assert result.pages == {"example.org-changelog": CHANGELOG_MD}
    assert "ServerError" in result.failures["example.com-pricing"]


def test_fatal_errors_stop_the_run():
    client = FakeClient({PRICING: [AuthenticationError("bad key", status_code=401)]})
    with pytest.raises(AuthenticationError):
        collect(client, config().pages, sleep=no_sleep)
