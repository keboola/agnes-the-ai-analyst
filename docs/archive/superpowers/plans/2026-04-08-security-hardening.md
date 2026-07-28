# Security Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all critical/high security vulnerabilities found by audit before merging to main. Also update Python version, sync deps, fix Docker prod config.

**Architecture:** 7 independent fix tasks targeting specific vulnerabilities. Each is self-contained.

**Tech Stack:** Python 3.13, FastAPI, DuckDB, Docker

---

### Task 1: SQL Query Hardening

**Files:**
- Modify: `app/api/query.py`
- Modify: `src/db.py` (add read-only analytics getter)
- Test: existing `tests/test_security.py` should still pass

- [ ] **Step 1: Add read-only analytics DB connection**

In `src/db.py`, add after `get_analytics_db()`:

```python
def get_analytics_db_readonly() -> duckdb.DuckDBPyConnection:
    """Read-only connection to analytics DB. Blocks writes and external access."""
    db_path = _get_data_dir() / "analytics" / "server.duckdb"
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path), read_only=True)
    conn.execute("SET enable_external_access = false")
    return conn
```

- [ ] **Step 2: Harden query endpoint**

In `app/api/query.py`, replace `get_analytics_db()` with `get_analytics_db_readonly()`. Add to blocklist:

```python
blocked = [
    "drop ", "delete ", "insert ", "update ", "alter ", "create ",
    "copy ", "attach ", "detach ", "load ", "install ",
    "export ", "import ", "pragma ",
    "read_csv", "read_json", "read_parquet(", "read_text",
    "write_csv", "write_parquet",
    "read_blob", "glob(", "read_ndjson",
    ";",
    "'/", '"/",  # Block absolute file paths in FROM clause
]
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_security.py tests/test_e2e_api.py -v --tb=short
```

- [ ] **Step 4: Commit**

```bash
git commit -m "security: query endpoint — read-only DB + disable external access + block file paths"
```

---

### Task 2: Upload Path Traversal Fix

**Files:**
- Modify: `app/api/upload.py`

- [ ] **Step 1: Sanitize filenames**

Replace lines 30-31 and 47-48 with:

```python
# Sanitize filename — strip directory components to prevent traversal
raw_name = file.filename or f"session_{uuid.uuid4().hex[:8]}.jsonl"
filename = Path(raw_name).name  # strips ../../../ etc
if not filename or filename.startswith("."):
    filename = f"upload_{uuid.uuid4().hex[:8]}"
target = sessions_dir / filename
```

Same pattern for artifact upload.

- [ ] **Step 2: Commit**

```bash
git commit -m "security: sanitize upload filenames — prevent path traversal"
```

---

### Task 3: Script Sandbox Hardening

**Files:**
- Modify: `app/api/scripts.py`

- [ ] **Step 1: Add AST-based validation**

Add before the blocklist check:

```python
import ast

# AST-based validation — catches obfuscated imports
try:
    tree = ast.parse(source)
except SyntaxError as e:
    raise HTTPException(status_code=400, detail=f"Script syntax error: {e}")

for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.split(".")[0] in BLOCKED_MODULES:
                raise HTTPException(status_code=400, detail=f"Blocked import: {alias.name}")
    elif isinstance(node, ast.ImportFrom):
        if node.module and node.module.split(".")[0] in BLOCKED_MODULES:
            raise HTTPException(status_code=400, detail=f"Blocked import: {node.module}")
    elif isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_FUNCTIONS:
            raise HTTPException(status_code=400, detail=f"Blocked function: {node.func.id}")

BLOCKED_MODULES = {"os", "sys", "subprocess", "shutil", "ctypes", "importlib", "socket",
                   "requests", "urllib", "http", "signal", "pathlib", "builtins"}
BLOCKED_FUNCTIONS = {"exec", "eval", "compile", "open", "globals", "locals",
                     "getattr", "setattr", "delattr", "breakpoint", "__import__"}
```

Keep the string blocklist as a secondary check.

- [ ] **Step 2: Commit**

```bash
git commit -m "security: AST-based script validation — catches obfuscated imports"
```

---

### Task 4: Password Hashing + Auth Fixes

**Files:**
- Modify: `app/auth/providers/password.py` (remove SHA256 fallback)
- Modify: `app/auth/providers/google.py` (add secure cookie flag)
- Modify: `app/auth/dependencies.py` (fix get_optional_user bug)
- Modify: `app/auth/jwt.py` (add secret length validation)
- Modify: `pyproject.toml` (add missing deps)

- [ ] **Step 1: Remove SHA256 fallback in password.py**

Replace the try/except ImportError block with:

```python
from argon2 import PasswordHasher
ph = PasswordHasher()
hashed = ph.hash(request.password)
```

Remove all `except ImportError: import hashlib` blocks. Same in `app/auth/router.py` if present.

- [ ] **Step 2: Add secure flag to Google OAuth cookie**

In `app/auth/providers/google.py`, change set_cookie to:

```python
is_https = os.environ.get("HTTPS", "").lower() in ("1", "true") or request.url.scheme == "https"
response.set_cookie(
    key="access_token", value=jwt_token,
    httponly=True, max_age=86400 * 30, samesite="lax",
    secure=is_https,
)
```

- [ ] **Step 3: Fix get_optional_user argument bug**

In `app/auth/dependencies.py`, change line 68:

```python
# Before (wrong):
return await get_current_user(authorization, conn)
# After (correct):
return await get_current_user(request=None, authorization=authorization, conn=conn)
```

- [ ] **Step 4: Add JWT secret validation**

In `app/auth/jwt.py`, add after SECRET_KEY:

```python
if len(SECRET_KEY) < 32 and not os.environ.get("TESTING"):
    import warnings
    warnings.warn("JWT_SECRET_KEY is less than 32 characters — insecure for production", stacklevel=2)
```

- [ ] **Step 5: Sync pyproject.toml deps**

Add to pyproject.toml dependencies:

```toml
"authlib>=1.3.0",
"argon2-cffi>=23.1.0",
```

- [ ] **Step 6: Commit**

```bash
git commit -m "security: fix password hashing, OAuth cookie, JWT validation, optional_user bug"
```

---

### Task 5: CORS + Session Middleware

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Fix CORS**

Replace wildcard CORS with environment-configured origins:

```python
cors_origins = os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://localhost:8000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

- [ ] **Step 2: Fix SessionMiddleware secret**

```python
session_secret = os.environ.get("SESSION_SECRET", os.environ.get("JWT_SECRET_KEY", ""))
if not session_secret:
    import secrets
    session_secret = secrets.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=session_secret)
```

- [ ] **Step 3: Commit**

```bash
git commit -m "security: CORS from env config + session secret validation"
```

---

### Task 6: Docker Production Config

**Files:**
- Modify: `docker-compose.yml` (remove --reload)
- Modify: `Dockerfile` (upgrade Python)

- [ ] **Step 1: Remove --reload from docker-compose.yml**

```yaml
# Before:
command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
# After:
command: uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Create `docker-compose.override.yml` for dev:

```yaml
services:
  app:
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    volumes:
      - .:/app
```

- [ ] **Step 2: Upgrade Dockerfile to Python 3.13**

```dockerfile
FROM python:3.13-slim
```

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: Docker production config — no reload, Python 3.13"
```

---

### Task 7: Stale Docs Cleanup + datetime.utcnow Fix

**Files:**
- Modify: `CLAUDE.md` (fix test count)
- Delete or update: stale docs referencing Flask
- Modify: `app/api/jira_webhooks.py`, `src/profiler.py` (utcnow → now(UTC))

- [ ] **Step 1: Fix CLAUDE.md test count**

Update test count to current number.

- [ ] **Step 2: Delete stale docs**

```bash
rm docs/superpowers/plans/*.md  # agent plans, not needed in repo
```

- [ ] **Step 3: Fix datetime.utcnow deprecation**

Replace `datetime.utcnow()` with `datetime.now(timezone.utc)` in:
- `app/api/jira_webhooks.py`
- `src/profiler.py` (lines 1213, 1379)

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: fix stale docs, test count, datetime deprecation"
```

---

## Execution order

```
Task 1 (SQL)        ─┐
Task 2 (Upload)      ├─ 3 parallel agents
Task 3 (Scripts)    ─┘
Task 4 (Auth)       ─┐
Task 5 (CORS)        ├─ 3 parallel agents
Task 6 (Docker)     ─┘
Task 7 (Docs)       ── sequential after above
```

## Verification

```bash
# All tests pass
pytest tests/ --ignore=tests/test_cli.py -v

# Security spot checks
python3 -c "from app.api.query import *"  # no errors
curl -X POST http://localhost:8000/api/query -d '{"sql":"SELECT * FROM \"/etc/passwd\""}' # should fail

# Docker build
docker build -t test .
```
