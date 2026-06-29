# Dead Code & Legacy Artifact Cleanup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove 17 dead files, fix broken Makefile, and clean up legacy artifacts that survived the v1→v2 migration.

**Architecture:** Pure deletion + one Makefile rewrite. No functional changes — only removing code/files that are never imported, referenced, or executed.

**Tech Stack:** git rm, pytest

**Source:** Deep audit of all tracked files with grep-verified zero-reference confirmation (2026-04-09).

---

### Task 1: Remove dead scripts

These scripts have zero references anywhere in the codebase.

**Files to delete:**
- `scripts/collect_session.py` — unused SessionEnd hook
- `scripts/generate_user_sync_configs.py` — replaced by DuckDB API
- `scripts/standalone_profiler.py` — replaced by `src/profiler.py`
- `scripts/remote_query.sh` — references non-existent module
- `scripts/update.sh` — calls `src.data_sync` and `docs/data_description.md` (both gone)
- `scripts/setup_views.sh` — depends on deleted `sync_data.sh`
- `scripts/test_sync.sh` — rsync diagnostics for resolved Issue #197
- `scripts/activate_venv.sh` — Docker uses direct venv paths
- `scripts/backfill_gap.sh` — one-time Jira backfill with hardcoded issue ranges
- `scripts/sync_config_template.yaml` — v1 sync config template

- [ ] **Step 1: Delete all dead scripts**

```bash
git rm scripts/collect_session.py \
      scripts/generate_user_sync_configs.py \
      scripts/standalone_profiler.py \
      scripts/remote_query.sh \
      scripts/update.sh \
      scripts/setup_views.sh \
      scripts/test_sync.sh \
      scripts/activate_venv.sh \
      scripts/backfill_gap.sh \
      scripts/sync_config_template.yaml
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/ -q --tb=short`
Expected: All 654 pass (none of these scripts are imported by tests)

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: remove 10 dead scripts from v1 architecture"
```

---

### Task 2: Remove legacy config example and root artifacts

**Files to delete:**
- `config/data_description.md.example` — v1 markdown-based table config, replaced by DuckDB `table_registry`
- `llms.txt` — describes v1 modules (`src/data_sync.py`, `webapp/app.py`, etc.) that don't exist

**Note on `data_description.md`:** `src/profiler.py` still references `docs/data_description.md` (line 95) but handles its absence gracefully (logs warning, skips). The `.example` file is just a template — removing it doesn't affect runtime.

- [ ] **Step 1: Delete files**

```bash
git rm config/data_description.md.example llms.txt
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/ -q --tb=short`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: remove legacy data_description.md.example and outdated llms.txt"
```

---

### Task 3: Remove completed planning docs from dev_docs

These are implementation plans for features that are done.

**Files to delete:**
- `dev_docs/plan-rsync-fix.md` — rsync fix (Issue #197, resolved)
- `dev_docs/plan_parquet_types_fix.md` — parquet type fix (Issues #185-187, resolved)
- `dev_docs/plan-corporate-memory.md` — corporate memory governance (fully implemented)

- [ ] **Step 1: Delete files**

```bash
git rm dev_docs/plan-rsync-fix.md \
      dev_docs/plan_parquet_types_fix.md \
      dev_docs/plan-corporate-memory.md
```

- [ ] **Step 2: Commit**

```bash
git commit -m "chore: remove completed planning docs (rsync fix, parquet types, corporate memory)"
```

---

### Task 4: Remove unused notification examples

`examples/notifications/` contains 3 Python scripts for a notification feature that was never built.

**Files to delete:**
- `examples/notifications/data_freshness.py`
- `examples/notifications/metric_report.py`
- `examples/notifications/revenue_drop.py`

- [ ] **Step 1: Check if examples/ has anything else**

```bash
git ls-files examples/
```

If only these 3 files, delete the entire directory.

- [ ] **Step 2: Delete**

```bash
git rm -r examples/
```

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: remove unused notification examples (feature not implemented)"
```

---

### Task 5: Fix or replace broken Makefile

The `validate-config` target imports `src.config.Config` which doesn't exist. The Makefile also references `.venv/bin/python` (not Docker).

**File:** `Makefile`

- [ ] **Step 1: Rewrite Makefile**

Replace entire content with a minimal, working version:

```makefile
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
```

- [ ] **Step 2: Run `make test`**

Run: `make test`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "fix: rewrite Makefile — remove broken validate-config, add working targets"
```

---

### Task 6: Update scripts/README.md

After deleting 10 scripts, the README should reflect what's left.

**File:** `scripts/README.md`

- [ ] **Step 1: Rewrite scripts/README.md**

```markdown
# Scripts

Utility and migration scripts for Agnes AI Data Analyst.

## Active Scripts

| Script | Purpose |
|--------|---------|
| `generate_sample_data.py` | Generate sample data for development/demo |
| `duckdb_manager.py` | DuckDB database management utilities |
| `init.sh` | Initial server setup (install deps, create dirs) |

## Migration Scripts (one-time use)

| Script | Purpose |
|--------|---------|
| `migrate_json_to_duckdb.py` | Migrate v1 JSON state files to DuckDB |
| `migrate_parquets_to_extracts.py` | Migrate v1 parquet layout to extract.duckdb |
| `migrate_registry_to_duckdb.py` | Migrate v1 table registry to DuckDB |
```

- [ ] **Step 2: Commit**

```bash
git add scripts/README.md
git commit -m "docs: update scripts/README.md after dead script cleanup"
```

---

## Execution Order

All tasks are independent except Task 6 (depends on Task 1).

Recommended: run sequentially (Task 1-6) for clean git history.

**Verification after all tasks:**

```bash
# Tests pass
pytest tests/ -v --tb=short

# No broken imports
python -c "from app.main import create_app; print('OK')"

# Makefile works
make test
```

## Summary

| Action | Files | Lines removed (est.) |
|--------|-------|---------------------|
| Dead scripts | 10 files | ~800 |
| Legacy config + llms.txt | 2 files | ~250 |
| Completed plans | 3 files | ~300 |
| Notification examples | 3 files | ~150 |
| Makefile rewrite | 1 file | ~60 (replaced) |
| scripts/README.md | 1 file | updated |
| **Total** | **19 files removed, 2 rewritten** | **~1,500 lines** |
