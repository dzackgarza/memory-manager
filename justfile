set fallback := true

install:
    uv sync --dev

lint:
    uv run ruff check .

format:
    uv run ruff format .

typecheck:
    uv run basedpyright

test:
    uv run pytest

check: lint typecheck test
