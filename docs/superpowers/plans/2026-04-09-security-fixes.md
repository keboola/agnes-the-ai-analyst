# Security Fixes for Production Deployment

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all critical and important security findings before deploying to paying customers.

**Architecture:** Targeted fixes to auth, RBAC, query endpoint, script sandbox, and upload endpoints. No architectural changes — just closing specific holes identified by security review.

**Tech Stack:** Python 3.13, FastAPI, DuckDB, argon2-cffi, PyJWT

**Source:** Security posture review 2026-04-09 (findings C1-C3, I1-I5, I10-I11)

---

## File Map

| File | Responsibility | Tasks |
|------|---------------|-------|
| `app/auth/router.py` | Token endpoint auth | 1 |
| `app/web/router.py` | Web UI route guards | 2 |
| `app/api/query.py` | SQL query blocklist | 3 |
| `app/api/scripts.py` | Script execution RBAC | 4 |
| `app/api/catalog.py` | Catalog profile access control | 5 |
| `app/api/upload.py` | Upload path leak fix | 6 |
| `app/auth/providers/google.py` | Cookie secure flag | 7 |
| `app/instance_config.py` | Instance name YAML path fix | 8 |

---

### Task 1: Block /auth/token for OAuth-only users (C1)

Users without `password_hash` (OAuth-only) can get a JWT by just sending their email. This is an account takeover vulnerability.

**Files:**
- Modify: `app/auth/router.py:47-56`
- Test: `tests/test_auth_providers.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_auth_providers.py, add to TestTokenEndpoint:

def test_token_rejected_for_oauth_only_user(self, client, e2e_env):
    """OAuth-only users (no password_hash) cannot get token via /auth/token."""
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    repo = UserRepository(conn)
    # Create user without password (simulates Google OAuth user)
    repo.create(id="oauth-user", email="oauth@test.com", role="analyst")
    conn.close()

    resp = client.post("/auth/token", json={"email": "oauth@test.com"})
    assert resp.status_code == 401
    assert "password" in resp.json()["detail"].lower() or "provider" in resp.json()["detail"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auth_providers.py::TestTokenEndpoint::test_token_rejected_for_oauth_only_user -v`
Expected: FAIL — currently returns 200

- [ ] **Step 3: Fix the auth logic**

In `app/auth/router.py`, replace lines 47-56 with:

```python
    # Require authentication proof
    if user.get("password_hash"):
        # User has password — require and verify it
        if not request.password:
            raise HTTPException(status_code=401, detail="Password required")
        try:
            from argon2 import PasswordHasher
            ph = PasswordHasher()
            ph.verify(user["password_hash"], request.password)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid password")
    else:
        # No password set — user must use their auth provider (Google, magic link)
        raise HTTPException(
            status_code=401,
            detail="This account uses external authentication. Please log in via your configured provider.",
        )
```

Also update the docstring on line 41:
```python
    """Issue a JWT token. Requires password for password-protected accounts.
    OAuth-only accounts must use their auth provider instead."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auth_providers.py -v`
Expected: All pass

- [ ] **Step 5: Verify bootstrap still works**

Run: `pytest tests/test_bootstrap.py -v`
Expected: All pass (bootstrap creates user WITH password or returns token directly)

- [ ] **Step 6: Commit**

```bash
git add app/auth/router.py tests/test_auth_providers.py
git commit -m "fix: block /auth/token for OAuth-only users — require password or external provider"
```

---

### Task 2: Add role checks to web admin pages (C2)

`/admin/tables`, `/admin/permissions`, `/corporate-memory/admin` are accessible to any authenticated user.

**Files:**
- Modify: `app/web/router.py:431-452,388-394`
- Test: `tests/test_web_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
# In tests/test_web_ui.py, add:

@pytest.fixture
def analyst_cookie(web_client, tmp_path, monkeypatch):
    """Create analyst user (non-admin) and return cookie."""
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    conn = get_system_db()
    UserRepository(conn).create(id="analyst1", email="analyst@test.com", name="Analyst", role="analyst")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "analyst@test.com"})
    # analyst has no password_hash — need to give them one first
    # Actually: after Task 1, this will fail. Create with password instead:
    from argon2 import PasswordHasher
    from src.db import get_system_db as gsdb
    c = gsdb()
    c.execute("UPDATE users SET password_hash = ? WHERE id = ?",
              [PasswordHasher().hash("testpass"), "analyst1"])
    c.close()
    resp = web_client.post("/auth/token", json={"email": "analyst@test.com", "password": "testpass"})
    assert resp.status_code == 200
    return {"access_token": resp.json()["access_token"]}


class TestWebUIRBAC:
    def test_admin_tables_requires_admin(self, web_client, analyst_cookie):
        resp = web_client.get("/admin/tables", cookies=analyst_cookie)
        assert resp.status_code == 403

    def test_admin_permissions_requires_admin(self, web_client, analyst_cookie):
        resp = web_client.get("/admin/permissions", cookies=analyst_cookie)
        assert resp.status_code == 403

    def test_corporate_memory_admin_requires_km_admin(self, web_client, analyst_cookie):
        resp = web_client.get("/corporate-memory/admin", cookies=analyst_cookie)
        assert resp.status_code == 403

    def test_admin_can_access_admin_tables(self, web_client, admin_cookie):
        resp = web_client.get("/admin/tables", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_admin_can_access_admin_permissions(self, web_client, admin_cookie):
        resp = web_client.get("/admin/permissions", cookies=admin_cookie)
        assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web_ui.py::TestWebUIRBAC -v`
Expected: analyst can access admin pages (403 expected, gets 200)

- [ ] **Step 3: Add role checks to web routes**

In `app/web/router.py`, add import at top:
```python
from src.rbac import Role
from app.auth.dependencies import require_role
```

Replace line 434 (`user: dict = Depends(get_current_user)`) in `admin_tables`:
```python
    user: dict = Depends(require_role(Role.ADMIN)),
```

Replace line 448 in `admin_permissions_page`:
```python
    user: dict = Depends(require_role(Role.ADMIN)),
```

Replace line 391 in `corporate_memory_admin`:
```python
    user: dict = Depends(require_role(Role.KM_ADMIN)),
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_web_ui.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add app/web/router.py tests/test_web_ui.py
git commit -m "fix: require admin/km_admin role for web admin pages"
```

---

### Task 3: Expand SQL query blocklist with DuckDB metadata (C3)

The query endpoint doesn't block `information_schema`, `duckdb_tables()`, `duckdb_columns()`, relative paths, or `pragma_` functions.

**Files:**
- Modify: `app/api/query.py:40-54`
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing tests**

```python
# In tests/test_security.py, add to TestQuerySecurity:

def test_blocks_information_schema(self, client, auth_headers):
    resp = client.post("/api/query", json={"sql": "SELECT * FROM information_schema.tables"}, headers=auth_headers)
    assert resp.status_code == 400

def test_blocks_duckdb_tables(self, client, auth_headers):
    resp = client.post("/api/query", json={"sql": "SELECT * FROM duckdb_tables()"}, headers=auth_headers)
    assert resp.status_code == 400

def test_blocks_duckdb_columns(self, client, auth_headers):
    resp = client.post("/api/query", json={"sql": "SELECT * FROM duckdb_columns()"}, headers=auth_headers)
    assert resp.status_code == 400

def test_blocks_duckdb_databases(self, client, auth_headers):
    resp = client.post("/api/query", json={"sql": "SELECT * FROM duckdb_databases()"}, headers=auth_headers)
    assert resp.status_code == 400

def test_blocks_relative_path(self, client, auth_headers):
    resp = client.post("/api/query", json={"sql": "SELECT * FROM '../secret.parquet'"}, headers=auth_headers)
    assert resp.status_code == 400

def test_blocks_pragma_table_info(self, client, auth_headers):
    resp = client.post("/api/query", json={"sql": "SELECT * FROM pragma_table_info('users')"}, headers=auth_headers)
    assert resp.status_code == 400
```

- [ ] **Step 2: Run to verify failures**

Run: `pytest tests/test_security.py::TestQuerySecurity -v`
Expected: New tests FAIL

- [ ] **Step 3: Expand the blocklist**

In `app/api/query.py`, replace the `blocked` list (lines 40-54):

```python
    blocked = [
        # DDL/DML
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
        # URL/path schemes
        "'/", '"/','http://', 'https://', 's3://', 'gcs://',
        "'../", '"../',
        # DuckDB metadata (leaks schema info regardless of RBAC)
        "information_schema", "duckdb_tables", "duckdb_columns",
        "duckdb_databases", "duckdb_settings", "duckdb_functions",
        "duckdb_views", "duckdb_indexes", "duckdb_schemas",
        "pragma_table_info", "pragma_storage_info",
        # Multiple statements
        ";",
    ]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_security.py::TestQuerySecurity -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add app/api/query.py tests/test_security.py
git commit -m "fix: block DuckDB metadata functions and relative paths in query endpoint"
```

---

### Task 4: Restrict script execution to analyst role (I2/I4)

Any authenticated user (including viewers) can deploy and execute scripts.

**Files:**
- Modify: `app/api/scripts.py:53-56,72-73,83-84`
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_security.py, add:

class TestScriptRBAC:
    def test_viewer_cannot_run_scripts(self, client):
        """Viewers should not be able to execute scripts."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from app.auth.jwt import create_access_token

        conn = get_system_db()
        UserRepository(conn).create(id="viewer1", email="viewer@test.com", role="viewer")
        conn.close()

        token = create_access_token(user_id="viewer1", email="viewer@test.com", role="viewer")
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.post("/api/scripts/run", json={
            "name": "test", "source": "print('hi')"
        }, headers=headers)
        assert resp.status_code == 403

    def test_viewer_cannot_deploy_scripts(self, client):
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from app.auth.jwt import create_access_token

        conn = get_system_db()
        try:
            UserRepository(conn).create(id="viewer2", email="viewer2@test.com", role="viewer")
        except Exception:
            pass
        conn.close()

        token = create_access_token(user_id="viewer2", email="viewer2@test.com", role="viewer")
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.post("/api/scripts/deploy", json={
            "name": "test", "source": "print('hi')", "schedule": ""
        }, headers=headers)
        assert resp.status_code == 403
```

- [ ] **Step 2: Run to verify failures**

Run: `pytest tests/test_security.py::TestScriptRBAC -v`
Expected: FAIL — viewers get 200

- [ ] **Step 3: Add role requirements**

In `app/api/scripts.py`, add import:
```python
from app.auth.dependencies import require_role
from src.rbac import Role
```

Replace `get_current_user` with `require_role(Role.ANALYST)` on these endpoints:
- `deploy_script` (line 56): `user: dict = Depends(require_role(Role.ANALYST)),`
- `run_ad_hoc` (line 73): `user: dict = Depends(require_role(Role.ANALYST)),`
- `run_deployed` (line 84): `user: dict = Depends(require_role(Role.ANALYST)),`
- `list_scripts` (line 46): keep as `get_current_user` (read-only, safe for all)
- `undeploy_script` (line 101): `user: dict = Depends(require_role(Role.ADMIN)),`

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_security.py tests/test_api_scripts.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add app/api/scripts.py tests/test_security.py
git commit -m "fix: restrict script deploy/execute to analyst role, undeploy to admin"
```

---

### Task 5: Add access control to catalog profile endpoints (I5)

`/api/catalog/profile/{table_name}` returns profile data without checking table access.

**Files:**
- Modify: `app/api/catalog.py:18-39`
- Test: `tests/test_access_control.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_access_control.py, add to TestPrivateTablesRestricted or new class:

class TestCatalogProfileAccessControl:
    def test_profile_denied_for_private_table(self, client, e2e_env):
        """Analyst without explicit access should not see profile of private table."""
        # Assumes 'private_table' is registered as private in e2e_env
        # and the test user doesn't have access
        from app.auth.jwt import create_access_token
        token = create_access_token(user_id="analyst-no-access", email="noaccess@test.com", role="analyst")
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get("/api/catalog/profile/private_table", headers=headers)
        assert resp.status_code == 403
```

- [ ] **Step 2: Run to verify failure**

- [ ] **Step 3: Add access check**

In `app/api/catalog.py`, add import:
```python
from src.rbac import can_access_table
```

In `get_table_profile` (line 18), after `user: dict = Depends(get_current_user)`, add:

```python
    # Check table-level access
    if not can_access_table(user, table_name, conn):
        raise HTTPException(status_code=403, detail=f"Access denied to table '{table_name}'")
```

Add the same check to the `/profile/{table_name}/refresh` endpoint if it exists.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_access_control.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add app/api/catalog.py tests/test_access_control.py
git commit -m "fix: add per-table access control to catalog profile endpoints"
```

---

### Task 6: Stop leaking internal file paths in upload responses (I10)

Upload endpoints return `"path": str(target)` exposing server filesystem structure.

**Files:**
- Modify: `app/api/upload.py:37,59`
- Test: `tests/test_api_complete.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_api_complete.py, add to TestUpload:

def test_upload_does_not_leak_absolute_path(self, client, admin_headers):
    """Upload response should not contain absolute filesystem paths."""
    import io
    resp = client.post(
        "/api/upload/session/test-session",
        files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert not data.get("path", "").startswith("/"), "Response should not leak absolute path"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_api_complete.py::TestUpload::test_upload_does_not_leak_absolute_path -v`
Expected: FAIL — returns `/data/user_sessions/...`

- [ ] **Step 3: Fix upload responses**

In `app/api/upload.py`, replace `"path": str(target)` with `"filename": filename` in both endpoints:

Line 37:
```python
    return {"status": "ok", "filename": filename, "size": len(content)}
```

Line 59:
```python
    return {"status": "ok", "filename": filename, "size": len(content)}
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_api_complete.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add app/api/upload.py tests/test_api_complete.py
git commit -m "fix: return filename instead of absolute path in upload responses"
```

---

### Task 7: Force secure cookie flag in production (I11)

Google OAuth callback sets `secure=True` only when the request is HTTPS. Behind a TLS-terminating proxy, the app sees HTTP.

**Files:**
- Modify: `app/auth/providers/google.py:92-98`

- [ ] **Step 1: Fix the cookie setting**

In `app/auth/providers/google.py`, replace lines 92-98:

```python
        is_production = os.environ.get("TESTING", "").lower() not in ("1", "true")
        response = RedirectResponse(url="/dashboard", status_code=302)
        response.set_cookie(
            key="access_token", value=jwt_token,
            httponly=True, max_age=86400, samesite="lax",
            secure=is_production,  # Always secure in production (behind TLS proxy)
        )
```

Note: `max_age` reduced from `86400 * 30` (30 days) to `86400` (1 day) to match JWT expiry.

- [ ] **Step 2: Run auth tests**

Run: `pytest tests/test_auth_providers.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add app/auth/providers/google.py
git commit -m "fix: force secure cookie flag in production, align cookie max_age with JWT expiry"
```

---

### Task 8: Fix instance_config YAML path for instance name (C4)

`get_instance_name()` reads flat key `instance_name` but YAML structure is `instance.name`.

**Files:**
- Modify: `app/instance_config.py:48-53`
- Test: `tests/test_instance_config.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_instance_config.py, add:

def test_reads_nested_instance_name(self, tmp_path, monkeypatch):
    """get_instance_name should read instance.name from YAML, not flat instance_name."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")

    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "instance.yaml").write_text(
        "instance:\n  name: Acme Analytics\n  subtitle: Data Team\n"
    )

    import importlib
    import app.instance_config as mod
    importlib.reload(mod)

    assert mod.get_instance_name() == "Acme Analytics"
    assert mod.get_instance_subtitle() == "Data Team"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_instance_config.py::TestInstanceConfig::test_reads_nested_instance_name -v`
Expected: FAIL — returns "AI Data Analyst" instead of "Acme Analytics"

- [ ] **Step 3: Fix the accessor functions**

In `app/instance_config.py`, replace lines 48-53:

```python
def get_instance_name() -> str:
    return get_value("instance", "name", default="AI Data Analyst")


def get_instance_subtitle() -> str:
    return get_value("instance", "subtitle", default="")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_instance_config.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add app/instance_config.py tests/test_instance_config.py
git commit -m "fix: get_instance_name reads nested instance.name from YAML"
```

---

## Execution Order

Tasks are independent and can run in parallel (different files). Recommended order by impact:

1. **Task 1** (C1) — account takeover via /auth/token
2. **Task 2** (C2) — admin pages exposed
3. **Task 3** (C3) — SQL metadata leaks
4. **Task 4** (I4) — script execution RBAC
5. **Task 5** (I5) — catalog profile access control
6. **Task 8** (C4) — instance name config
7. **Task 6** (I10) — upload path leak
8. **Task 7** (I11) — cookie secure flag

**Verification after all tasks:**

```bash
pytest tests/ -v --tb=short  # All 650+ tests pass
```
