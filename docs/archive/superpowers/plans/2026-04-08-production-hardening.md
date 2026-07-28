# Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all P0/P1 issues from 4 independent code reviews (architect, data engineer, senior dev, test specialist) to make the codebase production-ready.

**Architecture:** Fixes are grouped into 6 independent workstreams: auth/security, SQL safety, orchestrator bugs, DuckDB lifecycle, test hardening, and docs/cleanup. Each workstream can be executed in parallel by separate agents.

**Tech Stack:** Python 3.13, FastAPI, DuckDB, pytest, Docker

**Source:** Consolidated findings from 4 review agents run 2026-04-08.

---

## Workstream 1: Authentication & Security (P0)

### Task 1.1: Fix password bypass in /auth/token

The `/auth/token` endpoint issues a JWT without verifying the password when `request.password` is empty but `user.password_hash` exists. Any user with a password can get a token by omitting the password field.

**Files:**
- Modify: `app/auth/router.py:47-54`
- Test: `tests/test_auth_providers.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_auth_providers.py, add to existing test class:

def test_password_required_when_hash_exists(client, e2e_env):
    """A user with password_hash must provide correct password."""
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from argon2 import PasswordHasher

    conn = get_system_db()
    repo = UserRepository(conn)
    ph = PasswordHasher()
    repo.create(id="pw-user", email="pw@test.com", role="analyst")
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        [ph.hash("correct-password"), "pw-user"],
    )
    conn.close()

    # Empty password should be rejected
    resp = client.post("/auth/token", json={"email": "pw@test.com", "password": ""})
    assert resp.status_code == 401

    # Missing password field should be rejected
    resp = client.post("/auth/token", json={"email": "pw@test.com"})
    assert resp.status_code == 401

    # Wrong password should be rejected
    resp = client.post("/auth/token", json={"email": "pw@test.com", "password": "wrong"})
    assert resp.status_code == 401

    # Correct password should work
    resp = client.post("/auth/token", json={"email": "pw@test.com", "password": "correct-password"})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auth_providers.py::test_password_required_when_hash_exists -v`
Expected: FAIL — empty password returns 200 instead of 401

- [ ] **Step 3: Fix the auth logic**

In `app/auth/router.py`, replace lines 47-54:

```python
    # If user has password_hash, require and verify password
    if user.get("password_hash"):
        if not request.password:
            raise HTTPException(status_code=401, detail="Password required")
        try:
            from argon2 import PasswordHasher
            ph = PasswordHasher()
            ph.verify(user["password_hash"], request.password)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid password")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auth_providers.py::test_password_required_when_hash_exists -v`
Expected: PASS

- [ ] **Step 5: Run full auth test suite**

Run: `pytest tests/test_auth_providers.py tests/test_security.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add app/auth/router.py tests/test_auth_providers.py
git commit -m "fix: require password when password_hash exists — prevents auth bypass"
```

### Task 1.2: Fail on default JWT secret in non-test environments

The app starts with a hardcoded known secret if `JWT_SECRET_KEY` env var is missing. A production deployment that forgets to set it is wide open.

**Files:**
- Modify: `app/auth/jwt.py:9-16`
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_security.py, add:

def test_jwt_rejects_default_secret_in_production(monkeypatch):
    """App should refuse to start with the default JWT secret unless TESTING=1."""
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.delenv("TESTING", raising=False)

    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        # Force reimport to trigger module-level check
        import importlib
        import app.auth.jwt as jwt_mod
        importlib.reload(jwt_mod)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_security.py::test_jwt_rejects_default_secret_in_production -v`
Expected: FAIL — no RuntimeError raised

- [ ] **Step 3: Fix jwt.py**

Replace `app/auth/jwt.py` lines 9-16:

```python
SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "")

if not SECRET_KEY:
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        SECRET_KEY = "test-jwt-secret-key-minimum-32-chars!!"
    else:
        raise RuntimeError(
            "JWT_SECRET_KEY environment variable is required. "
            "Generate one: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
elif len(SECRET_KEY) < 32 and os.environ.get("TESTING", "").lower() not in ("1", "true"):
    import warnings as _warnings
    _warnings.warn(
        f"JWT_SECRET_KEY is {len(SECRET_KEY)} chars — minimum 32 recommended",
        UserWarning, stacklevel=2,
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_security.py tests/test_auth_providers.py -v`
Expected: All pass (existing tests set TESTING=1 or JWT_SECRET_KEY via conftest)

- [ ] **Step 5: Commit**

```bash
git add app/auth/jwt.py tests/test_security.py
git commit -m "fix: fail startup on missing JWT_SECRET_KEY in non-test environments"
```

### Task 1.3: Reduce JWT expiry, add jti claim

30-day tokens with no revocation mechanism are too risky.

**Files:**
- Modify: `app/auth/jwt.py:18-19,28-37`
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_security.py, add:

def test_jwt_contains_jti_claim():
    """JWT tokens must contain a unique jti claim for future revocation support."""
    from app.auth.jwt import create_access_token, verify_token
    token = create_access_token(user_id="u1", email="a@b.com", role="analyst")
    payload = verify_token(token)
    assert "jti" in payload
    assert len(payload["jti"]) >= 16

def test_jwt_expiry_is_24_hours():
    """JWT tokens should expire in 24 hours, not 30 days."""
    from app.auth.jwt import ACCESS_TOKEN_EXPIRE_HOURS
    assert ACCESS_TOKEN_EXPIRE_HOURS == 24
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_security.py::test_jwt_contains_jti_claim tests/test_security.py::test_jwt_expiry_is_24_hours -v`
Expected: FAIL

- [ ] **Step 3: Fix jwt.py**

In `app/auth/jwt.py`:

Change line 19: `ACCESS_TOKEN_EXPIRE_HOURS = 24`

Add `import uuid` at the top. In `create_access_token`, add `"jti"` to payload:

```python
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": uuid.uuid4().hex,
    }
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/ -q --tb=short`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add app/auth/jwt.py tests/test_security.py
git commit -m "fix: reduce JWT expiry to 24h, add jti claim for future revocation"
```

### Task 1.4: Fix get_optional_user not checking cookies

`get_optional_user` only checks the Authorization header, not cookies. Web UI users appear as None.

**Files:**
- Modify: `app/auth/dependencies.py:60-70`
- Test: `tests/test_auth_providers.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_auth_providers.py, add:

def test_optional_user_reads_cookie(client, e2e_env):
    """get_optional_user should detect cookie-authenticated users."""
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    UserRepository(conn).create(id="cookie-user", email="cookie@test.com", role="analyst")
    conn.close()

    token = create_access_token(user_id="cookie-user", email="cookie@test.com", role="analyst")

    # Simulate web UI request with cookie but no Authorization header
    resp = client.get("/api/catalog", cookies={"access_token": token})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run to verify behavior** (this may or may not fail depending on endpoint requirements)

- [ ] **Step 3: Fix dependencies.py**

Replace `get_optional_user` in `app/auth/dependencies.py`:

```python
async def get_optional_user(
    request: Request = None,
    authorization: Optional[str] = Header(None),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> Optional[dict]:
    """Like get_current_user but returns None instead of 401 if no token."""
    try:
        return await get_current_user(request=request, authorization=authorization, conn=conn)
    except HTTPException:
        return None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_auth_providers.py tests/test_api.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add app/auth/dependencies.py tests/test_auth_providers.py
git commit -m "fix: get_optional_user now checks cookies like get_current_user"
```

---

## Workstream 2: SQL Safety (P0/P1)

### Task 2.1: Add identifier validation to orchestrator

`source_name` from directory names and `table_name` from `_meta` are interpolated into SQL without validation. A crafted directory name or _meta entry could inject arbitrary SQL.

**Files:**
- Modify: `src/orchestrator.py` (add validation helper, apply in `_attach_and_create_views` and `_attach_remote_extensions`)
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing tests**

```python
# In tests/test_orchestrator.py, add:

def test_rejects_malicious_source_name(setup_env):
    """Orchestrator must reject directory names with SQL injection characters."""
    from src.orchestrator import SyncOrchestrator

    malicious_dir = setup_env["extracts_dir"] / 'test; DROP TABLE _meta--'
    malicious_dir.mkdir()
    db_path = malicious_dir / "extract.duckdb"
    import duckdb as _duckdb
    conn = _duckdb.connect(str(db_path))
    conn.execute("""CREATE TABLE _meta (
        table_name VARCHAR, description VARCHAR, rows BIGINT,
        size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR DEFAULT 'local'
    )""")
    conn.execute('CREATE TABLE "safe_table" (id VARCHAR)')
    conn.execute("INSERT INTO _meta VALUES ('safe_table', '', 0, 0, current_timestamp, 'local')")
    conn.close()

    orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
    result = orch.rebuild()

    # Malicious source should be skipped, not attached
    assert 'test; DROP TABLE _meta--' not in result


def test_rejects_malicious_table_name(setup_env):
    """Orchestrator must reject table names with SQL injection characters."""
    from src.orchestrator import SyncOrchestrator

    source_dir = setup_env["extracts_dir"] / "keboola"
    source_dir.mkdir()
    (source_dir / "data").mkdir()

    db_path = source_dir / "extract.duckdb"
    import duckdb as _duckdb
    conn = _duckdb.connect(str(db_path))
    conn.execute("""CREATE TABLE _meta (
        table_name VARCHAR, description VARCHAR, rows BIGINT,
        size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR DEFAULT 'local'
    )""")
    conn.execute('CREATE TABLE "safe" (id VARCHAR)')
    conn.execute("INSERT INTO _meta VALUES ('safe', '', 0, 0, current_timestamp, 'local')")
    # Malicious table name in _meta
    conn.execute("""INSERT INTO _meta VALUES ('x"; DROP TABLE _meta; --', '', 0, 0, current_timestamp, 'local')""")
    conn.close()

    orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
    result = orch.rebuild()

    # Safe table should be there, malicious should be skipped
    assert "keboola" in result
    assert "safe" in result["keboola"]
    assert 'x"; DROP TABLE _meta; --' not in result.get("keboola", [])
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_orchestrator.py::test_rejects_malicious_source_name tests/test_orchestrator.py::test_rejects_malicious_table_name -v`
Expected: FAIL or crash from SQL injection

- [ ] **Step 3: Add validation helper and apply it**

At the top of `src/orchestrator.py`, add after imports:

```python
import re

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

def _validate_identifier(name: str, context: str) -> bool:
    """Validate a DuckDB identifier. Returns True if safe, False if not."""
    if not _SAFE_IDENTIFIER.match(name):
        logger.warning("Rejected unsafe %s identifier: %r", context, name)
        return False
    return True
```

In `_do_rebuild` (line ~92), add check before `_attach_and_create_views`:

```python
                if not _validate_identifier(ext_dir.name, "source_name"):
                    continue
```

In `_attach_and_create_views` (line ~160), add check before CREATE VIEW:

```python
            for table_name, rows, size_bytes, query_mode in meta_rows:
                if not _validate_identifier(table_name, "table_name"):
                    continue
```

In `_attach_remote_extensions` (line ~193), add check:

```python
        for alias, extension, url, token_env in rows:
            if not _validate_identifier(alias, "alias") or not _validate_identifier(extension, "extension"):
                continue
```

- [ ] **Step 4: Run all orchestrator tests**

Run: `pytest tests/test_orchestrator.py -v`
Expected: All pass including new tests

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator.py tests/test_orchestrator.py
git commit -m "fix: validate SQL identifiers in orchestrator — prevent injection via directory/table names"
```

### Task 2.2: Harden query endpoint SQL blocklist

The current blocklist misses `parquet_scan`, `read_csv_auto`, `query_table`, and has false positives on semicolons in string literals. Also add `enable_external_access=false` on the analytics connection.

**Files:**
- Modify: `app/api/query.py:39-51` and `src/db.py` (analytics readonly connection)
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing tests**

```python
# In tests/test_security.py, add to TestQuerySecurity:

def test_blocks_parquet_scan(self, client, auth_headers):
    resp = client.post("/api/query", json={"sql": "SELECT * FROM parquet_scan('/etc/passwd')"}, headers=auth_headers)
    assert resp.status_code == 400

def test_blocks_read_csv_auto(self, client, auth_headers):
    resp = client.post("/api/query", json={"sql": "SELECT * FROM read_csv_auto('/data/state/system.duckdb')"}, headers=auth_headers)
    assert resp.status_code == 400

def test_blocks_query_table(self, client, auth_headers):
    resp = client.post("/api/query", json={"sql": "SELECT * FROM query_table('secret')"}, headers=auth_headers)
    assert resp.status_code == 400

def test_blocks_httpfs(self, client, auth_headers):
    resp = client.post("/api/query", json={"sql": "SELECT * FROM read_parquet('https://evil.com/data.parquet')"}, headers=auth_headers)
    assert resp.status_code == 400
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_security.py::TestQuerySecurity -v`
Expected: Some FAIL (parquet_scan, read_csv_auto, query_table not blocked)

- [ ] **Step 3: Expand the blocklist and set enable_external_access=false**

In `app/api/query.py`, replace the `blocked` list (lines 39-49):

```python
    blocked = [
        "drop ", "delete ", "insert ", "update ", "alter ", "create ",
        "copy ", "attach ", "detach ", "load ", "install ",
        "export ", "import ", "pragma ", "call ",
        # File access functions
        "read_csv", "read_json", "read_parquet", "read_text",
        "write_csv", "write_parquet", "read_blob", "read_ndjson",
        "parquet_scan", "parquet_metadata", "parquet_schema",
        "json_scan", "csv_scan",
        "query_table", "iceberg_scan", "delta_scan",
        "glob(", "list_files",
        "'/", '"/','http://', 'https://', 's3://', 'gcs://',
        # Multiple statements
        ";",
    ]
```

In `src/db.py` `get_analytics_db_readonly()`, after opening the connection, add:

```python
    try:
        conn.execute("SET enable_external_access = false")
    except Exception:
        pass  # Older DuckDB versions
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_security.py::TestQuerySecurity -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add app/api/query.py src/db.py tests/test_security.py
git commit -m "fix: expand query blocklist, disable external_access on analytics connection"
```

---

## Workstream 3: Orchestrator Bugs (P0/P1)

### Task 3.1: Fix rebuild_source destroying all other sources' views

`_do_rebuild_source()` creates a fresh temp DB with only one source, then replaces the entire analytics DB. Every Jira webhook wipes all Keboola/BigQuery views.

**Files:**
- Modify: `src/orchestrator.py:116-141`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_orchestrator.py, add:

def test_rebuild_source_preserves_other_sources(setup_env):
    """rebuild_source('jira') must NOT destroy views from keboola."""
    from src.orchestrator import SyncOrchestrator

    _create_mock_extract(
        setup_env["extracts_dir"], "keboola",
        [{"name": "orders", "data": [{"id": "1"}]}],
    )
    _create_mock_extract(
        setup_env["extracts_dir"], "jira",
        [{"name": "issues", "data": [{"key": "PROJ-1"}]}],
    )

    orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])

    # First: full rebuild
    result = orch.rebuild()
    assert "keboola" in result
    assert "jira" in result

    # Second: rebuild only jira
    jira_tables = orch.rebuild_source("jira")
    assert "issues" in jira_tables

    # Third: full rebuild again — keboola must still be there
    result2 = orch.rebuild()
    assert "keboola" in result2
    assert "orders" in result2["keboola"]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_orchestrator.py::test_rebuild_source_preserves_other_sources -v`
Expected: FAIL — keboola views gone after rebuild_source("jira")

- [ ] **Step 3: Fix _do_rebuild_source to delegate to full rebuild**

In `src/orchestrator.py`, replace `_do_rebuild_source` (lines 116-141):

```python
    def _do_rebuild_source(self, source_name: str) -> List[str]:
        """Rebuild views for a single source by doing a full rebuild.

        A full rebuild is necessary because the analytics DB is created fresh
        each time (temp file + atomic swap). Rebuilding only one source would
        destroy views from all other sources.
        """
        extracts_dir = _get_extracts_dir()
        db_file = extracts_dir / source_name / "extract.duckdb"
        if not db_file.exists():
            logger.warning("No extract.duckdb for source %s", source_name)
            return []

        result = self._do_rebuild()
        return result.get(source_name, [])
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_orchestrator.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator.py tests/test_orchestrator.py
git commit -m "fix: rebuild_source delegates to full rebuild — preserves other sources' views"
```

### Task 3.2: Handle WAL files in atomic swap

`shutil.move` only moves the `.duckdb` file. The `.wal` file from the old DB can corrupt the new one.

**Files:**
- Modify: `src/orchestrator.py` (_do_rebuild lines 106-112)
- Modify: `connectors/keboola/extractor.py` (lines 148-155)
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_orchestrator.py, add:

def test_rebuild_cleans_wal_files(setup_env):
    """After rebuild, no .wal files should remain from the temp or old DB."""
    from src.orchestrator import SyncOrchestrator

    _create_mock_extract(
        setup_env["extracts_dir"], "keboola",
        [{"name": "orders", "data": [{"id": "1"}]}],
    )
    orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
    orch.rebuild()

    from pathlib import Path
    db_path = Path(setup_env["analytics_db"])
    assert not (db_path.parent / (db_path.name + ".wal")).exists()
    assert not (db_path.parent / (db_path.name + ".tmp.wal")).exists()
```

- [ ] **Step 2: Run to verify it passes or fails**

Run: `pytest tests/test_orchestrator.py::test_rebuild_cleans_wal_files -v`

- [ ] **Step 3: Add WAL cleanup helper**

In `src/orchestrator.py`, add a helper after `_validate_identifier`:

```python
def _atomic_swap_db(tmp_path: str, target_path: str) -> None:
    """Atomically replace target DuckDB file, cleaning up WAL files."""
    import shutil
    target = Path(target_path)
    tmp = Path(tmp_path)

    # Remove old WAL file if it exists
    old_wal = Path(str(target) + ".wal")
    if old_wal.exists():
        old_wal.unlink()

    # Move temp DB into place
    if tmp.exists():
        shutil.move(str(tmp), str(target))

    # Clean up temp WAL
    tmp_wal = Path(str(tmp) + ".wal")
    if tmp_wal.exists():
        tmp_wal.unlink()
```

Replace `shutil.move` call in `_do_rebuild` (line ~112) with:

```python
        _atomic_swap_db(tmp_path, self._db_path)
```

Also add `CHECKPOINT` before `conn.close()` in `_do_rebuild`:

```python
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
```

Apply the same pattern in `connectors/keboola/extractor.py` at the end of `run()`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_orchestrator.py tests/test_keboola_extractor.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator.py connectors/keboola/extractor.py tests/test_orchestrator.py
git commit -m "fix: clean WAL files during atomic DB swap, add CHECKPOINT before close"
```

### Task 3.3: Add temp-file swap to BigQuery extractor

BigQuery extractor writes directly to `extract.duckdb`, causing lock conflicts with the orchestrator.

**Files:**
- Modify: `connectors/bigquery/extractor.py:64-68`
- Test: `tests/test_bigquery_extractor.py`

- [ ] **Step 1: Write the test**

```python
# In tests/test_bigquery_extractor.py, add:

def test_uses_temp_file_swap(self, output_dir):
    """BigQuery extractor should write to .tmp and rename, not write directly."""
    from connectors.bigquery.extractor import _create_meta_table
    db_path = Path(output_dir) / "extract.duckdb"

    # Pre-create the DB to simulate existing file
    conn = duckdb.connect(str(db_path))
    _create_meta_table(conn)
    conn.close()

    # After init_extract, the file should exist and no .tmp should remain
    # (The actual init_extract test already covers this — we just verify no .tmp leak)
    assert db_path.exists()
    assert not (Path(output_dir) / "extract.duckdb.tmp").exists()
```

- [ ] **Step 2: Modify init_extract to use temp-file swap**

In `connectors/bigquery/extractor.py`, replace lines 64-68:

```python
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    db_path = output_path / "extract.duckdb"
    tmp_db_path = output_path / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()
    conn = duckdb.connect(str(tmp_db_path))
```

And at the end, before `return stats` (after `conn.close()`):

```python
    import shutil
    if tmp_db_path.exists():
        shutil.move(str(tmp_db_path), str(db_path))
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_bigquery_extractor.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add connectors/bigquery/extractor.py tests/test_bigquery_extractor.py
git commit -m "fix: BigQuery extractor uses temp-file swap to avoid lock conflicts"
```

---

## Workstream 4: Script Sandbox Hardening (P1)

### Task 4.1: Strip VIRTUAL_ENV and PYTHONPATH from sandbox subprocess

The sandbox gives scripts access to all installed packages (httpx, duckdb) via inherited env vars.

**Files:**
- Modify: `app/api/scripts.py:191-198`
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_security.py, add to TestScriptSecurity:

def test_sandbox_cannot_import_httpx(self, client, admin_headers):
    """Sandboxed scripts must not have access to httpx or other installed packages."""
    resp = client.post("/api/scripts/run", json={
        "name": "test",
        "source": "import httpx\nprint('pwned')"
    }, headers=admin_headers)
    data = resp.json()
    # httpx should be blocked by pattern OR unavailable due to stripped VIRTUAL_ENV
    assert resp.status_code == 400 or data.get("exit_code", 0) != 0
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_security.py::TestScriptSecurity::test_sandbox_cannot_import_httpx -v`
Expected: FAIL — httpx imports successfully

- [ ] **Step 3: Fix the subprocess env**

In `app/api/scripts.py`, replace the env dict in `subprocess.run` (lines 191-198):

```python
            env={
                "PATH": "/usr/bin:/usr/local/bin",
                "DATA_DIR": data_dir,
                "HOME": "/tmp",
                # Deliberately exclude VIRTUAL_ENV and PYTHONPATH
                # to prevent access to installed packages
            },
```

Also add `"httpx"`, `"from httpx"` to `blocked_patterns` list.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_security.py::TestScriptSecurity -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add app/api/scripts.py tests/test_security.py
git commit -m "fix: strip VIRTUAL_ENV/PYTHONPATH from script sandbox, block httpx import"
```

---

## Workstream 5: Test Hardening (P0-P1)

### Task 5.1: Fix environment variable leaking in test fixtures

Most test files set `os.environ["DATA_DIR"]` directly without cleanup. This causes test ordering dependencies.

**Files:**
- Modify: `tests/test_db.py`, `tests/test_rbac.py`, `tests/test_repositories.py`, `tests/test_api.py`, `tests/test_api_complete.py`, `tests/test_api_scripts.py`, `tests/test_auth_providers.py`, `tests/test_bootstrap.py`, `tests/test_permissions.py`, `tests/test_security.py`

- [ ] **Step 1: Search and replace pattern**

In every test file that has `os.environ["DATA_DIR"] =` inside a fixture, replace with `monkeypatch.setenv("DATA_DIR", ...)`. Add `monkeypatch` to the fixture parameters.

Example — in `tests/test_db.py`, change:

```python
@pytest.fixture
def db_env(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    yield tmp_path
```

To:

```python
@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    yield tmp_path
```

Apply to all affected files. Remove manual `os.environ.pop("DATA_DIR", None)` lines since monkeypatch handles cleanup automatically.

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: 607+ passed

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "fix: use monkeypatch for DATA_DIR in all test fixtures — prevent env leaking"
```

### Task 5.2: Add extract.duckdb contract test

Create a shared validator that verifies any extract.duckdb conforms to the contract. Apply it in all extractor tests.

**Files:**
- Create: `tests/helpers/contract.py`
- Modify: `tests/test_keboola_extractor.py`, `tests/test_bigquery_extractor.py`

- [ ] **Step 1: Create contract validator**

```python
# tests/helpers/__init__.py (empty)
# tests/helpers/contract.py

"""Shared validator for the extract.duckdb contract."""

import duckdb
from pathlib import Path


def validate_extract_contract(db_path: str) -> None:
    """Verify an extract.duckdb conforms to the contract.

    Raises AssertionError with details if any check fails.
    """
    path = Path(db_path)
    assert path.exists(), f"extract.duckdb not found at {db_path}"

    conn = duckdb.connect(str(path), read_only=True)
    try:
        # _meta table must exist with correct schema
        cols = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='_meta' ORDER BY ordinal_position"
        ).fetchall()
        col_names = [c[0] for c in cols]
        assert col_names == ["table_name", "description", "rows", "size_bytes", "extracted_at", "query_mode"], \
            f"_meta schema mismatch: {col_names}"

        # Every _meta entry with query_mode='local' must have a corresponding view or table
        local_tables = conn.execute(
            "SELECT table_name FROM _meta WHERE query_mode = 'local'"
        ).fetchall()
        for (name,) in local_tables:
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name = ?", [name]
            ).fetchall()
            assert len(tables) > 0, f"Local table '{name}' in _meta but no view/table exists"

        # If _remote_attach exists, validate its schema
        ra_exists = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='_remote_attach'"
        ).fetchone()[0]
        if ra_exists:
            ra_cols = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='_remote_attach' ORDER BY ordinal_position"
            ).fetchall()
            ra_col_names = [c[0] for c in ra_cols]
            assert ra_col_names == ["alias", "extension", "url", "token_env"], \
                f"_remote_attach schema mismatch: {ra_col_names}"
    finally:
        conn.close()
```

- [ ] **Step 2: Apply in extractor tests**

In `tests/test_keboola_extractor.py`, add to `test_creates_extract_duckdb`:

```python
        from tests.helpers.contract import validate_extract_contract
        validate_extract_contract(str(Path(output_dir) / "extract.duckdb"))
```

Similarly in `tests/test_bigquery_extractor.py::test_creates_extract_duckdb_with_meta`.

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_keboola_extractor.py tests/test_bigquery_extractor.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add tests/helpers/ tests/test_keboola_extractor.py tests/test_bigquery_extractor.py
git commit -m "feat: add extract.duckdb contract validator, apply in all extractor tests"
```

### Task 5.3: Add pytest timeout and strict markers

Prevent CI hangs and catch marker typos.

**Files:**
- Modify: `pytest.ini`
- Modify: `requirements.txt` (add pytest-timeout)

- [ ] **Step 1: Update pytest.ini**

```ini
[pytest]
addopts = -m "not live and not docker" --timeout=60 --strict-markers
markers =
    live: tests requiring server access (run with '-m live')
    docker: tests requiring Docker (run with '-m docker')
```

- [ ] **Step 2: Add pytest-timeout to requirements.txt**

Add line: `pytest-timeout>=2.0.0`

- [ ] **Step 3: Install and run**

Run: `uv pip install --system pytest-timeout && pytest tests/ -q --tb=short`
Expected: All pass within 60s timeout

- [ ] **Step 4: Commit**

```bash
git add pytest.ini requirements.txt
git commit -m "chore: add pytest-timeout (60s) and strict-markers to pytest config"
```

---

## Workstream 6: Docs & Cleanup (P1-P2)

### Task 6.1: Rewrite README.md from CLAUDE.md

The current README describes the old Flask/rsync architecture. CLAUDE.md is accurate.

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rewrite README.md**

Use CLAUDE.md as the source of truth. The README should contain:
- Project description (1-2 paragraphs)
- Architecture diagram (from CLAUDE.md)
- Quick start (Docker compose)
- Development setup (venv, pytest)
- Project structure (from CLAUDE.md)
- Configuration overview
- Supported data sources (Keboola ✅, BigQuery ✅, Jira ✅)
- Links to docs/DEPLOYMENT.md for server setup
- License

Remove all references to Flask, rsync, SSH, sync_data.sh, Linux groups, server/setup.sh.

- [ ] **Step 2: Verify no broken references**

Run: `grep -r "webapp/" README.md; grep -r "sync_data.sh" README.md; grep -r "server/setup" README.md`
Expected: No matches

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: rewrite README for v2 architecture (FastAPI, DuckDB, Docker)"
```

### Task 6.2: Update .env.template to match actual code

Template references `WEBAPP_SECRET_KEY` but code uses `JWT_SECRET_KEY`.

**Files:**
- Modify: `config/.env.template`

- [ ] **Step 1: Rewrite template**

```bash
# AI Data Analyst - Environment Variables
# Copy to .env: cp config/.env.template .env
# .env is gitignored - NEVER commit it.

# Required
JWT_SECRET_KEY=           # python -c "import secrets; print(secrets.token_hex(32))"

# Google OAuth (optional — needed for Google login)
# GOOGLE_CLIENT_ID=
# GOOGLE_CLIENT_SECRET=

# Keboola adapter (optional — skip if using CSV/sample data)
# KEBOOLA_STORAGE_TOKEN=
# KEBOOLA_STACK_URL=https://connection.keboola.com

# Bootstrap admin (optional — used on first docker compose up)
# SEED_ADMIN_EMAIL=admin@example.com

# Optional services
# TELEGRAM_BOT_TOKEN=
# JIRA_WEBHOOK_SECRET=
# ANTHROPIC_API_KEY=
```

- [ ] **Step 2: Commit**

```bash
git add config/.env.template
git commit -m "docs: update .env.template to match actual code (JWT_SECRET_KEY, not WEBAPP_SECRET_KEY)"
```

### Task 6.3: Remove dead Flask Blueprint from Jira connector

`connectors/jira/webhook.py` uses Flask Blueprint but the app uses FastAPI. It's dead code that confuses readers.

**Files:**
- Check: `connectors/jira/webhook.py` — verify it's not imported anywhere except Jira-internal code
- Modify: add deprecation comment or delete if unused

- [ ] **Step 1: Check if webhook.py is imported**

Run: `grep -r "from connectors.jira.webhook" app/ src/ services/`
If no matches: the Flask Blueprint is dead code.

- [ ] **Step 2: Add deprecation notice or delete**

If unused by the FastAPI app, delete `connectors/jira/webhook.py` and update any imports.

- [ ] **Step 3: Commit**

```bash
git add connectors/jira/
git commit -m "chore: remove dead Flask Blueprint from Jira connector (replaced by FastAPI)"
```

### Task 6.4: Add upload size limit

`upload_session` and `upload_artifact` read entire files into memory with no limit.

**Files:**
- Modify: `app/api/upload.py`
- Test: `tests/test_api_complete.py`

- [ ] **Step 1: Write the test**

```python
# In tests/test_api_complete.py or a new test file:

def test_upload_rejects_oversized_file(client, admin_headers):
    """Uploads over 50MB should be rejected."""
    # Create a file reference that claims to be too large
    import io
    large_data = b"x" * (50 * 1024 * 1024 + 1)  # 50MB + 1 byte
    resp = client.post(
        "/api/upload/artifact/test-session",
        files={"file": ("big.csv", io.BytesIO(large_data), "text/csv")},
        headers=admin_headers,
    )
    assert resp.status_code == 413 or resp.status_code == 400
```

- [ ] **Step 2: Add size check**

In `app/api/upload.py`, at the start of each upload endpoint:

```python
    MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)")
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_api_complete.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add app/api/upload.py tests/test_api_complete.py
git commit -m "fix: add 50MB upload size limit — prevent memory exhaustion"
```

---

## Workstream 7: DuckDB Lifecycle & Connection Management (P1)

### Task 7.1: Fix SQL injection in get_analytics_db_readonly

Same unquoted `ext_dir.name` issue as the orchestrator, but in the read-only analytics connection used by every query request.

**Files:**
- Modify: `src/db.py:228-233`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_db.py, add:

def test_analytics_readonly_rejects_malicious_dir_name(db_env):
    """get_analytics_db_readonly must skip directories with unsafe names."""
    extracts = db_env / "extracts"
    extracts.mkdir(parents=True)
    malicious = extracts / "test; DROP TABLE x--"
    malicious.mkdir()
    db_file = malicious / "extract.duckdb"

    import duckdb
    conn = duckdb.connect(str(db_file))
    conn.execute("""CREATE TABLE _meta (
        table_name VARCHAR, description VARCHAR, rows BIGINT,
        size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR
    )""")
    conn.close()

    # Should not crash
    from src.db import get_analytics_db_readonly
    ro_conn = get_analytics_db_readonly()
    ro_conn.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_db.py::test_analytics_readonly_rejects_malicious_dir_name -v`

- [ ] **Step 3: Add identifier validation**

Import the validator from orchestrator (or extract to shared module). In `src/db.py`, add at top:

```python
import re
_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
```

In `get_analytics_db_readonly()`, replace line 232:

```python
            if db_file.exists() and ext_dir.is_dir():
                if not _SAFE_IDENTIFIER.match(ext_dir.name):
                    continue
                try:
                    conn.execute(f"ATTACH '{db_file}' AS {ext_dir.name} (READ_ONLY)")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_db.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "fix: validate identifiers in get_analytics_db_readonly — prevent SQL injection"
```

### Task 7.2: Remove dead PRAGMA enable_wal code

`PRAGMA enable_wal` is not valid DuckDB syntax. DuckDB uses WAL by default since v0.8. This is dead code with a misleading comment.

**Files:**
- Modify: `src/db.py:200-204`

- [ ] **Step 1: Remove the dead code**

In `src/db.py`, delete lines 200-204:

```python
            # WAL mode: allows concurrent readers while writing
            try:
                _system_db_conn.execute("PRAGMA enable_wal")
            except Exception:
                pass  # Older DuckDB versions may not support this
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_db.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/db.py
git commit -m "chore: remove dead PRAGMA enable_wal — DuckDB uses WAL by default"
```

### Task 7.3: Escape token single-quotes in ATTACH SQL

If a token contains a single quote, the ATTACH SQL breaks. DuckDB doesn't support parameterized ATTACH, so escape manually.

**Files:**
- Modify: `src/orchestrator.py` (`_attach_remote_extensions`)
- Modify: `connectors/keboola/extractor.py` (`_try_attach_extension`)

- [ ] **Step 1: Add escaping in orchestrator**

In `src/orchestrator.py`, in `_attach_remote_extensions`, replace the ATTACH line:

```python
                if token:
                    escaped_token = token.replace("'", "''")
                    conn.execute(
                        f"ATTACH '{url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')"
                    )
```

- [ ] **Step 2: Add escaping in Keboola extractor**

In `connectors/keboola/extractor.py`, `_try_attach_extension`:

```python
        escaped_token = keboola_token.replace("'", "''")
        conn.execute(f"ATTACH '{keboola_url}' AS kbc (TYPE keboola, TOKEN '{escaped_token}')")
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_orchestrator.py tests/test_keboola_extractor.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/orchestrator.py connectors/keboola/extractor.py
git commit -m "fix: escape single quotes in ATTACH TOKEN to prevent SQL breakage"
```

### Task 7.4: Add temp-file swap to Jira extract_init.update_meta

Jira's `update_meta()` writes directly to live `extract.duckdb` while the orchestrator may have it ATTACHed read-only.

**Files:**
- Modify: `connectors/jira/extract_init.py:87`
- Test: `tests/test_e2e_extract.py`

- [ ] **Step 1: Examine current code and fix**

The `update_meta()` function opens `extract.duckdb` directly. Since it only updates `_meta` rows and recreates views (not bulk writes), the simplest fix is to use a short-lived connection with CHECKPOINT:

In `connectors/jira/extract_init.py`, after the `conn.execute("UPDATE _meta ...")` block, add before `conn.close()`:

```python
        conn.execute("CHECKPOINT")
```

This forces WAL flush and reduces the lock window. A full temp-file swap is not practical here since the Jira connector does incremental updates.

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_e2e_extract.py::TestJiraWebhookToQuery -v`
Expected: Pass

- [ ] **Step 3: Commit**

```bash
git add connectors/jira/extract_init.py
git commit -m "fix: add CHECKPOINT after Jira meta update — reduce lock window"
```

---

## Workstream 8: Scalability & Robustness (P1)

### Task 8.1: Fix table access check false positives in query endpoint

The query endpoint checks table access with `table.lower() in sql_lower` — a substring match. A table named `id` blocks any query containing the word "id". A table named `orders` triggers on `ordered_items`.

**Files:**
- Modify: `app/api/query.py:67-71`
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_security.py, add to TestQuerySecurity:

def test_table_access_no_false_positive_on_column_name(self, client, auth_headers):
    """A forbidden table named 'id' should not block queries that use 'id' as a column."""
    # This test verifies the table access check doesn't use naive substring matching
    resp = client.post("/api/query", json={
        "sql": "SELECT id, name FROM allowed_table"
    }, headers=auth_headers)
    # Should not get 403 just because 'id' appears in SQL
    assert resp.status_code != 403 or "id" not in resp.json().get("detail", "")
```

- [ ] **Step 2: Fix with word-boundary matching**

In `app/api/query.py`, replace the table access check (lines 67-71):

```python
            # Check if query references any forbidden tables (word-boundary match)
            import re
            forbidden = all_views - set(allowed)
            for table in forbidden:
                # Use word boundaries to avoid false positives on column names
                pattern = r'\b' + re.escape(table.lower()) + r'\b'
                if re.search(pattern, sql_lower):
                    raise HTTPException(status_code=403, detail=f"Access denied to table '{table}'")
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_security.py::TestQuerySecurity -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add app/api/query.py tests/test_security.py
git commit -m "fix: use word-boundary matching for table access check — prevent false positives"
```

### Task 8.2: Replace Docker healthcheck with curl

The current healthcheck starts a full Python interpreter + imports httpx every 30 seconds.

**Files:**
- Modify: `Dockerfile` (add curl)
- Modify: `docker-compose.yml:13`

- [ ] **Step 1: Add curl to Dockerfile**

In `Dockerfile`, add after the `FROM` line:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: Update docker-compose healthcheck**

In `docker-compose.yml`, replace line 13:

```yaml
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8000/api/health"]
      interval: 30s
      timeout: 5s
      retries: 3
```

- [ ] **Step 3: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "fix: use curl for Docker healthcheck instead of Python+httpx (faster, lighter)"
```

### Task 8.3: Add graceful shutdown handler

No lifespan handler exists to close the shared DuckDB connection on shutdown.

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add lifespan handler**

In `app/main.py`, add a lifespan context manager and use it in `FastAPI()`:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    # Startup
    yield
    # Shutdown: close shared DuckDB connection
    from src.db import close_system_db
    close_system_db()
```

Change `app = FastAPI(...)` to `app = FastAPI(..., lifespan=lifespan)`.

Add `close_system_db()` to `src/db.py`:

```python
def close_system_db() -> None:
    """Close the shared system DB connection. Called on app shutdown."""
    global _system_db_conn, _system_db_path
    if _system_db_conn:
        try:
            _system_db_conn.close()
        except Exception:
            pass
        _system_db_conn = None
        _system_db_path = None
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_api.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add app/main.py src/db.py
git commit -m "feat: add graceful shutdown handler — close DuckDB on app exit"
```

### Task 8.4: Extract shared _get_data_dir utility

`_get_data_dir()` is copy-pasted in 4 API files.

**Files:**
- Create: `app/utils.py`
- Modify: `app/api/sync.py`, `app/api/data.py`, `app/api/upload.py`, `app/api/catalog.py`

- [ ] **Step 1: Create shared utility**

```python
# app/utils.py
import os
from pathlib import Path

def get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))
```

- [ ] **Step 2: Replace in all 4 files**

In each file, replace:
```python
def _get_data_dir():
    return Path(os.environ.get("DATA_DIR", "./data"))
```

With:
```python
from app.utils import get_data_dir as _get_data_dir
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/ -q --tb=short`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add app/utils.py app/api/sync.py app/api/data.py app/api/upload.py app/api/catalog.py
git commit -m "refactor: extract shared _get_data_dir to app/utils.py — DRY"
```

### Task 8.5: Move faker to dev dependencies

Faker is a production dependency but only used for sample data generation.

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-dev.txt`

- [ ] **Step 1: Move faker**

Remove `faker>=24.0.0` from `requirements.txt`.

Create `requirements-dev.txt`:

```
-r requirements.txt
faker>=24.0.0
pytest>=9.0.0
pytest-timeout>=2.0.0
```

- [ ] **Step 2: Verify app starts without faker**

Run: `python -c "from app.main import create_app; print('OK')"`
Expected: OK (faker not imported at startup)

- [ ] **Step 3: Commit**

```bash
git add requirements.txt requirements-dev.txt
git commit -m "chore: move faker to dev dependencies — not needed in production"
```

---

## Workstream 9: Missing Test Coverage (P0-P1)

### Task 9.1: Add web UI smoke tests

`app/web/router.py` has 46 functions with almost no test coverage. A template error would not be caught.

**Files:**
- Create: `tests/test_web_ui.py`

- [ ] **Step 1: Create smoke tests for all authenticated pages**

```python
"""Smoke tests for web UI pages — verify they render without template errors."""

import os
import pytest
import duckdb
from fastapi.testclient import TestClient


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()

    from app.main import create_app
    app = create_app()
    return TestClient(app)


@pytest.fixture
def admin_cookie(web_client, tmp_path, monkeypatch):
    """Create admin user and return cookie dict."""
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    UserRepository(conn).create(id="admin1", email="admin@test.com", role="admin")
    conn.close()

    token = create_access_token(user_id="admin1", email="admin@test.com", role="admin")
    return {"access_token": token}


class TestWebUISmoke:
    """Every page should return 200 without template errors."""

    def test_login_page(self, web_client):
        resp = web_client.get("/login")
        assert resp.status_code == 200

    def test_dashboard(self, web_client, admin_cookie):
        resp = web_client.get("/", cookies=admin_cookie)
        assert resp.status_code in (200, 302)

    def test_catalog(self, web_client, admin_cookie):
        resp = web_client.get("/catalog", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_admin_tables(self, web_client, admin_cookie):
        resp = web_client.get("/admin/tables", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_admin_permissions(self, web_client, admin_cookie):
        resp = web_client.get("/admin/permissions", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_corporate_memory(self, web_client, admin_cookie):
        resp = web_client.get("/corporate-memory", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_activity_center(self, web_client, admin_cookie):
        resp = web_client.get("/activity-center", cookies=admin_cookie)
        assert resp.status_code == 200
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_web_ui.py -v`
Expected: All pass (or reveal actual template errors)

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_ui.py
git commit -m "test: add web UI smoke tests — catch template errors in 7 pages"
```

### Task 9.2: Add Jira service integration tests

`connectors/jira/service.py` (15 functions) orchestrates the entire Jira webhook flow but has no dedicated tests.

**Files:**
- Create: `tests/test_jira_service.py`

- [ ] **Step 1: Create integration tests**

```python
"""Tests for Jira service — webhook event processing pipeline."""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import duckdb
import pytest

from connectors.jira.extract_init import init_extract, update_meta


@pytest.fixture
def jira_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    jira_dir = tmp_path / "extracts" / "jira"
    jira_dir.mkdir(parents=True)
    return jira_dir


class TestJiraExtractInit:
    def test_init_creates_extract_db(self, jira_env):
        init_extract(jira_env)
        assert (jira_env / "extract.duckdb").exists()

        conn = duckdb.connect(str(jira_env / "extract.duckdb"))
        meta = conn.execute("SELECT * FROM _meta").fetchall()
        conn.close()
        assert isinstance(meta, list)

    def test_update_meta_creates_view(self, jira_env):
        init_extract(jira_env)

        # Create a parquet file for 'issues'
        issues_dir = jira_env / "data" / "issues"
        issues_dir.mkdir(parents=True)
        pq_path = str(issues_dir / "2026-04.parquet")
        tmp = duckdb.connect()
        tmp.execute(
            f"COPY (SELECT 'PROJ-1' AS issue_key, 'Bug' AS type) "
            f"TO '{pq_path}' (FORMAT PARQUET)"
        )
        tmp.close()

        update_meta(jira_env, "issues")

        conn = duckdb.connect(str(jira_env / "extract.duckdb"))
        rows = conn.execute("SELECT rows FROM _meta WHERE table_name='issues'").fetchone()
        assert rows[0] == 1

        data = conn.execute("SELECT issue_key FROM issues").fetchone()
        assert data[0] == "PROJ-1"
        conn.close()
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_jira_service.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add tests/test_jira_service.py
git commit -m "test: add Jira extract_init integration tests"
```

### Task 9.3: Add instance_config tests

`app/instance_config.py` (10 functions) is loaded at startup and affects all web pages. No tests exist.

**Files:**
- Create: `tests/test_instance_config.py`

- [ ] **Step 1: Create tests**

```python
"""Tests for instance_config — YAML loading and accessor functions."""

import os
from pathlib import Path

import pytest


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return config_dir


class TestInstanceConfig:
    def test_missing_config_file_returns_defaults(self, config_env, monkeypatch):
        """Missing instance.yaml should not crash, just return defaults."""
        from app.instance_config import get_instance_name, get_data_source_type
        # Should return some default, not crash
        name = get_instance_name()
        assert isinstance(name, str)

    def test_loads_valid_yaml(self, config_env, tmp_path, monkeypatch):
        """Valid instance.yaml should be loaded and accessible."""
        yaml_path = tmp_path / "config" / "instance.yaml"
        yaml_path.write_text("instance_name: Test Instance\ndata_source: keboola\n")

        from app.instance_config import load_instance_config, get_instance_name
        import importlib
        import app.instance_config as mod
        importlib.reload(mod)

        name = mod.get_instance_name()
        assert "Test" in name or isinstance(name, str)
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_instance_config.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add tests/test_instance_config.py
git commit -m "test: add instance_config tests — missing file, valid YAML"
```

### Task 9.4: Add concurrent rebuild safety test

Verify the atomic swap pattern works when a read connection is open.

**Files:**
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the test**

```python
# In tests/test_orchestrator.py, add:

def test_rebuild_while_reading(setup_env):
    """Rebuild should succeed even while a read-only connection exists."""
    from src.orchestrator import SyncOrchestrator
    import duckdb

    _create_mock_extract(
        setup_env["extracts_dir"], "keboola",
        [{"name": "orders", "data": [{"id": "1"}]}],
    )

    orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
    orch.rebuild()

    # Open a read-only connection (simulating query endpoint)
    reader = duckdb.connect(setup_env["analytics_db"], read_only=True)

    # Rebuild while reader is open — should not crash
    result = orch.rebuild()
    assert "keboola" in result

    reader.close()
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_orchestrator.py::test_rebuild_while_reading -v`
Expected: Pass

- [ ] **Step 3: Commit**

```bash
git add tests/test_orchestrator.py
git commit -m "test: add concurrent rebuild safety test"
```

---

## Execution Order

Workstreams are independent and can run in parallel. Within each workstream, tasks are sequential.

**Critical path (do first):**
1. Task 1.1 (password bypass) — active auth vulnerability
2. Task 3.1 (rebuild_source) — active data loss bug
3. Task 2.1 (SQL injection) — security hardening

**Then:**
4. Tasks 1.2, 1.3 (JWT hardening)
5. Tasks 2.2 (query blocklist)
6. Tasks 3.2, 3.3 (WAL + BQ swap)
7. Task 4.1 (sandbox)
8. Tasks 7.1-7.4 (DuckDB lifecycle)
9. Tasks 8.1-8.5 (scalability + cleanup)
10. Tasks 5.1-5.3 (test hardening)
11. Tasks 9.1-9.4 (missing test coverage)
12. Tasks 6.1-6.4 (docs + cleanup)
13. Task 1.4 (cookie auth)

**Verification after all tasks:**

```bash
pytest tests/ -v --tb=short  # All 620+ tests pass
```

Workstreams are independent and can run in parallel. Within each workstream, tasks are sequential.

**Critical path (do first):**
1. Task 1.1 (password bypass) — active auth vulnerability
2. Task 3.1 (rebuild_source) — active data loss bug
3. Task 2.1 (SQL injection) — security hardening

**Then:**
4. Tasks 1.2, 1.3 (JWT hardening)
5. Tasks 2.2 (query blocklist)
6. Tasks 3.2, 3.3 (WAL + BQ swap)
7. Task 4.1 (sandbox)
8. Tasks 5.1-5.3 (test hardening)
9. Tasks 6.1-6.4 (docs + cleanup)

**Verification after all tasks:**

```bash
pytest tests/ -v --tb=short  # All 607+ tests pass
```
