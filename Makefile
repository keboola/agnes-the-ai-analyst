# Agnes AI Data Analyst — Development Makefile

LOCAL_DEV_COMPOSE := -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.local-dev.yml

.PHONY: help test lint dev docker local-dev local-dev-down local-dev-logs update-openapi-snapshot

help:
	@echo "Available targets:"
	@echo "  make test            Run test suite"
	@echo "  make dev             Start FastAPI dev server (native uvicorn)"
	@echo "  make docker          Build and start Docker Compose"
	@echo "  make local-dev       Start Agnes with LOCAL_DEV_MODE=1 (auth bypass, no .env needed)"
	@echo "  make local-dev-down  Stop and remove the local-dev stack"
	@echo "  make local-dev-logs  Tail logs from the local-dev stack"
	@echo "  make lint            Run ruff linter (if installed)"

test:
	pytest tests/ -v --tb=short

dev:
	uvicorn app.main:app --reload

docker:
	docker compose up --build

local-dev:
	./scripts/run-local-dev.sh

local-dev-down:
	docker compose $(LOCAL_DEV_COMPOSE) down

local-dev-logs:
	docker compose $(LOCAL_DEV_COMPOSE) logs -f

lint:
	@ruff check . 2>/dev/null || echo "ruff not installed: pip install ruff"

update-openapi-snapshot:
	TESTING=1 python scripts/generate_openapi.py > tests/snapshots/openapi.json
	@echo "Snapshot updated. Review diff and commit."
