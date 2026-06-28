"""Tests for run_once orchestration with every external call mocked."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import pipeline
from app.config import settings
from app.models import Channel
from app.tone import get_preset


@pytest.fixture
def bot():
    return AsyncMock()


def _channel(
    *, rss_url: str | None = "http://f", tone: str = "news", chat_id: str = "@chan"
) -> Channel:
    return Channel(id=1, chat_id=chat_id, title="Chan", rss_url=rss_url, tone=tone, run_hours="9")


def _entries(n: int) -> list[dict]:
    return [{"id": str(i), "title": f"t{i}", "summary": f"s{i}", "link": f"l{i}"} for i in range(n)]


async def test_no_feed_when_url_missing(bot):
    result = await pipeline.run_once(bot, _channel(rss_url=None))
    assert result.status == "no_feed"
    bot.send_message.assert_not_called()


async def test_no_new_when_feed_empty(monkeypatch, bot):
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=[]))
    result = await pipeline.run_once(bot, _channel())
    assert result.status == "no_new"
    bot.send_message.assert_not_called()


async def test_all_seen_consults_fallback(monkeypatch, bot):
    # All entries seen → the pipeline must consult the seen-but-unpublished
    # fallback rather than immediately reporting "no_new".
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=_entries(3)))
    monkeypatch.setattr(pipeline, "filter_unseen", AsyncMock(return_value=set()))
    fallback = AsyncMock(return_value={"0", "1", "2"})  # everything already published
    monkeypatch.setattr(pipeline, "published_among", fallback)
    pick = AsyncMock()
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", pick)

    result = await pipeline.run_once(bot, _channel())

    fallback.assert_awaited_once()
    assert result.status == "no_new"
    pick.assert_not_called()
    bot.send_message.assert_not_called()


async def test_posted_happy_path(monkeypatch, bot):
    ch = _channel()
    entries = _entries(3)
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=entries))
    monkeypatch.setattr(pipeline, "filter_unseen", AsyncMock(return_value={"0", "1", "2"}))
    monkeypatch.setattr(pipeline, "recent_published_titles", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", AsyncMock(return_value=1))
    monkeypatch.setattr(pipeline.deepseek, "generate_post", AsyncMock(return_value="<b>post</b>"))
    mark = AsyncMock()
    monkeypatch.setattr(pipeline, "mark_seen", mark)

    result = await pipeline.run_once(bot, ch)

    assert result.status == "posted"
    assert result.detail == "t1"  # the picked entry's title
    bot.send_message.assert_awaited_once_with(
        chat_id=ch.chat_id, text="<b>post</b>", disable_web_page_preview=True
    )
    bot.send_photo.assert_not_called()  # this entry carries no image
    # Marked seen scoped to the channel, with the chosen one flagged published.
    args, kwargs = mark.await_args
    assert args[0] == ch.id
    assert args[1] == entries
    assert kwargs == {"published_id": "1"}


async def test_max_candidates_limits_what_deepseek_sees(monkeypatch, bot):
    monkeypatch.setattr(settings, "max_candidates", 2)
    entries = _entries(5)
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=entries))
    monkeypatch.setattr(
        pipeline, "filter_unseen", AsyncMock(return_value={"0", "1", "2", "3", "4"})
    )
    monkeypatch.setattr(pipeline, "recent_published_titles", AsyncMock(return_value=[]))
    pick = AsyncMock(return_value=0)
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", pick)
    monkeypatch.setattr(pipeline.deepseek, "generate_post", AsyncMock(return_value="p"))
    monkeypatch.setattr(pipeline, "mark_seen", AsyncMock())

    await pipeline.run_once(bot, _channel())

    shown = pick.await_args.args[0]
    assert len(shown) == 2  # capped by max_candidates


async def test_falls_back_to_seen_but_unpublished(monkeypatch, bot):
    entries = _entries(3)  # ids 0,1,2
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=entries))
    # Nothing fresh — every entry has been seen.
    monkeypatch.setattr(pipeline, "filter_unseen", AsyncMock(return_value=set()))
    # Only id "0" was actually published; "1" and "2" are seen-but-unposted.
    monkeypatch.setattr(pipeline, "published_among", AsyncMock(return_value={"0"}))
    monkeypatch.setattr(pipeline, "recent_published_titles", AsyncMock(return_value=[]))
    pick = AsyncMock(return_value=0)
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", pick)
    monkeypatch.setattr(pipeline.deepseek, "generate_post", AsyncMock(return_value="p"))
    monkeypatch.setattr(pipeline, "mark_seen", AsyncMock())

    result = await pipeline.run_once(bot, _channel())

    assert result.status == "posted"
    shown = {e["id"] for e in pick.await_args.args[0]}
    assert shown == {"1", "2"}


async def test_no_new_only_when_all_published(monkeypatch, bot):
    entries = _entries(2)
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=entries))
    monkeypatch.setattr(pipeline, "filter_unseen", AsyncMock(return_value=set()))
    monkeypatch.setattr(pipeline, "published_among", AsyncMock(return_value={"0", "1"}))
    pick = AsyncMock()
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", pick)

    result = await pipeline.run_once(bot, _channel())

    assert result.status == "no_new"
    pick.assert_not_called()
    bot.send_message.assert_not_called()


async def test_recent_published_titles_passed_to_pick(monkeypatch, bot):
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=_entries(3)))
    monkeypatch.setattr(pipeline, "filter_unseen", AsyncMock(return_value={"0", "1", "2"}))
    monkeypatch.setattr(
        pipeline, "recent_published_titles", AsyncMock(return_value=["вчерашний пост"])
    )
    pick = AsyncMock(return_value=0)
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", pick)
    monkeypatch.setattr(pipeline.deepseek, "generate_post", AsyncMock(return_value="p"))
    monkeypatch.setattr(pipeline, "mark_seen", AsyncMock())

    await pipeline.run_once(bot, _channel())

    assert pick.await_args.args[1] == ["вчерашний пост"]


async def test_publish_failure_marks_rest_but_not_chosen(monkeypatch, bot):
    entries = _entries(3)
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=entries))
    monkeypatch.setattr(pipeline, "filter_unseen", AsyncMock(return_value={"0", "1", "2"}))
    monkeypatch.setattr(pipeline, "recent_published_titles", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", AsyncMock(return_value=1))
    monkeypatch.setattr(pipeline.deepseek, "generate_post", AsyncMock(return_value="post"))
    bot.send_message.side_effect = RuntimeError("telegram down")
    mark = AsyncMock()
    monkeypatch.setattr(pipeline, "mark_seen", mark)

    result = await pipeline.run_once(bot, _channel())

    assert result.status == "error"
    assert "telegram down" in result.detail
    # The failed (chosen, id="1") entry is NOT marked, so it can be retried.
    marked = mark.await_args.args[1]  # args[0] is the channel id
    assert {e["id"] for e in marked} == {"0", "2"}


async def test_generate_post_failure_is_caught(monkeypatch, bot):
    entries = _entries(2)
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=entries))
    monkeypatch.setattr(pipeline, "filter_unseen", AsyncMock(return_value={"0", "1"}))
    monkeypatch.setattr(pipeline, "recent_published_titles", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", AsyncMock(return_value=0))
    monkeypatch.setattr(
        pipeline.deepseek, "generate_post", AsyncMock(side_effect=ValueError("llm error"))
    )
    monkeypatch.setattr(pipeline, "mark_seen", AsyncMock())

    result = await pipeline.run_once(bot, _channel())
    assert result.status == "error"
    bot.send_message.assert_not_called()


async def test_preview_sends_to_chat_and_skips_persist(monkeypatch, bot):
    entries = _entries(3)
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=entries))
    monkeypatch.setattr(pipeline, "filter_unseen", AsyncMock(return_value={"0", "1", "2"}))
    monkeypatch.setattr(pipeline, "recent_published_titles", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", AsyncMock(return_value=1))
    monkeypatch.setattr(pipeline.deepseek, "generate_post", AsyncMock(return_value="<b>p</b>"))
    mark = AsyncMock()
    monkeypatch.setattr(pipeline, "mark_seen", mark)

    result = await pipeline.run_once(bot, _channel(), chat_id=42, persist=False)

    assert result.status == "posted"
    # Post goes to the given chat, not the channel...
    bot.send_message.assert_awaited_once_with(
        chat_id=42, text="<b>p</b>", disable_web_page_preview=True
    )
    # ...and nothing is written to the DB.
    mark.assert_not_called()


async def test_preview_publish_failure_skips_persist(monkeypatch, bot):
    entries = _entries(2)
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=entries))
    monkeypatch.setattr(pipeline, "filter_unseen", AsyncMock(return_value={"0", "1"}))
    monkeypatch.setattr(pipeline, "recent_published_titles", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", AsyncMock(return_value=0))
    monkeypatch.setattr(pipeline.deepseek, "generate_post", AsyncMock(return_value="post"))
    bot.send_message.side_effect = RuntimeError("telegram down")
    mark = AsyncMock()
    monkeypatch.setattr(pipeline, "mark_seen", mark)

    result = await pipeline.run_once(bot, _channel(), chat_id=42, persist=False)

    assert result.status == "error"
    mark.assert_not_called()  # even on failure the preview leaves the DB untouched


async def test_channel_tone_passed_to_generate(monkeypatch, bot):
    entries = _entries(2)
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=entries))
    monkeypatch.setattr(pipeline, "filter_unseen", AsyncMock(return_value={"0", "1"}))
    monkeypatch.setattr(pipeline, "recent_published_titles", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", AsyncMock(return_value=0))
    gen = AsyncMock(return_value="p")
    monkeypatch.setattr(pipeline.deepseek, "generate_post", gen)
    monkeypatch.setattr(pipeline, "mark_seen", AsyncMock())

    await pipeline.run_once(bot, _channel(tone="expert"))

    # generate_post(entry, tone) — the channel's preset is forwarded.
    assert gen.await_args.args[1] is get_preset("expert")


async def test_publish_short_post_with_image_uses_caption(bot):
    await pipeline._publish(bot, 7, "<b>post</b>", "https://cdn/p.jpg")
    bot.send_photo.assert_awaited_once_with(
        chat_id=7, photo="https://cdn/p.jpg", caption="<b>post</b>"
    )
    bot.send_message.assert_not_called()


async def test_publish_long_post_with_image_splits(bot):
    text = "x" * (pipeline._CAPTION_LIMIT + 1)
    await pipeline._publish(bot, 7, text, "https://cdn/p.jpg")
    # Image first as its own message, full text after — nothing truncated.
    bot.send_photo.assert_awaited_once_with(chat_id=7, photo="https://cdn/p.jpg")
    bot.send_message.assert_awaited_once_with(
        chat_id=7, text=text, disable_web_page_preview=True
    )


async def test_publish_falls_back_to_text_when_image_fails(bot):
    bot.send_photo.side_effect = RuntimeError("bad image url")
    await pipeline._publish(bot, 7, "post", "https://cdn/dead.jpg")
    bot.send_message.assert_awaited_once_with(
        chat_id=7, text="post", disable_web_page_preview=True
    )


async def test_posted_with_image_sends_photo(monkeypatch, bot):
    ch = _channel()
    entries = [{"id": "0", "title": "t0", "summary": "s0", "image": "https://cdn/i.jpg"}]
    monkeypatch.setattr(pipeline.rss, "fetch_entries", AsyncMock(return_value=entries))
    monkeypatch.setattr(pipeline, "filter_unseen", AsyncMock(return_value={"0"}))
    monkeypatch.setattr(pipeline, "recent_published_titles", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline.deepseek, "pick_most_relevant", AsyncMock(return_value=0))
    monkeypatch.setattr(pipeline.deepseek, "generate_post", AsyncMock(return_value="<b>p</b>"))
    monkeypatch.setattr(pipeline, "mark_seen", AsyncMock())

    result = await pipeline.run_once(bot, ch)

    assert result.status == "posted"
    bot.send_photo.assert_awaited_once_with(
        chat_id=ch.chat_id, photo="https://cdn/i.jpg", caption="<b>p</b>"
    )
    bot.send_message.assert_not_called()


def test_runresult_str():
    assert str(pipeline.RunResult("posted", "title")) == "posted: title"
    assert str(pipeline.RunResult("no_new")) == "no_new"
