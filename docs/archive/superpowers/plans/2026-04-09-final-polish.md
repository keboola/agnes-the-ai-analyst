# Final Polish — Remaining P2 Fixes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clean up all remaining P2 issues from reviews + port 3 fixes from padak/tmp_oss + update stale docs.

**Architecture:** Small, independent fixes grouped by area. No architectural changes.

**Tech Stack:** Python 3.13, FastAPI, DuckDB, pytest

---

### Task 1: Fix argon2 error handling and imports (3 files)

Bare `except Exception` swallows non-auth errors. argon2 imported inside function body.

**Files:**
- Modify: `app/auth/router.py:51-56`
- Modify: `app/auth/providers/password.py`

**Changes:**

1. In `app/auth/router.py`, add at top:
```python
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
```

Replace lines 51-56 (the try/except inside the password_hash check):
```python
        try:
            ph = PasswordHasher()
            ph.verify(user["password_hash"], request.password)
        except VerifyMismatchError:
            raise HTTPException(status_code=401, detail="Invalid password")
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Password verification error for %s", request.email)
            raise HTTPException(status_code=500, detail="Authentication error")
```

2. In `app/auth/providers/password.py`, apply the same pattern — top-level import, catch `VerifyMismatchError` specifically.

3. Run: `pytest tests/test_auth_providers.py tests/test_security.py -v`
4. Commit: `fix: specific argon2 exception handling, top-level imports`

---

### Task 2: Fix duplicate import and raw SQL (2 files)

**Files:**
- Modify: `app/api/upload.py:7-8` — remove duplicate `from pathlib import Path as _Path`, use `Path` everywhere
- Modify: `app/api/memory.py:236` — route admin_edit through KnowledgeRepository instead of raw SQL

**Changes:**

1. In `upload.py`, remove line 8 (`from pathlib import Path as _Path`), replace all `_Path` usages with `Path`.

2. In `memory.py`, find the `admin_edit` function. Replace raw `conn.execute(f"UPDATE knowledge_items SET {set_clause}...")` with a call through the repository. Check if `KnowledgeRepository` has an `update` method; if not, add one.

3. Run: `pytest tests/test_api_complete.py -v`
4. Commit: `fix: remove duplicate import, route admin_edit through repository`

---

### Task 3: Fix Google OAuth connection management

**File:** `app/auth/providers/google.py:79-86`

Manual `get_system_db()` / `conn.close()` instead of using DI. If exception occurs between open and close, connection leaks.

**Changes:**

Wrap in try/finally:
```python
    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        # ... existing user lookup/creation logic ...
    finally:
        conn.close()
```

Run: `pytest tests/test_auth_providers.py -v`
Commit: `fix: wrap Google OAuth DB access in try/finally`

---

### Task 4: Add auth event audit logging

Login, token creation, bootstrap — no audit trail.

**File:** `app/auth/router.py`

**Changes:**

After successful token creation (line ~58) and bootstrap (line ~104), add:
```python
    from src.repositories.audit import AuditRepository
    try:
        audit_conn = get_system_db()
        AuditRepository(audit_conn).log(
            user_id=user["id"], action="token_created", resource="auth",
            params={"email": user["email"]},
        )
        audit_conn.close()
    except Exception:
        pass  # Audit failure should not block auth
```

Check what `AuditRepository.log()` signature looks like first — read `src/repositories/audit.py`.

Run: `pytest tests/test_auth_providers.py -v`
Commit: `feat: add audit logging for auth events (token, bootstrap)`

---

### Task 5: Port profiler union_by_name fix from padak

`src/profiler.py` crashes on partitioned tables when parquet files have schema evolution.

**File:** `src/profiler.py`

**Changes:**

Find all `read_parquet(` calls in profiler.py. Add `union_by_name=true` parameter. For example:
```python
# Before:
conn.execute(f"SELECT * FROM read_parquet('{path}')")
# After:
conn.execute(f"SELECT * FROM read_parquet('{path}', union_by_name=true)")
```

Run: `pytest tests/test_auto_profiling.py -v`
Commit: `fix: add union_by_name=true to profiler parquet reads (schema evolution support)`

---

### Task 6: Port strip_html fix for enricher from padak

`connectors/openmetadata/enricher.py` passes raw HTML to templates.

**File:** `connectors/openmetadata/enricher.py`

**Changes:**

1. Import strip_html from transformer:
```python
from connectors.openmetadata.transformer import strip_html
```

2. Find where table/column descriptions are set in `_parse_table_response()` or similar. Apply `strip_html()` to description fields before returning.

Run: `pytest tests/test_openmetadata_enricher.py -v`
Commit: `fix: strip HTML from catalog descriptions in enricher`

---

### Task 7: Update stale docs (3 files)

**Files:**
- `docs/CONFIGURATION.md` — references SendGrid, WEBAPP_SECRET_KEY, old patterns
- `dev_docs/disaster-recovery.md` — describes v1 architecture (systemd, nginx, /home)
- `dev_docs/server.md` — partially stale

**Changes:**

1. `docs/CONFIGURATION.md`: Read it, remove all Flask/SendGrid/WEBAPP_SECRET_KEY references. Update to match current env vars (JWT_SECRET_KEY, SESSION_SECRET). Reference `.env.template` for the full list.

2. `dev_docs/disaster-recovery.md`: Read it. If mostly v1, either rewrite for Docker-based backup (GCP disk snapshots + DuckDB export) or delete and add a brief backup section to DEPLOYMENT.md.

3. `dev_docs/server.md`: Read it. Remove rsync/SSH/systemd sections. Keep any still-relevant Docker deployment info.

Commit: `docs: update CONFIGURATION.md, disaster-recovery, server.md for v2 architecture`

---

## Execution Order

Tasks 1-6 are independent (different files). Task 7 is docs-only.

All can run in parallel.

**Verification:**
```bash
pytest tests/ -v --tb=short  # All 654+ tests pass
```
