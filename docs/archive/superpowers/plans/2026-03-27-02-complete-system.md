# Complete System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Make the new FastAPI system feature-complete with the old Flask system. Every route, every service function, every template — replicated with the new DuckDB-backed architecture.

**Status:** Infrastructure done (DuckDB, repos, FastAPI skeleton, CLI, Docker). Missing: business logic wiring, web UI, auth providers, 18 routes, 38 service functions.

---

## Part A: Wire sync trigger to DataSyncManager

Files:
- Modify: `app/api/sync.py` (replace stub with real sync)
- Modify: `app/main.py` (add instance config loading)

## Part B: Instance config integration

Files:
- Create: `app/instance_config.py` (load instance.yaml, expose to FastAPI)
- Modify: `app/main.py` (lifespan event loads config)
- Modify: `app/api/health.py` (include data source info)

## Part C: Web UI — Jinja2 templates in FastAPI

Files:
- Create: `app/web/router.py` (ALL web routes: /, /dashboard, /catalog, /login, /corporate-memory, /admin/tables, etc.)
- Copy: `webapp/templates/` → `app/web/templates/` (adapt for FastAPI)
- Copy: `webapp/static/` → `app/web/static/`
- Modify: `app/main.py` (mount templates + static)

## Part D: Auth providers (Google OAuth + Email + Password)

Files:
- Create: `app/auth/providers/google.py`
- Create: `app/auth/providers/email.py`
- Create: `app/auth/providers/password.py`
- Modify: `app/auth/router.py` (OAuth callback, magic link, password verify)

## Part E: Missing API endpoints (18 routes)

Files:
- Create: `app/api/catalog.py` (profile, metrics)
- Create: `app/api/telegram.py` (verify, unlink, status)
- Create: `app/api/desktop.py` (scripts, run)
- Create: `app/api/admin.py` (tables discover, registry CRUD)
- Modify: `app/api/memory.py` (add 10 admin governance endpoints)
- Modify: `app/api/sync.py` (add sync-settings, table-subscriptions)

## Part F: Service logic rewiring

Files:
- Rewrite all old service calls to use DuckDB repositories
- Bridge: old corporate_memory_service → KnowledgeRepository
- Bridge: old sync_settings_service → SyncSettingsRepository
- Bridge: old telegram_service → TelegramRepository

## Part G: CLI missing commands + old test fixes

Files:
- Create: `cli/commands/setup.py`
- Create: `cli/commands/server.py`
- Create: `cli/commands/explore.py`
- Fix: old tests to work with new code

## Part H: Full test coverage

- Integration tests for all 40 routes
- E2E Docker test
