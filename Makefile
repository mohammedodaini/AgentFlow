# =============================================================================
# Developer entry points. `make <target>` is the universal "how do I run
# things?" answer — new teammates read this file first.
# =============================================================================

.PHONY: help up down install dev worker test lint format typecheck migrate makemigration check

help:            ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

up:              ## Start local infrastructure (Postgres + Redis)
	docker compose up -d

down:            ## Stop local infrastructure
	docker compose down

install:         ## Install backend dependencies into a virtualenv
	cd backend && uv sync --all-extras

dev:             ## Run the API with hot reload
	cd backend && uv run uvicorn app.main:app --reload --port 8000

worker:          ## Run the background task worker (arq)
	cd backend && uv run arq app.workers.settings.WorkerSettings

test:            ## Run the test suite with coverage
	cd backend && uv run pytest

eval:            ## Run the golden set — exits non-zero on a regression (M8)
	cd backend && uv run python -m app.evaluation

eval-baseline:   ## Accept this run's scores as the new baseline (read the report first)
	cd backend && uv run python -m app.evaluation --save-baseline

test-fast:       ## Unit tests only — no database, no Redis (sub-second)
	cd backend && uv run pytest -m unit --no-cov

test-pyramid:    ## Show the shape of the suite: unit vs integration vs e2e
	@cd backend && for layer in unit integration e2e; do \
		printf "%-12s " "$$layer"; \
		uv run pytest -m $$layer --collect-only -q --no-cov 2>/dev/null | tail -1; \
	done

lint:            ## Lint (no changes)
	cd backend && uv run ruff check .

format:          ## Auto-format and fix imports
	cd backend && uv run ruff format . && uv run ruff check --fix .

typecheck:       ## Static type checking
	cd backend && uv run mypy app

migrate:         ## Apply database migrations
	cd backend && uv run alembic upgrade head

makemigration:   ## Autogenerate a migration (usage: make makemigration m="add users")
	cd backend && uv run alembic revision --autogenerate -m "$(m)"

# --- Frontend (M13) -----------------------------------------------------------
# Separate targets rather than folding the frontend into `check`, because these
# need a different toolchain (pnpm, not uv) and `check` is what CI runs on every
# push. A backend-only change should not wait on a Next build.

web:             ## Run the frontend with hot reload (needs `make dev` too)
	cd frontend && pnpm dev

web-install:     ## Install frontend dependencies
	cd frontend && pnpm install

web-check:       ## Lint, typecheck and build the frontend
	cd frontend && pnpm lint && pnpm typecheck && pnpm build

smoke:           ## Drive the real UI in a browser — needs the whole stack running
	cd frontend && pnpm smoke

check: lint typecheck test  ## Everything CI runs — run before pushing
