.PHONY: setup check lint format test validate eval-test

export PYTHONDONTWRITEBYTECODE := 1

setup:
	uv sync --locked
	npm ci

check: lint test validate

# npm ci writes this file, so a fresh checkout or worktree installs the Markdown linter before its first lint.
node_modules/.package-lock.json: package-lock.json
	npm ci

lint: node_modules/.package-lock.json
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	npm run lint:markdown

format:
	uv run --locked ruff check --fix .
	uv run --locked ruff format .

test:
	uv run --locked python -B -m unittest discover -s tests

validate:
	uv run --locked python -B scripts/validate_release.py --source plugin --json

eval-test:
	uv run --locked python -B -m unittest discover -s eval/tests
