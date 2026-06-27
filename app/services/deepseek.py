"""DeepSeek (OpenAI-compatible) calls: pick the top story and write a post."""

from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

from app.config import settings

log = logging.getLogger("deepseek")

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url
        )
    return _client


async def pick_most_relevant(candidates: list[dict], recent_titles: list[str] | None = None) -> int:
    """Return the index of the best candidate to publish next.

    `candidates` is a list of {title, summary} that have NOT been published yet.
    `recent_titles` are headlines already posted in the last day — passed to the
    model so it can diversify topics and avoid repeating a recent story.
    Falls back to 0 on any parsing problem so the pipeline still posts something.
    """
    if len(candidates) == 1:
        return 0

    listing = "\n".join(
        f"{i}. {c['title']}\n   {c.get('summary', '')}" for i, c in enumerate(candidates)
    )
    published = (
        "\n".join(f"- {t}" for t in recent_titles)
        if recent_titles
        else "(за последние сутки ничего не публиковалось)"
    )
    prompt = (
        "Ты редактор Telegram-канала и подбираешь следующую публикацию.\n\n"
        "Уже опубликовано за последние сутки:\n"
        f"{published}\n\n"
        "Свежие новости-кандидаты (ещё не публиковались):\n"
        f"{listing}\n\n"
        "Выбери ОДНУ новость для публикации прямо сейчас. Она должна быть важной и "
        "актуальной, но при этом РАЗНООБРАЗИТЬ ленту: по теме и сюжету отличаться "
        "от уже опубликованного выше — не повторяй недавние темы. Если несколько "
        "кандидатов про одно и то же, предпочти новость из другой тематики.\n"
        'Ответь строго JSON-объектом вида {"index": <число>, "reason": "<кратко>"}.'
    )
    resp = await _get_client().chat.completions.create(
        model=settings.deepseek_model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    try:
        data = json.loads(resp.choices[0].message.content or "")
        index = int(data["index"])
        if 0 <= index < len(candidates):
            log.info("Picked #%s: %s", index, data.get("reason", ""))
            return index
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        log.warning("Could not parse DeepSeek pick, defaulting to 0")
    return 0


async def generate_post(entry: dict) -> str:
    """Generate a ready-to-send Telegram post (HTML) for a news entry."""
    prompt = (
        f"Напиши пост для Telegram-канала СТРОГО на {settings.post_language} языке "
        "по этой новости.\n"
        f"Заголовок: {entry['title']}\n"
        f"Описание: {entry.get('summary', '')}\n"
        f"Ссылка: {entry.get('link', '')}\n\n"
        "Требования:\n"
        "- 2–5 коротких абзацев, живо и по делу, без воды и кликбейта.\n"
        "- Допустима только Telegram HTML-разметка: <b>, <i>, <a href>. Без markdown.\n"
        "- В конце добавь ссылку на источник через <a href>.\n"
        "- Не используй заголовки markdown и эмодзи-спам (1–2 уместных эмодзи максимум).\n"
        "Верни только текст поста, без пояснений."
    )
    resp = await _get_client().chat.completions.create(
        model=settings.deepseek_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    )
    return (resp.choices[0].message.content or "").strip()
