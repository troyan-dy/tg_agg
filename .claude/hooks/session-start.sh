#!/bin/bash
# SessionStart-хук для облачных сессий Claude Code: ставит зависимости
# (включая dev-группу), чтобы сразу работали `make check` / pytest / ruff / mypy.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "export PATH=\"$HOME/.local/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
  fi
fi

# Идемпотентно: uv сам скачает Python из .python-version и доустановит только недостающее.
# Dev-группа включает ruff, mypy, pytest и wemake-python-styleguide (flake8).
uv sync --frozen

# Быстрая проверка, что линтеры на месте (сам прогон — `make check`).
uv run --frozen flake8 --version >/dev/null
uv run --frozen ruff --version >/dev/null
