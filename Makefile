.PHONY: help install dev infra app stop test lint format typecheck migrate clean

# Run binaries from the project venv directly. This sidesteps a bug where
# `uv run` errors on paths containing `:` (this dir has `(21-04-2026 15:16)`
# in its name). `uv sync` itself is unaffected.
VENV := .venv/bin

help:
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies via uv
	uv sync --all-extras

infra: ## Start postgres, mailpit, otel-collector, oidc-mock
	docker compose up -d --build postgres mailpit otel-collector oidc-mock

app: ## Run FastAPI with reload on :8001
	$(VENV)/uvicorn app.api.app:app --reload --host 0.0.0.0 --port 8001

worker: ## Run the orchestrator worker loop in the foreground
	$(VENV)/python -m app.orchestrator.worker

dev: infra app ## Start infra + run the app

stop: ## Stop docker services
	docker compose stop

test: ## Run the pytest suite
	$(VENV)/pytest

lint: ## Lint with ruff (check only)
	$(VENV)/ruff check .
	$(VENV)/ruff format --check .

format: ## Auto-format + auto-fix with ruff
	$(VENV)/ruff format .
	$(VENV)/ruff check --fix .

typecheck: ## Type-check with mypy
	$(VENV)/mypy app

migrate: ## Apply Alembic migrations (no-op until W2)
	$(VENV)/alembic upgrade head

clean: ## Stop docker services and remove caches
	docker compose down
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist *.egg-info
