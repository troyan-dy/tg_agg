# tg_agg — RSS-агрегатор для Telegram-каналов

Бот ведёт **несколько** Telegram-каналов на автопилоте. У каждого канала своя
RSS-лента, свой тон постов и своё расписание. По расписанию канала бот читает его
RSS, через **DeepSeek** выбирает самую актуальную новость, генерирует пост в тоне
канала и публикует. Управление и настройка — только через личный чат с ботом
(доступ у одного админа). Канал добавляется присланной ссылкой (бот резолвит чат и
проверяет, что он админ канала с правом публикации).

## Поток (pipeline)

`app/pipeline.py::run_once(bot, channel, ...)` — один прогон ОДНОГО канала:
1. Берёт `channel.rss_url`; пусто → `no_feed`.
2. `services/rss.fetch_entries` парсит ленту (feedparser в отдельном потоке), чистит HTML.
3. `storage.filter_unseen(channel.id, ...)` отбрасывает записи, уже виденные ЭТИМ
   каналом (дедуп в `seen_items` по составному ключу `(channel_id, entry_id)`).
   Свежих нет → **fallback** `storage.published_among(channel.id, ...)`: исключаем
   только опубликованное в этот канал, оставляя виденные-но-неопубликованные
   (хронология может ломаться — это ОК; ручной прогон почти всегда находит что постить).
4. `services/deepseek.pick_most_relevant` — JSON-режим, возвращает индекс лучшей
   новости. На вход также идут заголовки опубликованного каналом за сутки
   (`recent_published_titles(channel.id)`) + инструкция выбирать разнообразно по теме.
5. `services/deepseek.generate_post(chosen, get_preset(channel.tone))` — HTML-пост.
6. `bot.send_message` в `channel.chat_id`, затем
   `storage.mark_seen(channel.id, candidates, published_id=...)`.

**Дедуп — по-канальный.** Одна новость может выйти в разные каналы. В `seen_items`
помечаются ВСЕ записи, показанные DeepSeek (не только опубликованная) — нет повторов
и нет повторной траты токенов на те же заголовки. Если публикация упала — выбранная
запись НЕ помечается, чтобы повторить позже. `no_new` только когда вся лента канала
уже опубликована (см. fallback в п.3). `mark_seen` форсит флаг `published` отдельным
UPDATE — чтобы повторная публикация уже виденной записи (fallback-путь) корректно
записалась (один `on_conflict_do_nothing` оставил бы старую строку с `published=false`).

## Архитектура

- Один процесс (`app/main.py`): aiogram-бот (long polling) + APScheduler в фоне.
- `app/scheduler/worker.py` — **один ежечасный тик** (`CronTrigger(minute=0)` в
  `TIMEZONE`): берёт текущий час и прогоняет каждый enabled-канал с лентой, у кого
  этот час в `run_hours`. Расписание читается в момент тика → правка часов канала
  применяется сразу, без пересборки заданий (`reschedule` больше нет).
- `app/bot/handlers.py` — каналы (`/channels`, `/addchannel`) + настройки активного
  канала (`/setrss`, `/sethours`, `/settone`, `/run`, `/preview`, `/status`, ...).
  «Активный канал» хранится в БД; кнопки-настройки действуют на него. Весь роутер
  ограничен админом: `router.message.filter(F.from_user.id == settings.admin_id)`.
- `app/models.py` — `Channel` (канал + его rss_url/tone/run_hours/enabled),
  `SeenItem` (дедуп, PK `(channel_id, entry_id)`, FK на channels с ON DELETE CASCADE)
  и `Setting` (key/value, хранит id активного канала).
- `app/storage.py` — весь доступ к БД: CRUD каналов, выбор активного, по-канальный дедуп.
- `app/config.py` — pydantic-settings из `.env`.

## Команды

```bash
uv sync                          # зависимости (uv — единственный пакетный менеджер)
uv run python -m app.main        # запуск локально (нужен Postgres + .env); на старте сам гонит alembic upgrade head
uv add <pkg> / uv remove <pkg>   # менять зависимости (обновляет uv.lock)
docker compose up -d db          # только Postgres для локалки
docker compose up --build -d     # весь проект (db + bot) в Docker

# Миграции (Alembic). URL и metadata берутся из app.config/app.models (env.py),
# не из alembic.ini. После правки моделей:
uv run alembic revision --autogenerate -m "что изменилось"
uv run alembic upgrade head      # применить (то же делает app.main на старте)
uv run alembic downgrade -1      # откатить на шаг
uv run alembic check             # есть ли расхождение моделей и миграций (CI-проверка)
```

Быстрый офлайн-smoke-test (без сети/БД), как проверялось раньше:
```bash
BOT_TOKEN=123:abc ADMIN_ID=1 DEEPSEEK_API_KEY=sk-x \
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
- **Схему ведёт Alembic, не create_all.** `db.run_migrations()` (вызывается из
  `app.main` на старте) гонит `alembic upgrade head` в отдельном потоке (alembic
  синхронный, внутри `env.py` свой `asyncio.run`). `env.py` тянет
  `settings.database_url` и `Base.metadata` напрямую — `alembic.ini` без секретов.
  Сейчас одна ревизия `migrations/versions/0001_initial` (вся текущая схема,
  включая `require_media`) — деплой на чистую БД накатывает её сам. **Тесты**
  создают таблицы сами (`Base.metadata.create_all` в `conftest`), мимо Alembic.
  **Docker** копирует `migrations/` и `alembic.ini` (см. Dockerfile). При правке
  моделей — `alembic revision --autogenerate`, новая ревизия применится на старте.
- **Каналы — только из чата.** В `.env` больше нет `CHANNEL_ID`/`RSS_URL`. Канал
  добавляется присланной ссылкой; `add_channel` проверять `get_chat` + `get_chat_member`
  (статус admin/creator и `can_post_messages`). `RUN_HOURS`/`POST_TONE` в env — лишь
  ДЕФОЛТЫ для нового канала, дальше у каждого канала свои значения в БД.
- **Docker:** `DATABASE_URL` для сервиса `bot` переопределяется в compose на хост `db`
  (не `localhost`). `.env` обязателен для `docker compose` (env_file), создаётся из
  `.env.example`. `.env` в `.gitignore`.
- **Версии моделей Claude (на случай задач про LLM-инфру):** Opus 4.8 `claude-opus-4-8`,
  Sonnet 4.6 `claude-sonnet-4-6`, Haiku 4.5 `claude-haiku-4-5-20251001`, Fable 5 `claude-fable-5`.

## Эволюция (контекст решений)

Стартовали с веб-панели на FastAPI + много-канальность → пользователь упростил до
**одного канала, управления через чат, захардкоженного админа в env** → затем
переориентировали в **RSS→DeepSeek→автопостинг** → перевели на **uv** → добавили
**Docker Compose** для всего → постепенно вернули **по-канальные настройки**
(сначала тон/часы как настройки одного канала) → и наконец **снова много-канальность**
(несколько каналов, у каждого своя лента/тон/расписание, добавление по ссылке).
То есть прошлое упрощение до одного канала здесь сознательно развёрнуто обратно.
