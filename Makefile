# Development tasks for the Specivo plugin for Hermes Agent. See README.md.

.PHONY: install test lint format hermes-validate

install:
	uv sync

test:
	uv run pytest -n 4

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:
	uv run ruff format .
	uv run ruff check --fix .

# The Hermes Agent release the plugin is checked against.
HERMES_REF ?= 8da31006f3bcf70d0df05fb2b5a334eec4db2e07
# An empty HERMES_HOME for the checks, so they never touch your own Hermes setup.
CHECK_HOME ?= .hermes-check-home

# Run the plugin catalog's admission checks with the pinned Hermes (needs network).
hermes-validate:
	uv tool install --python 3.12 "hermes-agent @ git+https://github.com/NousResearch/hermes-agent@$(HERMES_REF)"
	HERMES_HOME=$(CURDIR)/$(CHECK_HOME) hermes plugins validate . --install-deps < /dev/null
	HERMES_HOME=$(CURDIR)/$(CHECK_HOME) hermes plugins doctor . --ci < /dev/null
