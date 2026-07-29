.PHONY: help sync test lint fmt check

# Thin wrappers over the native uv/ruff/pytest/mypy commands. CI (.github/
# workflows/ci.yml) runs the same targets so local and CI cannot drift.

help:
	@echo "sync   Install dependencies (including dev and test extras)"
	@echo "test   Run the pytest suite"
	@echo "lint   Run ruff check, ruff format --check, and mypy"
	@echo "fmt    Reformat in place with ruff"
	@echo "check  Run test and lint"

sync:
	uv sync --dev --extra test

test:
	uv run --extra test pytest tests/

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy .

fmt:
	uv run ruff format .

check: test lint
