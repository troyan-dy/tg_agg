.PHONY: check lint types test

# Все технические проверки одной командой (линтер, типы, тесты + покрытие).
check: lint types test

lint:
	uv run ruff check .

types:
	uv run mypy

test:
	uv run pytest -q
