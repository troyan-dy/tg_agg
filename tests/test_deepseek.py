"""Tests for the DeepSeek service with the OpenAI client mocked out."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import deepseek


def _fake_client(content: str) -> SimpleNamespace:
    """Build a stand-in for AsyncOpenAI returning `content` from the LLM."""
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    create = AsyncMock(return_value=resp)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.fixture
def patch_client(monkeypatch):
    def _install(content: str):
        client = _fake_client(content)
        monkeypatch.setattr(deepseek, "_get_client", lambda: client)
        return client

    return _install


async def test_pick_single_candidate_short_circuits(monkeypatch):
    # Must not even touch the client when there's only one option.
    monkeypatch.setattr(
        deepseek, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("called"))
    )
    assert await deepseek.pick_most_relevant([{"title": "only"}]) == 0


async def test_pick_returns_parsed_index(patch_client):
    patch_client('{"index": 2, "reason": "важно"}')
    candidates = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    assert await deepseek.pick_most_relevant(candidates) == 2


async def test_pick_out_of_range_falls_back_to_zero(patch_client):
    patch_client('{"index": 9}')
    candidates = [{"title": "a"}, {"title": "b"}]
    assert await deepseek.pick_most_relevant(candidates) == 0


async def test_pick_invalid_json_falls_back_to_zero(patch_client):
    patch_client("not json at all")
    candidates = [{"title": "a"}, {"title": "b"}]
    assert await deepseek.pick_most_relevant(candidates) == 0


async def test_pick_missing_key_falls_back_to_zero(patch_client):
    patch_client('{"reason": "no index here"}')
    candidates = [{"title": "a"}, {"title": "b"}]
    assert await deepseek.pick_most_relevant(candidates) == 0


async def test_pick_includes_recent_titles_and_diversity(patch_client):
    client = patch_client('{"index": 0}')
    candidates = [{"title": "a"}, {"title": "b"}]
    await deepseek.pick_most_relevant(candidates, ["Старая новость про X"])

    prompt = client.chat.completions.create.await_args.kwargs["messages"][0]["content"]
    assert "Старая новость про X" in prompt  # recent headline is shown
    assert "РАЗНООБРАЗ" in prompt.upper()  # diversity instruction present


async def test_pick_without_recent_titles_notes_empty(patch_client):
    client = patch_client('{"index": 1}')
    candidates = [{"title": "a"}, {"title": "b"}]
    await deepseek.pick_most_relevant(candidates)

    prompt = client.chat.completions.create.await_args.kwargs["messages"][0]["content"]
    assert "ничего не публиковалось" in prompt


async def test_generate_post_strips_whitespace(patch_client):
    client = patch_client("  <b>Post</b>\n")
    entry = {"title": "T", "summary": "S", "link": "https://x/y"}
    assert await deepseek.generate_post(entry) == "<b>Post</b>"
    # Sanity: it issued exactly one completion call.
    assert client.chat.completions.create.await_count == 1
