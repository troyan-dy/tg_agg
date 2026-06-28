#!/usr/bin/env bash
#
# Авто-деплой готового образа. НЕ собирает на хосте: образ bot собирается и
# публикуется в GHCR в GitHub Actions. Здесь только:
#   1) тянем main (нужен лишь для свежего docker-compose.prod.yml и деплой-скриптов),
#   2) пуллим образы из реестра,
#   3) если compose-файл или образ изменились — пересоздаём контейнеры.
# Запускается из cron раз в несколько минут. Идемпотентен: нет изменений —
# тихо выходит, ничего не трогая.
#
set -euo pipefail

# Директория проекта = на уровень выше папки deploy/, где лежит этот скрипт.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

BRANCH="main"
IMAGE="ghcr.io/troyan-dy/tg_agg:${BOT_TAG:-latest}"
COMPOSE=(docker compose -f docker-compose.prod.yml)
LOG="$SCRIPT_DIR/auto-update.log"
LOCK="$SCRIPT_DIR/.auto-update.lock"

log() { echo "[$(date '+%F %T')] $*" >>"$LOG"; }

# Защита от наложения запусков: если предыдущий ещё идёт — выходим.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "previous run still in progress, skip"
  exit 0
fi

# --- 1. Свежий main (только для конфигов: compose-файл, .env.example, скрипты) ---
git fetch --quiet origin "$BRANCH"
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"
code_changed=false
if [ "$LOCAL" != "$REMOTE" ]; then
  code_changed=true
  log "repo update: ${LOCAL:0:7} -> ${REMOTE:0:7}"
  git pull --ff-only --quiet origin "$BRANCH"
fi

# --- 2. Пуллим образы. Сравниваем id образа bot до и после ---
before="$(docker images --no-trunc -q "$IMAGE" 2>/dev/null || true)"
if ! "${COMPOSE[@]}" pull --quiet >>"$LOG" 2>&1; then
  # Чаще всего — не залогинены в GHCR (приватный пакет). Лечится один раз:
  #   echo <PAT> | docker login ghcr.io -u <github-user> --password-stdin
  log "docker compose pull failed (GHCR login? network?) — skip"
  exit 1
fi
after="$(docker images --no-trunc -q "$IMAGE" 2>/dev/null || true)"
image_changed=false
[ "$before" != "$after" ] && image_changed=true

# --- 3. Пересоздаём контейнеры только если что-то изменилось ---
if [ "$code_changed" = false ] && [ "$image_changed" = false ]; then
  exit 0   # обычный случай: ничего нового, лог не засоряем
fi

log "applying changes (code=$code_changed image=$image_changed)..."
# up -d пересоздаёт только сервисы с изменившимся образом/конфигом; db не трогается.
"${COMPOSE[@]}" up -d >>"$LOG" 2>&1
log "done, now at $(git rev-parse --short HEAD)"

# Подчистить висячие образы прошлых версий, чтобы не копился мусор.
docker image prune -f >>"$LOG" 2>&1 || true
