from dataclasses import replace

import pytest
from conftest import LATER, NOW, PRICING, PRICING_MD, config

from website_change_monitor.process import (
    BASELINE,
    CHANGE,
    FAILING,
    PriceChange,
    SectionNotFound,
    compare,
    drop_section,
    evaluate,
    fingerprint,
    keep_section,
    normalize,
    prices_in,
    record_failure,
    unique_prices,
)


def pricing_page(**changes):
    page = config().pages[0]
    rules = {k: v for k, v in changes.items() if k in page.rules.__dataclass_fields__}
    other = {k: v for k, v in changes.items() if k not in rules}
    return replace(page, rules=replace(page.rules, **rules), **other)


# ------------------------------------------------------------------------- normalize


def test_normalize_strips_links_images_and_blank_lines():
    md = (
        "# Title[¶](https://x.example/#title)\n\n"
        "[Get started](https://x.example/signup?ref=a&exp=7) now\n"
        "![Logo](https://cdn.example/logo.png?v=123)\n"
        "Mail [\\[email protected\\]](https://x.example/cdn-cgi/l/email-protection#abc)\n"
        "Zero\N{ZERO WIDTH SPACE}width   and   spaces  \n"
    )
    lines = normalize(md, pricing_page())
    assert lines == [
        "# Title",
        "Get started now",
        "Logo",
        "Mail [email protected]",
        "Zerowidth and spaces",
    ]


def test_normalize_can_keep_links():
    lines = normalize("[Docs](https://x.example/docs)", pricing_page(keep_links=True))
    assert lines == ["[Docs](https://x.example/docs)"]


def test_links_with_parentheses_in_the_url():
    lines = normalize("See [Tax](https://en.example/wiki/Tax_(law)) here", pricing_page())
    assert lines == ["See Tax here"]


def test_ignore_patterns_drop_matching_lines():
    lines = normalize(PRICING_MD, pricing_page(ignore=("^Trusted by more than",)))
    assert not any(line.startswith("Trusted by") for line in lines)
    assert "$21 per user / month" in lines


def test_keep_section_takes_heading_and_its_subsections():
    lines = ["# Pricing", "## Team", "$4", "### Extras", "x", "## Enterprise", "$21"]
    assert keep_section(lines, "team") == ["## Team", "$4", "### Extras", "x"]
    assert keep_section(lines, "Enterprise") == ["## Enterprise", "$21"]
    with pytest.raises(SectionNotFound):
        keep_section(lines, "Business")


def test_drop_section_removes_it_and_ignores_missing():
    lines = ["## Team", "$4", "## FAQ", "Q?", "A.", "## Contact", "mail"]
    assert drop_section(lines, "faq") == ["## Team", "$4", "## Contact", "mail"]
    assert drop_section(lines, "missing") == lines


def test_section_matches_bold_and_linked_headings():
    md = "### [**Custom Agents**](https://x.example/agents)\nFrom $10\n### Other\nno"
    lines = normalize(md, pricing_page(section="custom agents"))
    assert lines == ["### **Custom Agents**", "From $10"]


def test_skip_sections_in_normalize():
    lines = normalize(PRICING_MD, pricing_page(skip_sections=("Questions & answers",)))
    assert "## Questions & answers" not in lines
    assert lines[-1] == "Trusted by more than **50,000** companies"


# ----------------------------------------------------------------------------- prices


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$4USDper user / month", ["$4"]),
        ("€9.50per seat / month", ["€9.50"]),
        ("From $1,200, billed yearly.", ["$1,200"]),
        ("1,200 USD or 1100 EUR", ["1,200 USD", "1100 EUR"]),
        ("£5 and US$7 and A$9", ["£5", "US$7", "A$9"]),
        ("10 projects, 2,000 minutes", []),
    ],
)
def test_prices_in(text, expected):
    assert prices_in(text) == expected


def test_unique_prices_keep_first_order():
    assert unique_prices(["$0 free", "$4 team", "$0 again", "$21"]) == ["$0", "$4", "$21"]


# ------------------------------------------------------------------------------- diff


def test_compare_shows_hunk_with_nearest_heading():
    old = ["# Pricing", "## Team", "Collaboration", "$4 per user / month", "- Support"]
    new = ["# Pricing", "## Team", "Collaboration", "$5 per user / month", "- Support"]
    diff = compare(old, new)
    assert diff.added == 1 and diff.removed == 1
    assert diff.lines == (
        "@@ ## Team @@",
        " Collaboration",
        "-$4 per user / month",
        "+$5 per user / month",
        " - Support",
    )
    assert diff.prices == (PriceChange("$4", "$5"),)
    assert diff.changed_chars == 1


def test_compare_prices_removed_new_and_moved():
    old = ["## A", "Basic $10", "## B", "Pro $20", "## C", "Max $99"]
    new = ["## A", "## B", "Pro $20", "Starter $10", "## C", "Max $129", "Ultra $299"]
    diff = compare(old, new)
    # $10 only moved; $99 became $129; $299 is new.
    assert diff.prices == (PriceChange("$99", "$129"), PriceChange(None, "$299"))


def test_compare_dedupes_repeated_price_changes():
    old = ["Plus $10 per seat", "x", "x", "x", "Plus $10 per seat/month"]
    new = ["Plus $12 per seat", "x", "x", "x", "Plus $12 per seat/month"]
    assert compare(old, new).prices == (PriceChange("$10", "$12"),)


def test_to_text_truncates():
    diff = compare(["a"] * 3, ["b"] * 3)
    text = diff.to_text(3)
    assert text.splitlines()[-1] == "... 4 more diff lines"


def test_price_change_labels():
    assert str(PriceChange("$4", "$5")) == "$4 → $5"
    assert str(PriceChange("$4", None)) == "removed $4"
    assert str(PriceChange(None, "$5")) == "new $5"


# ---------------------------------------------------------------------------- evaluate

FEATURES = [f"- Feature number {i} with a longer description" for i in range(40)]
V1 = ["# Pricing", "## Team", "$4 per user / month", *FEATURES]
V2 = ["# Pricing", "## Team", "$5 per user / month", *FEATURES]


def first(lines=V1, page=None, **kw):
    return evaluate(page or pricing_page(), lines, None, None, NOW, **kw)


def test_first_check_is_a_baseline():
    verdict = first()
    assert verdict.event.kind == BASELINE
    assert verdict.event.prices == ("$4",)
    assert verdict.snapshot == V1
    assert verdict.entry["sha256"] == fingerprint(V1)
    assert verdict.entry["first_seen"] == "2026-11-10T06:17:00Z"
    assert verdict.entry["changes"] == 0


def test_baseline_message_can_be_turned_off():
    verdict = first(baseline_message=False)
    assert verdict.event is None and verdict.snapshot == V1


def test_unchanged_page_sends_nothing_and_keeps_state():
    entry = first().entry
    verdict = evaluate(pricing_page(), list(V1), entry, V1, LATER)
    assert verdict.event is None
    assert verdict.snapshot is None
    assert verdict.entry == entry
    assert verdict.note == "unchanged"


def test_change_is_delivered_once_then_settles():
    entry = first().entry
    verdict = evaluate(pricing_page(), V2, entry, V1, LATER)
    assert verdict.event.kind == CHANGE
    assert verdict.event.previous_at == "2026-11-10T06:17:00Z"
    assert verdict.event.diff.prices == (PriceChange("$4", "$5"),)
    assert verdict.snapshot == V2
    assert verdict.entry["changes"] == 1
    assert verdict.entry["last_changed"] == "2026-11-10T12:17:00Z"
    again = evaluate(pricing_page(), V2, verdict.entry, V2, LATER)
    assert again.event is None


def test_small_changes_below_threshold_accumulate():
    page = pricing_page(min_changed_chars=5)
    entry = first(page=page).entry
    tiny = ["# Pricing", "## Team", "$5 per user / month"] + V1[3:]
    verdict = evaluate(page, tiny, entry, V1, LATER)
    assert verdict.event is None and verdict.snapshot is None
    assert "below min_changed_chars" in verdict.note
    # The stored version stays, so drift adds up and is sent once it is big enough.
    bigger = ["# Pricing", "## Team", "$5 per seat / month, billed yearly"] + V1[3:]
    later = evaluate(page, bigger, verdict.entry, V1, LATER)
    assert later.event.kind == CHANGE


def test_confirm_runs_waits_for_a_repeat():
    page = pricing_page(confirm_runs=2)
    entry = first(page=page).entry
    held = evaluate(page, V2, entry, V1, LATER)
    assert held.event is None and held.snapshot is None
    assert held.entry["pending"] == {"sha256": fingerprint(V2), "count": 1}
    sent = evaluate(page, V2, held.entry, V1, LATER)
    assert sent.event.kind == CHANGE
    assert "pending" not in sent.entry


def test_flip_back_clears_pending():
    page = pricing_page(confirm_runs=2)
    entry = first(page=page).entry
    held = evaluate(page, V2, entry, V1, LATER)
    back = evaluate(page, V1, held.entry, V1, LATER)
    assert "pending" not in back.entry and back.event is None


def test_big_shrink_needs_a_second_look():
    entry = first().entry
    short = V1[:3]
    held = evaluate(pricing_page(), short, entry, V1, LATER)
    assert held.event is None
    assert "shrank by more than half" in held.note
    confirmed = evaluate(pricing_page(), short, held.entry, V1, LATER)
    assert confirmed.event.kind == CHANGE


def test_failures_alert_once_at_the_threshold():
    page = pricing_page()
    entry = first().entry
    v1 = record_failure(page, "BlockedError: x", entry, NOW)
    v2 = record_failure(page, "BlockedError: x", v1.entry, NOW)
    assert v1.event is None and v2.event is None
    v3 = record_failure(page, "BlockedError: x", v2.entry, LATER)
    assert v3.event.kind == FAILING and v3.event.failed_runs == 3
    v4 = record_failure(page, "BlockedError: x", v3.entry, LATER)
    assert v4.event is None and v4.entry == v3.entry  # the counter stops changing
    recovered = evaluate(page, V1, v4.entry, V1, LATER)
    assert "failed_runs" not in recovered.entry and recovered.event is None


def test_failure_alerts_can_be_turned_off():
    page = pricing_page(alert_after_failures=0)
    verdict = record_failure(page, "x", None, NOW)
    assert verdict.event is None and "failed_runs" not in verdict.entry


def test_page_that_failed_first_gets_a_baseline_later():
    page = pricing_page()
    failed = record_failure(page, "x", None, NOW)
    verdict = evaluate(page, V1, failed.entry, None, LATER)
    assert verdict.event.kind == BASELINE
    assert "failed_runs" not in verdict.entry


def test_entry_records_url_and_name():
    verdict = first()
    assert verdict.entry["url"] == PRICING
    assert verdict.entry["name"] == "Example pricing"
