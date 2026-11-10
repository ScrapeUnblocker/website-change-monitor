import json

import pytest

from website_change_monitor.state import StateError, Store


def test_empty_when_nothing_is_stored(tmp_path):
    store = Store(tmp_path / "state")
    assert store.load() == {}
    assert store.read_snapshot("x") is None


def test_round_trip_and_unchanged_save_writes_nothing(tmp_path):
    store = Store(tmp_path / "state")
    pages = {"b": {"sha256": "2", "url": "u"}, "a": {"url": "u", "sha256": "1"}}
    changed = store.save(pages, {"a": ["# A", "text"]})
    assert {p.name for p in changed} == {"pages.json", "a.md"}
    assert store.load() == pages
    assert store.read_snapshot("a") == ["# A", "text"]
    assert (tmp_path / "state" / "pages" / "a.md").read_text(encoding="utf-8") == "# A\ntext\n"
    # Same content, different key order: the files stay byte-identical.
    again = {"a": {"sha256": "1", "url": "u"}, "b": {"url": "u", "sha256": "2"}}
    assert store.save(again, {"a": ["# A", "text"]}) == []


def test_index_is_sorted_json(tmp_path):
    store = Store(tmp_path)
    store.save({"z": {"b": 1, "a": 2}, "m": {}})
    data = json.loads((tmp_path / "pages.json").read_text(encoding="utf-8"))
    assert list(data["pages"]) == ["m", "z"]
    assert data["version"] == 1


def test_broken_index_is_an_error(tmp_path):
    (tmp_path / "pages.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError, match="cannot read"):
        Store(tmp_path).load()
    (tmp_path / "pages.json").write_text('{"pages": []}', encoding="utf-8")
    with pytest.raises(StateError, match="no 'pages'"):
        Store(tmp_path).load()
