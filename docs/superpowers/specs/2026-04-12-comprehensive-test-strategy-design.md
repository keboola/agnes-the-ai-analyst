# Comprehensive Test Strategy — AI Data Analyst

**Date:** 2026-04-12
**Approach:** Hybrid (gap analysis + critical journeys + parallel sub-agents)
**Goal:** Full test coverage across unit, integration, Docker E2E, and live layers — repeatable, parallelizable, non-blocking to development.

---

## 1. Test Taxonomy

| Layer | Marker | Runs in CI | What it tests | Isolation |
|-------|--------|-----------|---------------|-----------|
| **Unit** | _(none)_ | Every PR | Isolated functions, business logic, parsers, validators | `tmp_path`, mocks |
| **Integration** | `@pytest.mark.integration` | Every PR | FastAPI TestClient, repository+DuckDB, CLI with mock server | `tmp_path`, `seeded_app` fixture |
| **Docker E2E** | `@pytest.mark.docker` | Nightly | Full docker-compose stack, HTTP from outside | docker compose up/down |
| **Live** | `@pytest.mark.live` | Manual/weekly | Real Keboola, BigQuery, Jira credentials | Read-only against real sources |

### CI matrix

```yaml
# PR check (fast, <3 min)
pytest tests/ -x --timeout=60 -n auto  # unit + integration, parallel

# Nightly (docker, ~10 min)
pytest tests/ -m docker --timeout=120

# Weekly/manual (live, ~5 min)
pytest tests/ -m live --timeout=300
```

### Repeatability guarantees

- Every test uses `tmp_path` + `monkeypatch` — no global state leakage
- Faker factories use deterministic seeds — same data on every run
- Docker tests are idempotent — compose up → test → compose down, clean start
- Live tests are read-only — they never mutate real data sources
- CI uses pinned dependencies — no version drift between runs

---

## 2. Gap Analysis — Current vs. Target

| Module | Current tests | Gap | Priority |
|--------|--------------|-----|----------|
| WebSocket gateway | 0 | Auth, connection mgmt, heartbeat, multi-client | High |
| Corporate memory service | ~0 | Collector, hash detection, LLM mock, API CRUD+voting | High |
| Telegram bot | 1 integration | Storage, sender, dispatch, verify/unlink flow | Medium |
| Upload API | 0 | Upload limits, directory traversal protection, session/artifact upload | High |
| Scripts API | 0 | Deploy, run, undeploy, ad-hoc execution | High |
| Settings API | 0 | Get/update settings | Medium |
| Memory API | 0 | CRUD, voting, admin approve/reject/mandate | High |
| Access requests API | 0 | Request→approve→verify flow, deny flow | High |
| Permissions API | unit ok, API weak | Grant→query→revoke integration flow | Medium |
| Metadata API | weak | Get/save/push metadata | Medium |
| Admin configure API | weak | Configure flow, credential validation | High |
| Admin discover-and-register | weak | Discovery + registration in one call | Medium |
| CLI commands | 27 for ~15 cmds | Per-command coverage, error handling, output formats | High |
| Web UI routes | 11 | Auth redirects, dashboard render, setup wizard | Medium |
| Jira service | 2 | Incremental transform, webhook→rebuild pipeline | High |
| Scheduler edge cases | few | All parse_interval formats, is_table_due edge cases | Medium |

---

## 3. Critical E2E Journeys

Eight user flows tested end-to-end:

### J1: Bootstrap → Auth → Dashboard
- `da setup init` → `da setup bootstrap` (admin user)
- Password login → JWT token
- Google OAuth mock → callback → session
- GET /dashboard with valid session → 200
- GET /dashboard without session → redirect to /login

### J2: Table Registration → Sync → Query
- POST /api/admin/register-table (name, folder, sync_strategy)
- POST /api/sync/trigger → background sync with mock extractor
- Orchestrator rebuild → views created in analytics.duckdb
- POST /api/query `SELECT * FROM registered_table` → data returned
- GET /api/catalog/tables → table appears in catalog

### J3: Hybrid BQ + Local Query
- Register local table via sync
- POST /api/query/hybrid with register_bq → BQ subquery mocked + local join
- CLI: `da query --register-bq "alias=SELECT ..." --sql "SELECT ..."`
- CLI: stdin mode with JSON input
- Live variant: real BigQuery credentials

### J4: RBAC & Permissions
- Create admin + analyst users
- Admin grants permission on dataset → analyst can query
- Admin revokes → analyst gets 403
- Analyst creates access request → admin approves → analyst can query again
- Wildcard bucket permissions tested

### J5: Jira Webhook Pipeline
- POST /webhooks/jira with valid HMAC signature → 200
- POST /webhooks/jira with invalid signature → 401
- Verify incremental_transform called → parquet updated
- Verify rebuild_source("jira") called → views refreshed
- POST /api/query on Jira data → results returned

### J6: Corporate Memory Lifecycle
- POST /api/upload/local-md → CLAUDE.local.md stored
- Corporate memory collector runs (mocked LLM) → knowledge items created
- GET /api/memory → items listed with filtering
- POST /api/memory/{id}/vote → vote recorded
- POST /api/memory/admin/approve → status changed
- CLI sync picks up mandated items

### J7: Analyst Workflow
- `da analyst setup` → workspace created, data downloaded
- `da query --local "SELECT ..."` → local DuckDB query works
- POST /api/upload/sessions → session transcript stored
- POST /api/upload/artifacts → artifact stored
- `da analyst status` → freshness check passes

### J8: Multi-source Orchestration
- Create Keboola extract.duckdb (mock) + Jira extract.duckdb (mock) + BQ remote attach
- SyncOrchestrator.rebuild() → all sources attached
- Query across sources: `SELECT * FROM keboola_table UNION SELECT * FROM jira_issues`
- Verify _remote_attach extensions loaded correctly
- Live variant: real multi-source with actual credentials

---

## 4. Parallel Work Blocks (6 agents)

Each block writes to its own files — no conflicts. All blocks can run simultaneously.

### Block A: API Gaps (Agent 1)

**New test files:**
- `tests/test_upload_api.py` — session upload, artifact upload, 50MB limit, directory traversal reject, invalid content type
- `tests/test_scripts_api.py` — deploy script, run deployed, run ad-hoc, undeploy, invalid script
- `tests/test_settings_api.py` — get settings, update dataset settings, invalid input
- `tests/test_memory_api.py` — CRUD, pagination, search, filtering, voting, admin approve/reject/mandate/revoke
- `tests/test_access_requests_api.py` — create request, list my requests, pending (admin), approve, deny, duplicate request
- `tests/test_permissions_api.py` — grant, revoke, list per-user, list all, wildcard bucket, query enforcement
- `tests/test_metadata_api.py` — get metadata, save metadata, push to source (mock)
- `tests/test_admin_configure_api.py` — configure data source, credential validation, discover-and-register

**Estimated:** ~60-80 tests

### Block B: CLI Gaps (Agent 2)

**New test files:**
- `tests/test_cli_auth.py` — login, logout, whoami, token storage, invalid credentials
- `tests/test_cli_admin.py` — add-user, list-users, remove-user, register-table, discover-and-register, list-tables, metadata show/apply
- `tests/test_cli_sync.py` — sync (--table, --upload-only, --docs-only, --json), progress reporting
- `tests/test_cli_query.py` — query (--remote, --local, --hybrid, --limit, --format json/csv/table), error cases
- `tests/test_cli_analyst.py` — analyst setup, analyst status, freshness check
- `tests/test_cli_server.py` — server status, logs, restart, deploy, rollback, backup
- `tests/test_cli_diagnose.py` — diagnose output collection, error formatting
- `tests/test_cli_explore.py` — explore (--table, --limit, --json)
- `tests/test_cli_metrics.py` — metrics list, create, update, delete

**Testing pattern:** Each CLI test uses `CliRunner` (Typer) + `mock_http_server` fixture for API calls.

**Estimated:** ~40-50 tests

### Block C: Services (Agent 3)

**New test files:**
- `tests/test_ws_gateway.py` — connection lifecycle, JWT auth on connect, heartbeat timeout, multi-client per user, connection limit, message routing, disconnect cleanup
- `tests/test_telegram_bot.py` — /start flow, verification code generation, code verification, /help response, message dispatch, get_updates polling, callback query handling
- `tests/test_telegram_storage.py` — SQLite storage: create code, get chat_id, expiry, duplicate codes
- `tests/test_scheduler_full.py` — all parse_interval formats ("every 5m", "every 2h", "daily 05:00"), is_table_due with edge cases (never synced, just synced, overdue, future schedule), poll loop mock
- `tests/test_corporate_memory_collector.py` — MD5 hash change detection, full refresh trigger, LLM extraction mock, knowledge merge, vote/ID preservation, governance field preservation
- `tests/test_session_collector.py` — CLAUDE.local.md processing, session transcript parsing, artifact collection

**Testing pattern:** Services use mock sockets, mock HTTP clients, mock LLM responses. No real network.

**Estimated:** ~40-50 tests

### Block D: Connectors (Agent 4)

**New/expanded test files:**
- `tests/test_keboola_extractor_full.py` — DuckDB extension path, legacy client fallback, _meta creation, _remote_attach creation, multi-table extraction, error recovery, partial extraction
- `tests/test_bigquery_extractor_full.py` — remote-only extraction, _remote_attach table, BQ extension mock, credential handling, query timeout
- `tests/test_jira_service_full.py` — process_webhook_event (create/update/delete), trigger_incremental_transform, rebuild_source, concurrent webhook handling, malformed events
- `tests/test_jira_incremental.py` — monthly parquet update, issue insert/update/delete in parquet, concurrent file access (file_lock)
- `tests/test_llm_providers_full.py` — factory selection, OpenAI provider, Anthropic provider, retry logic, rate limit handling, structured output parsing

**Testing pattern:** Mock DuckDB extensions, mock API clients. Test the connector logic, not the external services.

**Estimated:** ~20-30 tests

### Block E: E2E Journeys (Agent 5)

**New test files:**
- `tests/test_journey_bootstrap_auth.py` — J1
- `tests/test_journey_sync_query.py` — J2
- `tests/test_journey_hybrid.py` — J3
- `tests/test_journey_rbac.py` — J4
- `tests/test_journey_jira.py` — J5
- `tests/test_journey_memory.py` — J6
- `tests/test_journey_analyst.py` — J7
- `tests/test_journey_multisource.py` — J8

**Testing pattern:** Each journey uses `seeded_app` fixture + `mock_extract_factory`. Multi-step flows with assertions at each stage. Marked `@pytest.mark.journey` for selective running.

**Estimated:** ~30-40 tests

### Block F: Docker & Live (Agent 6)

**New/expanded test files:**
- `tests/test_docker_full.py` — extend existing docker E2E: full bootstrap flow, sync trigger, query via HTTP, multi-service health (app + scheduler + ws-gateway), profile=full (telegram + corporate memory)
- `tests/test_live_keboola.py` — real Keboola extraction, table discovery, data validation (read-only)
- `tests/test_live_bigquery.py` — real BQ query, hybrid query with real BQ source (read-only)
- `tests/test_live_jira.py` — real Jira API read, webhook signature validation with real secret

**Testing pattern:** Docker tests use `docker compose up` with health wait. Live tests use env vars for credentials, skip if not set. All read-only.

**Estimated:** ~15-20 tests

---

## 5. Shared Test Infrastructure

Prepared before agents start — agents consume but don't modify these.

### `tests/conftest.py` (extend existing)

New fixtures:
- `mock_extract_factory(source_name, tables, query_mode)` — creates extract.duckdb with _meta, _remote_attach, and parquet data in tmp_path
- `mock_http_server(responses)` — lightweight HTTP server on random port, returns configured responses, for CLI tests
- `analyst_user(seeded_app)` — pre-created analyst user with limited permissions

### `tests/helpers/factories.py` (new)

Faker-based factories with deterministic seeds:
- `UserFactory` — email, name, role, hashed password
- `TableRegistryFactory` — name, source_type, bucket, source_table, query_mode, sync_schedule
- `KnowledgeItemFactory` — title, content, category, status
- `WebhookEventFactory` — Jira webhook payloads with valid/invalid HMAC

### `tests/helpers/assertions.py` (new)

- `assert_api_error(response, status, detail_contains)` — validate error response shape
- `assert_parquet_schema(path, expected_columns)` — validate parquet file structure
- `assert_extract_contract(extract_dir)` — validate extract.duckdb has _meta + correct schema
- `assert_duckdb_table_exists(db_path, table_name)` — check table in DuckDB

### `tests/helpers/mocks.py` (new)

- `MockKeboolaExtension` — simulates DuckDB Keboola extension behavior
- `MockBigQueryExtension` — simulates DuckDB BQ extension behavior
- `MockJiraWebhook(valid_signature=True)` — generates webhook payloads with correct HMAC
- `MockLLMProvider` — returns configured responses for corporate memory tests

### `tests/helpers/docker.py` (new)

- `wait_for_healthy(url, timeout=30)` — poll health endpoint until ready
- `docker_compose_up(profile="default")` — start services, return cleanup function
- `docker_exec(service, cmd)` — run command inside container

### pytest configuration

Add to `pytest.ini`:
```ini
markers =
    live: requires real credentials (deselected by default)
    docker: requires docker-compose (deselected by default)
    integration: FastAPI TestClient tests
    journey: end-to-end user flow tests
```

Add to `pyproject.toml` dev dependencies:
```
pytest-xdist>=3.0.0
```

---

## 6. Quality Gates & Review Checkpoints

### Per-agent review
After each agent completes its block, a code-review sub-agent verifies:
- All tests pass (`pytest <block_files> -v`)
- No test relies on global state or execution order
- Each test has a descriptive name and tests ONE thing
- Negative cases covered (auth failures, invalid input, missing data, edge cases)
- Assertions are specific (not just status code checks)
- No hardcoded paths, ports, or credentials
- Proper cleanup via fixtures

### Post-merge validation
After all 6 blocks are merged:
- Full suite run: `pytest tests/ -v --timeout=60`
- Parallel run: `pytest tests/ -n auto` — verify no ordering dependencies
- Docker run: `pytest tests/ -m docker`
- Check no test file naming collisions
- Verify total test count matches expectations (~210-270 new tests + ~204 existing)

### Ongoing
- PR CI runs unit + integration on every push
- Nightly CI adds docker tests
- Weekly manual run includes live tests
- Test count tracked — regressions flagged in PR review
