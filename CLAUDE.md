# tg_agg — RSS-агрегатор для Telegram-канала

Бот ведёт **один** Telegram-канал на автопилоте: несколько раз в день читает RSS,
через **DeepSeek** выбирает самую актуальную новость, генерирует пост и публикует.
Управление и настройка — только через личный чат с ботом (доступ у одного админа).

## Поток (pipeline)

`app/pipeline.py::run_once` — один прогон:
1. Берёт RSS-url из БД (`storage.get_rss_url`); нет url → `no_feed`.
2. `services/rss.fetch_entries` парсит ленту (feedparser в отдельном потоке), чистит HTML.
3. `storage.filter_unseen` отбрасывает записи, чьи id уже в `seen_items` (дедуп).
   Свежих нет → **fallback** `storage.published_among`: исключаем только
   опубликованное, оставляя виденные-но-неопубликованные (хронология может
   ломаться — это ОК; ручной прогон почти всегда находит что постить).
4. `services/deepseek.pick_most_relevant` — JSON-режим, возвращает индекс лучшей
   новости. На вход также идут заголовки опубликованного за сутки
   (`recent_published_titles`) + инструкция выбирать разнообразно по теме.
5. `services/deepseek.generate_post` — готовый HTML-пост для Telegram.
6. `bot.send_message` в канал, затем `storage.mark_seen(candidates, published_id=...)`.

**Дедуп:** в `seen_items` помечаются ВСЕ записи, показанные DeepSeek (не только
опубликованная) — нет повторов и нет повторной траты токенов на те же заголовки.
Если публикация упала — выбранная запись НЕ помечается, чтобы повторить позже.
`no_new` теперь только когда вся лента уже опубликована (см. fallback в п.3).
`mark_seen` форсит флаг `published` отдельным UPDATE — чтобы повторная публикация
уже виденной записи (fallback-путь) корректно записалась (один `on_conflict_do_nothing`
оставил бы старую строку с `published=false`).

## Архитектура

- Один процесс (`app/main.py`): aiogram-бот (long polling) + APScheduler в фоне.
- `app/scheduler/worker.py` — cron по `RUN_HOURS` (часы в `TIMEZONE`), несколько раз в день.
- `app/bot/handlers.py` — команды `/setrss`, `/rss`, `/run`, `/status`, `/help`.
  Весь роутер ограничен админом: `router.message.filter(F.from_user.id == settings.admin_id)`.
- `app/models.py` — `Setting` (key/value, хранит `rss_url`) и `SeenItem` (дедуп).
- `app/storage.py` — весь доступ к БД (rss-url, filter_unseen, mark_seen).
- `app/config.py` — pydantic-settings из `.env`.

## Команды

```bash
uv sync                          # зависимости (uv — единственный пакетный менеджер)
uv run python -m app.main        # запуск локально (нужен Postgres + .env)
uv add <pkg> / uv remove <pkg>   # менять зависимости (обновляет uv.lock)
docker compose up -d db          # только Postgres для локалки
docker compose up --build -d     # весь проект (db + bot) в Docker
```

Быстрый офлайн-smoke-test (без сети/БД), как проверялось раньше:
```bash
BOT_TOKEN=123:abc CHANNEL_ID=@t ADMIN_ID=1 DEEPSEEK_API_KEY=sk-x \
  DATABASE_URL='sqlite+aiosqlite:///:memory:' \
  uv run python -c "from app import main, pipeline, storage; from app.services import rss, deepseek; print('OK')"
```

## Подводные камни / договорённости

- **Только PostgreSQL в проде.** `storage.mark_seen` использует pg-specific upsert
  (`postgresql.insert ... on_conflict_do_nothing`). На SQLite этот путь упадёт —
  `aiosqlite` стоит лишь в dev-группе для импорт-тестов, не для реального прогона.
- **DeepSeek = OpenAI-совместимый API.** Используется `openai` SDK с
  `base_url=https://api.deepseek.com`, модель `deepseek-chat`. Это сознательный выбор
  пользователя — не заменять на Anthropic/другого провайдера без запроса.
- **Таблицы создаются через `db.init_db` (create_all), без Alembic.** Миграций нет —
  при изменении моделей это учитывать.
- **Docker:** `DATABASE_URL` для сервиса `bot` переопределяется в compose на хост `db`
  (не `localhost`). `.env` обязателен для `docker compose` (env_file), создаётся из
  `.env.example`. `.env` в `.gitignore`.
- **Версии моделей Claude (на случай задач про LLM-инфру):** Opus 4.8 `claude-opus-4-8`,
  Sonnet 4.6 `claude-sonnet-4-6`, Haiku 4.5 `claude-haiku-4-5-20251001`, Fable 5 `claude-fable-5`.

## Эволюция (контекст решений)

Стартовали с веб-панели на FastAPI + много-канальность → пользователь упростил до
**одного канала, управления через чат, захардкоженного админа в env** → затем
переориентировали в **RSS→DeepSeek→автопостинг** → перевели на **uv** → добавили
**Docker Compose** для всего. Тенденция пользователя: упрощать, убирать лишнее.
