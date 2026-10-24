.PHONY: check lint wps types test

# Все технические проверки одной командой (линтер, типы, тесты + покрытие).
check: lint wps types test

lint:
	uv run ruff check .

wps:
	uv run flake8 . --select=WPS

types:
	uv run mypy

test:
	uv run pytest -q
