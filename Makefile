.PHONY: help setup install migrate test lint typecheck server worker viewer ping clean

PYTHON := .venv/bin/python
PIP    := .venv/bin/pip

help:
	@awk 'BEGIN{FS=":.*##"; printf "Targets:\n"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## one-shot bootstrap (Homebrew + venv + DB + alembic)
	./scripts/setup.sh

install: ## create venv and install Python deps
	test -d .venv || python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

migrate: ## apply alembic migrations
	.venv/bin/alembic upgrade head

test: ## run unit tests
	.venv/bin/pytest -q

lint: ## ruff check (autofix-safe)
	.venv/bin/ruff check src tests

format: ## ruff format
	.venv/bin/ruff format src tests

typecheck: ## mypy
	.venv/bin/mypy src

ping: ## verify Anthropic connectivity
	.venv/bin/jazz-guru ping

server: ## run FastAPI server
	.venv/bin/jazz-guru-server

worker: ## run RQ worker (needs Redis)
	.venv/bin/jazz-guru-worker

viewer: ## run trace viewer on :8765
	.venv/bin/jazz-guru viewer

tui: ## launch the Textual TUI client (server must be running)
	.venv/bin/jazz-guru tui

mic-devices: ## list available input audio devices
	.venv/bin/jazz-guru mic-devices

clean: ## remove venv + caches (does not touch DB)
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
