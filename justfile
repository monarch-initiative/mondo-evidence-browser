# Generated from monarch-project-copier, modernised for this project.
# Project-specific recipes live in project.justfile.

# Local settings (ORCID client id, service and data URLs) come from a gitignored
# .env; see .env.example.
set dotenv-load

_default:
    @just --list

# Install project dependencies (incl. dev tools) and the pre-commit hooks
[group('project management')]
install:
    uv sync --group dev
    npm ci
    uv run playwright install chromium
    uv run pre-commit install

# Run all checks: tests + doctests + browser smoke tests, type checking, lint and format
[group('checks')]
test: pytest mypy format

# Run tests and doctests
[group('checks')]
pytest:
    uv run pytest

# Run doctests only
[group('checks')]
doctest:
    uv run pytest src

[group('checks')]
mypy:
    uv run mypy src tests

# Lint and check formatting
[group('checks')]
format:
    uv run ruff check .
    uv run ruff format --check .

# Run every pre-commit hook on every file
[group('checks')]
lint-all:
    uv run pre-commit run --all-files

# Update from the project template
[group('project management')]
_update-template:
    uvx --with jinja2-time copier update --trust --skip-answered

import "project.justfile"
