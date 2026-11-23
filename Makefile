# Development tasks for the Specivo plugin for Hermes Agent. See README.md.
#
# The tools and the supported Pythons are pinned in devtools/ (pyproject.toml and uv.lock). The
# repository root is the plugin directory and has no [project] table, so Hermes installs nothing
# for the plugin.

UV_RUN := uv run --project devtools

.PHONY: install test lint format hermes-validate

install:
	uv sync --project devtools

test:
	$(UV_RUN) pytest -n 4

lint:
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .
	$(UV_RUN) mypy

format:
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix .

# The Hermes Agent release the plugin is checked against: v0.23.3.
HERMES_COMMIT ?= caafe5389ae585ef77387a962aee05b1bcf17029
# Everything hermes-validate writes stays here: Hermes's checkout, a scratch HOME (the installer
# writes ~/.local/bin and shell rc files there, never in your own home) and a copy of the plugin.
CHECK_DIR := $(CURDIR)/.hermes-check

# Install Hermes with its own installer (it cannot be built as a wheel), then run the plugin
# catalog's checks against the files git would publish. Needs network.
hermes-validate:
	mkdir -p $(CHECK_DIR)/home $(CHECK_DIR)/plugin
	curl -fsSL "https://raw.githubusercontent.com/NousResearch/hermes-agent/$(HERMES_COMMIT)/scripts/install.sh" \
		-o $(CHECK_DIR)/install.sh
	test -x $(CHECK_DIR)/home/.local/bin/hermes || HOME=$(CHECK_DIR)/home bash $(CHECK_DIR)/install.sh \
		--commit $(HERMES_COMMIT) --dir $(CHECK_DIR)/src --hermes-home $(CHECK_DIR)/home/.hermes \
		--non-interactive --skip-browser --skip-computer-use < /dev/null
	git ls-files -co --exclude-standard -z | xargs -0 -I{} rsync -R "{}" $(CHECK_DIR)/plugin/
	HOME=$(CHECK_DIR)/home HERMES_HOME=$(CHECK_DIR)/check-home \
		$(CHECK_DIR)/home/.local/bin/hermes plugins validate $(CHECK_DIR)/plugin --install-deps < /dev/null
	HOME=$(CHECK_DIR)/home HERMES_HOME=$(CHECK_DIR)/check-home \
		$(CHECK_DIR)/home/.local/bin/hermes plugins doctor $(CHECK_DIR)/plugin --ci < /dev/null
