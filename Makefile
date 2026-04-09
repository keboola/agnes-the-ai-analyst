# Agnes AI Data Analyst — Development Makefile

.PHONY: help test lint dev docker

help:
	@echo "Available targets:"
	@echo "  make test     Run test suite"
	@echo "  make dev      Start FastAPI dev server"
	@echo "  make docker   Build and start Docker Compose"
	@echo "  make lint     Run ruff linter (if installed)"

test:
	pytest tests/ -v --tb=short

dev:
	uvicorn app.main:app --reload

docker:
	docker compose up --build

lint:
	@ruff check . 2>/dev/null || echo "ruff not installed: pip install ruff"
