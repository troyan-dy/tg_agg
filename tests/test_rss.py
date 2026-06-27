"""Tests for the RSS normalization layer (no network)."""
from __future__ import annotations

from app.services import rss


def test_clean_strips_tags_and_unescapes():
    assert rss._clean("<p>Hello &amp; <b>world</b></p>") == "Hello & world"


def test_clean_collapses_whitespace():
    assert rss._clean("a\n\n  b\t c") == "a b c"


def test_clean_truncates_to_limit():
    assert rss._clean("x" * 100, limit=10) == "x" * 10


def test_clean_handles_none():
    assert rss._clean(None) == ""


def test_entry_id_prefers_id_then_guid_then_link_then_title():
    assert rss._entry_id({"id": "I", "guid": "G", "link": "L"}) == "I"
    assert rss._entry_id({"guid": "G", "link": "L"}) == "G"
    assert rss._entry_id({"link": "L", "title": "T"}) == "L"
    assert rss._entry_id({"title": "T"}) == "T"
    assert rss._entry_id({}) == ""


def test_normalize_shape():
    entry = {
        "id": "id-1",
        "title": "<b>Title</b>",
        "summary": "<i>Summary</i>",
        "link": "https://example.com/a",
    }
    assert rss._normalize(entry) == {
        "id": "id-1",
        "title": "Title",
        "summary": "Summary",
        "link": "https://example.com/a",
    }


async def test_fetch_entries_normalizes_limits_and_drops_idless(monkeypatch):
    class FakeParsed:
        entries = [
            {"id": "1", "title": "One", "summary": "s1", "link": "l1"},
            {"id": "2", "title": "Two", "summary": "s2", "link": "l2"},
            {"id": "3", "title": "Three", "summary": "s3", "link": "l3"},
            {"title": "no id", "link": ""},  # no usable id -> dropped
        ]

    monkeypatch.setattr(rss.feedparser, "parse", lambda url: FakeParsed())

    result = await rss.fetch_entries("http://feed", limit=2)
    assert [e["id"] for e in result] == ["1", "2"]


async def test_fetch_entries_drops_entries_without_id(monkeypatch):
    class FakeParsed:
        entries = [{"title": "", "summary": "", "link": ""}]

    monkeypatch.setattr(rss.feedparser, "parse", lambda url: FakeParsed())
    assert await rss.fetch_entries("http://feed") == []
