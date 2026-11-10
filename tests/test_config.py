from pathlib import Path

import pytest

from website_change_monitor.config import (
    DESTINATIONS,
    ConfigError,
    load_config,
    page_id,
    parse_config,
)

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yml"


def test_shipped_config_is_valid():
    config = load_config(REPO_CONFIG)
    assert [p.id for p in config.pages] == [
        "github.com-pricing",
        "notion.com-pricing",
        "linear.app-pricing",
        "vercel.com-pricing",
    ]
    assert config.defaults.country == "us"
    assert config.delivery.destinations == DESTINATIONS
    assert config.state_dir == REPO_CONFIG.parent / "state"


def test_defaults_apply_and_pages_override():
    config = parse_config(
        {
            "defaults": {"country": "DE", "ignore": ["^Updated"], "confirm_runs": 2},
            "pages": [
                {"url": "https://a.example/pricing"},
                {
                    "url": "https://b.example/plans",
                    "country": None,
                    "ignore": ["^Trusted by"],
                    "confirm_runs": 1,
                },
            ],
        }
    )
    first, second = config.pages
    assert first.rules.country == "de"
    assert first.rules.confirm_runs == 2
    assert first.name == "a.example-pricing"
    assert second.rules.country is None
    assert second.rules.confirm_runs == 1
    # A page's ignore list adds to the defaults instead of replacing them.
    assert second.rules.ignore == ("^Updated", "^Trusted by")


def test_page_ids_are_stable_and_readable():
    assert page_id("https://www.github.com/pricing") == "github.com-pricing"
    assert page_id("https://linear.app/pricing/") == "linear.app-pricing"
    assert page_id("https://x.example/p?plan=team") == "x.example-p-plan-team"
    assert page_id("https://x.example/terms", "Section 4") == "x.example-terms-section-4"
    long_id = page_id("https://x.example/" + "a" * 200)
    assert len(long_id) <= 80


def test_destinations_keep_delivery_order():
    config = parse_config(
        {"pages": ["https://a.example"], "delivery": {"destinations": ["slack", "github_issues"]}}
    )
    assert config.delivery.destinations == ("github_issues", "slack")


def test_find_page_by_id_name_or_url():
    config = parse_config({"pages": [{"name": "Acme Pricing", "url": "https://acme.example/p"}]})
    page = config.pages[0]
    assert config.find_page("acme.example-p") is page
    assert config.find_page("acme pricing") is page
    assert config.find_page("https://acme.example/p") is page
    assert config.find_page("nope") is None


def test_short_url_drops_scheme_and_www():
    config = parse_config({"pages": ["https://www.notion.com/pricing/"]})
    assert config.pages[0].short_url == "notion.com/pricing"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "'pages' must be a non-empty list"),
        ({"pages": [{"url": "notion.com/pricing"}]}, "must be a full http"),
        ({"pages": ["https://a.example"], "colour": 1}, "unknown key"),
        ({"pages": [{"url": "https://a.example", "selector": "main"}]}, "unknown key"),
        ({"pages": ["https://a.example", "https://a.example"]}, "already used by page 1"),
        ({"pages": ["https://a.example"], "defaults": {"country": "usa"}}, "two-letter"),
        ({"pages": ["https://a.example"], "defaults": {"ignore": ["("]}}, "not a valid regex"),
        ({"pages": ["https://a.example"], "defaults": {"confirm_runs": 0}}, "1 to 10"),
        ({"pages": ["https://a.example"], "defaults": {"keep_links": "no"}}, "true or false"),
        ({"pages": [{"url": "https://a.example", "section": ""}]}, "heading"),
        ({"pages": [{"url": "https://a.example", "id": "Bad Id"}]}, "'id' must be"),
        ({"pages": ["https://a.example"], "delivery": {"destinations": ["sms"]}}, "unknown dest"),
        ({"pages": ["https://a.example"], "delivery": {"diff_lines": 1}}, "3 to 200"),
    ],
)
def test_invalid_config_is_rejected(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_missing_and_broken_files(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yml")
    bad = tmp_path / "bad.yml"
    bad.write_text("pages: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)
