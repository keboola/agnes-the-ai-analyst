# User Management + PAT + CLI Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dodat plný scope issues #10, #11, #12 a #9 — HTML auth redirect, user management UI/API/CLI, Personal Access Tokens a CLI distribuce z docker image s install stránkou — pro produkční multi-customer nasazení Agnes.

**Architecture:** 4 fáze sériové tam, kde sdílejí schema migrace nebo stejný soubor. V místech, kde scope nekoliduje, běží paralelně v oddělených worktreech. Každá fáze je self-contained, má vlastní TDD tasky a commituje se průběžně.

**Tech Stack:** Python 3.13, FastAPI, DuckDB, Jinja2 templates (Bootstrap-like CSS), Typer CLI, PyJWT, Argon2, pytest, Docker (uv build), httpx.

**Sekvence a paralelismus:**

```
Phase 0 (#10 HTML redirect)          ── 1–2 h, standalone
   │
   ▼
Phase 1 (#11 User management)        ── schema v5, API+CLI+UI, 1 den
   │
   ▼
Phase 2 (#12 PAT)  ──────────┐       ── schema v6, JWT+API+CLI+UI
                             ├── paralelně ve 2 worktreech
Phase 3 (#9 CLI dist)  ──────┘       ── Dockerfile+/cli/*+/install+login fix
```

**Konflikt mapa (review-audited):** Phase 2 a Phase 3 sdílejí 4 soubory, ne jen jeden. Všechny jsou ale malé/lokální edity a merge je triviální:

| Soubor | Phase 1 | Phase 2 | Phase 3 | Řešení |
|---|---|---|---|---|
| `app/main.py` | — | register `tokens_router` | register `cli_artifacts_router` | oba přidávají `include_router`, merge konflikt-free |
| `app/web/router.py` | `/admin/users` route | `/profile` route | `/install` route | appendy na konec, konflikt max 1 řádek |
| `app/web/templates/dashboard.html` | admin nav link | profile nav link | install nav link | ve stejném `<nav>` bloku — označit jeden owner (Phase 3) který doplní všechny tři linky podle existujících rolí |
| `cli/commands/auth.py` | — | register `token_app` | fix login password | sériové: Phase 3 první, Phase 2 rebase |

**Pravidlo:** Phase 3 se mergne první (obsahuje `da login` fix + dashboard nav). Phase 2 potom rebase na main. Phase 1 nemá konflikt s Phase 2/3 v nav (adminský link), ostatní soubory disjunktní.

**Testování:** Každá fáze končí zeleným `pytest tests/` na celé sadě. Každý task má TDD cyklus: failing test → minimal impl → passing test → commit.

---

## Phase 0 — HTML Auth Redirect (#10)

Backend `get_current_user` dnes hází `HTTPException(401)` pro všechny requesty. Browser dostane JSON místo přesměrování na `/login`. Cíl: rozlišit API vs. HTML request a pro HTML vracet `RedirectResponse("/login")`.

### File Structure

- Modify: `app/auth/dependencies.py` — rozlišit request typ přes `Accept` header / cestu
- Test: `tests/test_auth_html_redirect.py` (new)

### Task 0.1: Test — HTML request bez tokenu dostane 302 na /login

**Files:**
- Create: `tests/test_auth_html_redirect.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for #10 — unauthenticated HTML routes must redirect, not return JSON 401."""

from fastapi.testclient import TestClient

from app.main import app


def test_html_route_without_token_redirects_to_login():
    """GET /dashboard without token must return 302 to /login (not 401 JSON)."""
    client = TestClient(app, follow_redirects=False)
    response = client.get("/dashboard", headers={"Accept": "text/html"})
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_api_route_without_token_returns_401_json():
    """GET /api/users without token must return 401 JSON (not redirect)."""
    client = TestClient(app, follow_redirects=False)
    response = client.get("/api/users", headers={"Accept": "application/json"})
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_html_route_with_bad_token_redirects_to_login():
    """Expired/invalid cookie on HTML route → redirect to /login, not JSON 401."""
    client = TestClient(app, follow_redirects=False)
    response = client.get(
        "/dashboard",
        headers={"Accept": "text/html"},
        cookies={"access_token": "bogus.token.here"},
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_root_without_token_still_redirects_to_login():
    """Regression: `/` uses get_optional_user and must still render/redirect, not crash."""
    client = TestClient(app, follow_redirects=False)
    response = client.get("/", headers={"Accept": "text/html"})
    # `/` redirects based on auth state; without token we go to /login
    assert response.status_code == 302
    assert response.headers["location"] == "/login"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auth_html_redirect.py -v`
Expected: first and third tests FAIL (HTTP 401 returned instead of 302); second test likely passes.

- [ ] **Step 3: Implement HTML-aware auth dependency in `app/auth/dependencies.py`**

Replace the body of `get_current_user` so HTML requests get a redirect instead of a 401. Keep `get_optional_user` intact (it already returns None on failure).

```python
"""FastAPI auth dependencies — current user, role checking."""

from typing import Optional

import duckdb
from fastapi import Depends, HTTPException, Header, Request, status
from fastapi.responses import RedirectResponse

from app.auth.jwt import verify_token
from src.db import get_system_db
from src.rbac import Role, ROLE_HIERARCHY
from src.repositories.users import UserRepository


def _get_db():
    conn = get_system_db()
    try:
        yield conn
    finally:
        conn.close()


def _wants_html(request: Optional[Request]) -> bool:
    """True if client is a browser expecting HTML (not an API client wanting JSON)."""
    if request is None:
        return False
    # Explicit JSON request from an API client — never redirect.
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return False
    # Path heuristic — /api/* and /auth/* are never HTML surfaces.
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/auth/"):
        return False
    # Everything else (browser navigations to /dashboard, /admin/*, /profile/*, etc.)
    # treats text/html or */* as HTML.
    return "text/html" in accept or "*/*" in accept or accept == ""


class _HTMLAuthRedirect(Exception):
    """Sentinel raised by auth dependencies to trigger redirect instead of 401."""


async def get_current_user(
    request: Request = None,
    authorization: Optional[str] = Header(None),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Extract and validate JWT from Authorization header or cookie. Returns user dict.

    HTML browser requests without a valid token get redirected to /login via an
    exception handler in app/main.py (#10). API requests keep getting JSON 401.
    """
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
    if not token and request:
        token = request.cookies.get("access_token")

    def _fail(detail: str) -> None:
        if _wants_html(request):
            raise _HTMLAuthRedirect()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=detail
        )

    if not token:
        _fail("Missing or invalid Authorization header")
    payload = verify_token(token)
    if not payload:
        _fail("Invalid or expired token")

    repo = UserRepository(conn)
    user = repo.get_by_id(payload.get("sub", ""))
    if not user:
        _fail("User not found")
    return user


async def get_optional_user(
    request: Request = None,
    authorization: Optional[str] = Header(None),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> Optional[dict]:
    """Like get_current_user but returns None instead of 401 if no token."""
    try:
        return await get_current_user(request=request, authorization=authorization, conn=conn)
    except (HTTPException, _HTMLAuthRedirect):
        return None


def require_role(minimum_role: Role):
    """Dependency factory: require user has at least the given role."""
    async def _check(request: Request, user: dict = Depends(get_current_user)):
        user_role = Role(user.get("role", "viewer"))
        if ROLE_HIERARCHY.get(user_role, 0) < ROLE_HIERARCHY.get(minimum_role, 0):
            if _wants_html(request):
                raise _HTMLAuthRedirect()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role {minimum_role.value} or higher",
            )
        return user
    return _check


async def require_admin(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """Dependency: require user is an admin. Raises 403 or redirects on HTML requests."""
    if user.get("role") != "admin":
        if _wants_html(request):
            raise _HTMLAuthRedirect()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user
```

- [ ] **Step 4: Register redirect exception handler in `app/main.py`**

Add inside `create_app()` after middleware registration, before router includes:

```python
from fastapi import Request
from fastapi.responses import RedirectResponse
from app.auth.dependencies import _HTMLAuthRedirect

@app.exception_handler(_HTMLAuthRedirect)
async def _html_auth_redirect_handler(request: Request, exc: _HTMLAuthRedirect):
    return RedirectResponse(url="/login", status_code=302)
```

- [ ] **Step 5: Run tests — verify pass**

Run: `pytest tests/test_auth_html_redirect.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 6: Run full test suite — verify no regressions**

Run: `pytest tests/ -x --timeout=30`
Expected: no new failures.

- [ ] **Step 7: Commit**

```bash
git add app/auth/dependencies.py app/main.py tests/test_auth_html_redirect.py
git commit -m "fix(auth): redirect HTML requests to /login instead of JSON 401 (#10)"
```

---

## Phase 1 — User Management (#11)

Scope: schema v5 (`active` column + audit fields), API (PATCH, reset-password, set-password, deactivate, activate) s self-lockout safeguards, CLI (5 nových `da admin` příkazů), UI (`/admin/users` stránka), runtime-kontrola `active` v `get_current_user`.

### File Structure

- Modify: `src/db.py` — schema v5 + migration
- Modify: `src/repositories/users.py` — extended `update`, `count_admins`, `list_all_with_active`
- Modify: `app/api/users.py` — 5 nových endpointů + safeguards + audit
- Modify: `app/auth/dependencies.py` — kontrola `active=false` v `get_current_user`
- Modify: `cli/commands/admin.py` — 5 nových příkazů, rozšířený `list-users` output
- Create: `app/web/templates/admin_users.html`
- Modify: `app/web/router.py` — přidat `/admin/users` route + nav link
- Modify: `app/web/templates/dashboard.html` — admin nav link na user management
- Test: `tests/test_user_management.py` (new)

### Task 1.1: Schema v5 — users.active + deactivated_at/by

**Files:**
- Modify: `src/db.py:19` (bump `SCHEMA_VERSION`)
- Modify: `src/db.py:27-39` (`users` table definition)
- Modify: `src/db.py:387-426` (add `_V4_TO_V5_MIGRATIONS`)
- Modify: `src/db.py:460-471` (migration dispatch)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_management.py
"""Tests for #11 — user management (active flag, safeguards, endpoints)."""

import os
import tempfile
import pytest

import duckdb

from src.db import _ensure_schema, get_schema_version


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        yield tmp


def test_schema_v5_adds_active_column(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        cols = conn.execute("PRAGMA table_info(users)").fetchall()
        col_names = [c[1] for c in cols]
        assert "active" in col_names
        assert "deactivated_at" in col_names
        assert "deactivated_by" in col_names
        assert get_schema_version(conn) >= 5
    finally:
        conn.close()
        close_system_db()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_management.py::test_schema_v5_adds_active_column -v`
Expected: FAIL with "active column not found" or schema version < 5.

- [ ] **Step 3: Bump `SCHEMA_VERSION` and update users DDL**

In `src/db.py`:

```python
SCHEMA_VERSION = 5
```

Update the `users` table in `_SYSTEM_SCHEMA`:

```python
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR PRIMARY KEY,
    email VARCHAR UNIQUE NOT NULL,
    name VARCHAR,
    role VARCHAR DEFAULT 'analyst',
    password_hash VARCHAR,
    setup_token VARCHAR,
    setup_token_created TIMESTAMP,
    reset_token VARCHAR,
    reset_token_created TIMESTAMP,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    deactivated_at TIMESTAMP,
    deactivated_by VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP
);
```

Add migration list below `_V3_TO_V4_MIGRATIONS`:

```python
_V4_TO_V5_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS deactivated_by VARCHAR",
]
```

Extend the migration dispatch in `_ensure_schema` (inside the `else` branch at the current `if current < 4:` block):

```python
            if current < 5:
                for sql in _V4_TO_V5_MIGRATIONS:
                    conn.execute(sql)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_user_management.py::test_schema_v5_adds_active_column -v`
Expected: PASS.

- [ ] **Step 4b: Backfill test — upgrade from v4 with existing rows**

Append to `tests/test_user_management.py`:

```python
def test_schema_v5_backfill_keeps_existing_users_active(fresh_db):
    """Simulate upgrading from v4: insert a user pre-migration, verify active=TRUE afterwards."""
    import uuid
    import duckdb as _duckdb
    from pathlib import Path

    # 1. Create a v4-era DB by hand.
    db_dir = Path(fresh_db) / "state"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "system.duckdb"
    conn = _duckdb.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TIMESTAMP DEFAULT current_timestamp)")
        conn.execute("INSERT INTO schema_version (version) VALUES (4)")
        conn.execute("""CREATE TABLE users (
            id VARCHAR PRIMARY KEY, email VARCHAR UNIQUE NOT NULL,
            name VARCHAR, role VARCHAR DEFAULT 'analyst',
            password_hash VARCHAR, setup_token VARCHAR,
            setup_token_created TIMESTAMP, reset_token VARCHAR,
            reset_token_created TIMESTAMP,
            created_at TIMESTAMP DEFAULT current_timestamp, updated_at TIMESTAMP)""")
        uid = str(uuid.uuid4())
        conn.execute("INSERT INTO users (id, email, name, role) VALUES (?, 'pre@v4', 'Pre', 'admin')", [uid])
    finally:
        conn.close()

    # 2. Now let the app open it — schema should migrate to v5 and backfill active=TRUE.
    from src.db import get_system_db, close_system_db, get_schema_version
    close_system_db()
    conn = get_system_db()
    try:
        assert get_schema_version(conn) >= 5
        row = conn.execute("SELECT email, active FROM users WHERE email = 'pre@v4'").fetchone()
        assert row is not None
        assert row[1] is True
    finally:
        conn.close()
        close_system_db()
```

Run: `pytest tests/test_user_management.py::test_schema_v5_backfill_keeps_existing_users_active -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_user_management.py
git commit -m "feat(db): schema v5 — users.active + deactivated_at/by (#11)"
```

### Task 1.2: UserRepository — update supports active/deactivated_*, add count_admins & list_all

**Files:**
- Modify: `src/repositories/users.py:49-58` (extend `update` allowed fields)
- Modify: `src/repositories/users.py:27-32` (list_all already exists; keep)
- Add: `count_admins()` method

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_management.py — append

def test_repository_update_accepts_active(fresh_db):
    import uuid
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        uid = str(uuid.uuid4())
        repo.create(id=uid, email="a@b.c", name="A", role="analyst")
        repo.update(id=uid, active=False, deactivated_by="admin-uuid")
        row = repo.get_by_id(uid)
        assert row["active"] is False
        assert row["deactivated_by"] == "admin-uuid"
    finally:
        conn.close()
        close_system_db()


def test_repository_count_admins(fresh_db):
    import uuid
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        assert repo.count_admins() == 0
        repo.create(id=str(uuid.uuid4()), email="a@b.c", name="A", role="admin")
        repo.create(id=str(uuid.uuid4()), email="b@b.c", name="B", role="analyst")
        assert repo.count_admins() == 1
    finally:
        conn.close()
        close_system_db()
```

- [ ] **Step 2: Run — verify fail**

Run: `pytest tests/test_user_management.py -v -k "repository"`
Expected: FAIL.

- [ ] **Step 3: Extend repository**

In `src/repositories/users.py`, update `allowed` set and set special handling for `active` + add `count_admins`. Also add `deactivated_at`:

```python
    def update(self, id: str, **kwargs) -> None:
        allowed = {
            "email", "name", "role", "password_hash", "setup_token",
            "setup_token_created", "reset_token", "reset_token_created",
            "active", "deactivated_at", "deactivated_by",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [id]
        self.conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)

    def count_admins(self, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        if active_only:
            sql += " AND COALESCE(active, TRUE) = TRUE"
        result = self.conn.execute(sql).fetchone()
        return int(result[0]) if result else 0
```

- [ ] **Step 4: Run — verify pass**

Run: `pytest tests/test_user_management.py -v -k "repository"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repositories/users.py tests/test_user_management.py
git commit -m "feat(users): repository supports active flag + count_admins (#11)"
```

### Task 1.3: API — PATCH /api/users/{id} + audit

**Files:**
- Modify: `app/api/users.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_user_management.py — append (use existing API test fixtures pattern)

from fastapi.testclient import TestClient


@pytest.fixture
def app_client(fresh_db, monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    from app.main import app
    return TestClient(app)


def _seed_admin(fresh_db):
    """Create an admin user and return (id, bearer_token)."""
    import uuid
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@test", name="Admin", role="admin")
        token = create_access_token(user_id=uid, email="admin@test", role="admin")
        return uid, token
    finally:
        conn.close()


def test_patch_user_updates_role(app_client, fresh_db):
    import uuid
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    admin_id, token = _seed_admin(fresh_db)
    target_id = str(uuid.uuid4())
    conn = get_system_db()
    try:
        UserRepository(conn).create(id=target_id, email="x@test", name="X", role="viewer")
    finally:
        conn.close()

    resp = app_client.patch(
        f"/api/users/{target_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"role": "analyst", "name": "X2"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "analyst"
    assert data["name"] == "X2"
```

- [ ] **Step 2: Run — verify fail**

Run: `pytest tests/test_user_management.py::test_patch_user_updates_role -v`
Expected: FAIL (405 Method Not Allowed or 404).

- [ ] **Step 3: Implement PATCH in `app/api/users.py`**

Replace entire file with extended version (additions bolded conceptually):

```python
"""User management endpoints (#11)."""

import uuid
from datetime import datetime, timezone
from typing import Optional, List

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from argon2 import PasswordHasher

from app.auth.dependencies import require_role, Role, _get_db
from src.repositories.users import UserRepository
from src.repositories.audit import AuditRepository

router = APIRouter(prefix="/api/users", tags=["users"])


def _audit(conn: duckdb.DuckDBPyConnection, actor_id: str, action: str, target_id: str, params: Optional[dict] = None) -> None:
    try:
        AuditRepository(conn).log(
            user_id=actor_id,
            action=action,
            resource=f"user:{target_id}",
            params=params,
        )
    except Exception:
        pass  # never block the endpoint on audit failure


class CreateUserRequest(BaseModel):
    email: str
    name: str
    role: str = "analyst"


class UpdateUserRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None


class SetPasswordRequest(BaseModel):
    password: str


class UserResponse(BaseModel):
    id: str
    email: str
    name: Optional[str]
    role: str
    active: bool = True
    created_at: Optional[str]
    deactivated_at: Optional[str] = None


def _to_response(u: dict) -> UserResponse:
    return UserResponse(
        id=u["id"],
        email=u["email"],
        name=u.get("name"),
        role=u["role"],
        active=bool(u.get("active", True)),
        created_at=str(u.get("created_at", "")),
        deactivated_at=str(u["deactivated_at"]) if u.get("deactivated_at") else None,
    )


@router.get("", response_model=List[UserResponse])
async def list_users(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return [_to_response(u) for u in UserRepository(conn).list_all()]


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    payload: CreateUserRequest,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    if repo.get_by_email(payload.email):
        raise HTTPException(status_code=409, detail="User with this email already exists")
    user_id = str(uuid.uuid4())
    repo.create(id=user_id, email=payload.email, name=payload.name, role=payload.role)
    _audit(conn, user["id"], "user.create", user_id, {"email": payload.email, "role": payload.role})
    created = repo.get_by_id(user_id)
    return _to_response(created)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    updates: dict = {}
    if payload.name is not None:
        updates["name"] = payload.name
    if payload.role is not None:
        # Validate role is a known value
        try:
            Role(payload.role)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown role: {payload.role}")
        # Protect: don't let admin demote themselves if they are the last admin
        if (
            target["id"] == user["id"]
            and target["role"] == "admin"
            and payload.role != "admin"
            and repo.count_admins(active_only=True) <= 1
        ):
            raise HTTPException(status_code=409, detail="Cannot demote the last active admin")
        updates["role"] = payload.role
    if payload.active is not None:
        # Protect: cannot self-deactivate
        if target["id"] == user["id"] and payload.active is False:
            raise HTTPException(status_code=409, detail="Cannot deactivate yourself")
        # Protect: cannot deactivate the last active admin
        if (
            target.get("role") == "admin"
            and payload.active is False
            and repo.count_admins(active_only=True) <= 1
        ):
            raise HTTPException(status_code=409, detail="Cannot deactivate the last active admin")
        updates["active"] = payload.active
        if payload.active is False:
            updates["deactivated_at"] = datetime.now(timezone.utc)
            updates["deactivated_by"] = user["id"]
        else:
            updates["deactivated_at"] = None
            updates["deactivated_by"] = None

    if updates:
        repo.update(id=user_id, **updates)
        _audit(conn, user["id"], "user.update", user_id, {k: v for k, v in updates.items() if k != "deactivated_at"})
    return _to_response(repo.get_by_id(user_id))


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target["id"] == user["id"]:
        raise HTTPException(status_code=409, detail="Cannot delete yourself")
    if target.get("role") == "admin" and repo.count_admins(active_only=True) <= 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last active admin")
    repo.delete(user_id)
    _audit(conn, user["id"], "user.delete", user_id, {"email": target["email"]})


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Generate a reset token and (best-effort) email it to the user."""
    import secrets
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    token = secrets.token_urlsafe(32)
    repo.update(
        id=user_id,
        reset_token=token,
        reset_token_created=datetime.now(timezone.utc),
    )
    _audit(conn, user["id"], "user.reset_password", user_id, {"email": target["email"]})
    # Best-effort email
    email_sent = False
    try:
        from app.auth.providers.email import _send_email, is_available
        if is_available():
            _send_email(target["email"], token)
            email_sent = True
    except Exception:
        pass
    return {"reset_token": token, "email_sent": email_sent}


@router.post("/{user_id}/set-password", status_code=204)
async def set_password(
    user_id: str,
    payload: SetPasswordRequest,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if not payload.password or len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    ph = PasswordHasher()
    repo.update(id=user_id, password_hash=ph.hash(payload.password))
    _audit(conn, user["id"], "user.set_password", user_id, {"email": target["email"]})


@router.post("/{user_id}/deactivate", response_model=UserResponse)
async def deactivate_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return await update_user(
        user_id=user_id,
        payload=UpdateUserRequest(active=False),
        request=request, user=user, conn=conn,
    )


@router.post("/{user_id}/activate", response_model=UserResponse)
async def activate_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return await update_user(
        user_id=user_id,
        payload=UpdateUserRequest(active=True),
        request=request, user=user, conn=conn,
    )
```

- [ ] **Step 4: Run — verify pass**

Run: `pytest tests/test_user_management.py::test_patch_user_updates_role -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/users.py tests/test_user_management.py
git commit -m "feat(api): user PATCH/reset-password/set-password/activate/deactivate (#11)"
```

### Task 1.4: Safeguards — self-deactivate / last-admin protection

**Files:**
- Test: `tests/test_user_management.py` — append

- [ ] **Step 1: Failing tests**

```python
# tests/test_user_management.py — append

def test_cannot_self_deactivate(app_client, fresh_db):
    admin_id, token = _seed_admin(fresh_db)
    resp = app_client.patch(
        f"/api/users/{admin_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"active": False},
    )
    assert resp.status_code == 409
    assert "yourself" in resp.json()["detail"].lower()


def test_cannot_delete_last_admin(app_client, fresh_db):
    admin_id, token = _seed_admin(fresh_db)
    # Create a non-admin so we have ≥2 users, but admin is still the only admin.
    resp = app_client.post(
        "/api/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "x@test", "name": "X", "role": "viewer"},
    )
    x_id = resp.json()["id"]
    # Try deleting the admin.
    resp = app_client.delete(
        f"/api/users/{admin_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert "last" in resp.json()["detail"].lower()


def test_cannot_deactivate_last_admin(app_client, fresh_db):
    admin_id, token = _seed_admin(fresh_db)
    # Promote a helper admin then deactivate self via them? Simpler: ensure demotion rule.
    # Create a second user and try to demote the current admin via PATCH.
    resp = app_client.post(
        "/api/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "y@test", "name": "Y", "role": "viewer"},
    )
    y_id = resp.json()["id"]
    # Try to demote self (admin → viewer) while only admin — should fail.
    resp = app_client.patch(
        f"/api/users/{admin_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"role": "viewer"},
    )
    assert resp.status_code == 409
    assert "admin" in resp.json()["detail"].lower()
```

- [ ] **Step 2: Run**

Run: `pytest tests/test_user_management.py -v -k "safeguard or cannot"`
Expected: PASS (implementation already includes safeguards in Task 1.3).

- [ ] **Step 3: Commit tests**

```bash
git add tests/test_user_management.py
git commit -m "test(api): safeguard tests for self-deactivate and last admin (#11)"
```

### Task 1.5: Active-flag enforcement in get_current_user

**Files:**
- Modify: `app/auth/dependencies.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_user_management.py — append

def test_deactivated_user_cannot_authenticate(app_client, fresh_db):
    """A deactivated user's old JWT must be rejected."""
    import uuid
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@test", name="U", role="analyst")
        token = create_access_token(user_id=uid, email="u@test", role="analyst")
        UserRepository(conn).update(id=uid, active=False)
    finally:
        conn.close()

    resp = app_client.get(
        "/api/users",  # any authenticated endpoint
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    # Deactivated — must not succeed.
    assert resp.status_code in (401, 403)
```

- [ ] **Step 2: Run — verify fail**

Run: `pytest tests/test_user_management.py::test_deactivated_user_cannot_authenticate -v`
Expected: may PASS (if user happens to be admin denied on role) or FAIL — depending on seed. If it passes here spuriously, adjust test to use 403-guaranteed endpoint. For safety, check any auth'd endpoint; 401 from inactive check is intended.

- [ ] **Step 3: Add active check in `get_current_user`**

In `app/auth/dependencies.py`, after repo lookup:

```python
    repo = UserRepository(conn)
    user = repo.get_by_id(payload.get("sub", ""))
    if not user:
        _fail("User not found")
    if not bool(user.get("active", True)):
        _fail("Account deactivated")
    return user
```

- [ ] **Step 4: Run full suite**

Run: `pytest tests/ -x --timeout=30`
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add app/auth/dependencies.py tests/test_user_management.py
git commit -m "feat(auth): reject requests from deactivated users (#11)"
```

### Task 1.6: CLI commands — 5 new admin subcommands

**Files:**
- Modify: `cli/commands/admin.py`
- Test: `tests/test_cli_admin.py` — append

- [ ] **Step 1: Failing test**

```python
# tests/test_cli_admin.py — append

def test_admin_set_role_invokes_patch(monkeypatch):
    """`da admin set-role` sends PATCH to /api/users/{id} with role."""
    import httpx
    from cli.commands.admin import admin_app
    from typer.testing import CliRunner

    captured = {}

    def fake_patch(path, json=None, **kwargs):
        captured["path"] = path
        captured["json"] = json
        return httpx.Response(200, json={
            "id": "abc", "email": "x@y.z", "name": "X",
            "role": json.get("role") if json else "viewer",
            "active": True, "created_at": "", "deactivated_at": None,
        })

    from cli import client as cli_client
    monkeypatch.setattr(cli_client, "api_patch", fake_patch, raising=False)
    # patch admin.api_patch too since admin.py imports names
    from cli.commands import admin as admin_mod
    monkeypatch.setattr(admin_mod, "api_patch", fake_patch, raising=False)

    runner = CliRunner()
    result = runner.invoke(admin_app, ["set-role", "abc", "analyst"])
    assert result.exit_code == 0
    assert captured["path"] == "/api/users/abc"
    assert captured["json"] == {"role": "analyst"}
```

- [ ] **Step 2: Run — verify fail**

Run: `pytest tests/test_cli_admin.py::test_admin_set_role_invokes_patch -v`
Expected: FAIL (no `set-role` command).

- [ ] **Step 3: Add `api_patch` to `cli/client.py`**

```python
def api_patch(path: str, **kwargs) -> httpx.Response:
    with get_client() as client:
        return client.patch(path, **kwargs)
```

- [ ] **Step 4: Add CLI commands to `cli/commands/admin.py`**

Append at the end of file:

```python
from cli.client import api_patch


@admin_app.command("set-role")
def set_role(
    user_ref: str = typer.Argument(..., help="User id or email"),
    role: str = typer.Argument(..., help="viewer | analyst | km_admin | admin"),
):
    """Set a user's role."""
    uid = _resolve_user_id(user_ref)
    resp = api_patch(f"/api/users/{uid}", json={"role": role})
    _print_user_result(resp, f"Updated role for {user_ref} → {role}")


@admin_app.command("deactivate")
def deactivate(user_ref: str = typer.Argument(..., help="User id or email")):
    """Deactivate a user (blocks login, existing tokens also rejected)."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/deactivate")
    _print_user_result(resp, f"Deactivated {user_ref}")


@admin_app.command("activate")
def activate(user_ref: str = typer.Argument(..., help="User id or email")):
    """Re-activate a deactivated user."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/activate")
    _print_user_result(resp, f"Activated {user_ref}")


@admin_app.command("reset-password")
def reset_password(user_ref: str = typer.Argument(..., help="User id or email")):
    """Generate a reset token (emailed if SMTP/SendGrid configured)."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/reset-password")
    if resp.status_code == 200:
        data = resp.json()
        typer.echo(f"Reset token: {data['reset_token']}")
        typer.echo(f"Email sent: {data['email_sent']}")
    else:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)


@admin_app.command("set-password")
def set_password(
    user_ref: str = typer.Argument(..., help="User id or email"),
    password: str = typer.Option(
        ..., prompt=True, hide_input=True, confirmation_prompt=True,
        help="New password (hidden input)",
    ),
):
    """Set a user's password directly (force-reset flow)."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/set-password", json={"password": password})
    if resp.status_code == 204:
        typer.echo(f"Password set for {user_ref}")
    else:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)


def _resolve_user_id(ref: str) -> str:
    """Accept either a UUID or an email; look up email → id via list."""
    if "@" not in ref:
        return ref
    resp = api_get("/api/users")
    if resp.status_code != 200:
        typer.echo(f"Could not list users: {resp.text}", err=True)
        raise typer.Exit(1)
    for u in resp.json():
        if u.get("email") == ref:
            return u["id"]
    typer.echo(f"User not found: {ref}", err=True)
    raise typer.Exit(1)


def _print_user_result(resp, ok_msg: str) -> None:
    if resp.status_code in (200, 204):
        typer.echo(ok_msg)
    else:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"Failed: {detail}", err=True)
        raise typer.Exit(1)
```

Also extend `list-users` output (replace body):

```python
@admin_app.command("list-users")
def list_users(as_json: bool = typer.Option(False, "--json")):
    """List all users."""
    resp = api_get("/api/users")
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)
    users = resp.json()
    if as_json:
        typer.echo(json.dumps(users, indent=2))
    else:
        for u in users:
            status_str = "active" if u.get("active", True) else "DEACTIVATED"
            typer.echo(
                f"  {u['email']:30s} role={u['role']:10s} {status_str:12s} id={u['id'][:8]}"
            )
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_cli_admin.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add cli/client.py cli/commands/admin.py tests/test_cli_admin.py
git commit -m "feat(cli): da admin set-role/activate/deactivate/reset-password/set-password (#11)"
```

### Task 1.7: UI — /admin/users page

**Files:**
- Create: `app/web/templates/admin_users.html`
- Modify: `app/web/router.py` — add route
- Modify: `app/web/templates/dashboard.html` — nav link (admins only)

- [ ] **Step 1: Create template**

Create `app/web/templates/admin_users.html`:

```html
{% extends "base.html" %}
{% block title %}User management — {{ config.INSTANCE_NAME }}{% endblock %}

{% block content %}
<div class="container">
  <div class="d-flex justify-content-between align-items-center mb-3">
    <h2>User management</h2>
    <button class="btn btn-primary" onclick="openCreateModal()">+ Add user</button>
  </div>

  <table class="table table-striped" id="users-table">
    <thead>
      <tr>
        <th>Email</th><th>Name</th><th>Role</th><th>Active</th>
        <th>Created</th><th>Deactivated</th><th>Actions</th>
      </tr>
    </thead>
    <tbody>
      <!-- rows rendered by JS -->
    </tbody>
  </table>
</div>

<div class="modal" id="create-modal" style="display:none;">
  <div class="modal-content">
    <h3>Add user</h3>
    <label>Email <input id="new-email" type="email" required></label>
    <label>Name <input id="new-name" type="text"></label>
    <label>Role
      <select id="new-role">
        <option value="viewer">viewer</option>
        <option value="analyst" selected>analyst</option>
        <option value="km_admin">km_admin</option>
        <option value="admin">admin</option>
      </select>
    </label>
    <button class="btn btn-primary" onclick="createUser()">Create</button>
    <button class="btn btn-secondary" onclick="closeCreateModal()">Cancel</button>
  </div>
</div>

<script>
const API = "/api/users";

function fmtDate(s) { return s ? s.slice(0, 19).replace("T", " ") : ""; }

async function loadUsers() {
  const r = await fetch(API, {credentials: "include"});
  if (!r.ok) { alert("Failed to load users: " + r.status); return; }
  const users = await r.json();
  const tbody = document.querySelector("#users-table tbody");
  tbody.innerHTML = "";
  for (const u of users) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${u.email}</td>
      <td>${u.name || ""}</td>
      <td>
        <select onchange="setRole('${u.id}', this.value)">
          ${["viewer","analyst","km_admin","admin"].map(r =>
            `<option value="${r}" ${r===u.role?"selected":""}>${r}</option>`).join("")}
        </select>
      </td>
      <td>
        <input type="checkbox" ${u.active?"checked":""}
          onchange="toggleActive('${u.id}', this.checked)">
      </td>
      <td>${fmtDate(u.created_at)}</td>
      <td>${fmtDate(u.deactivated_at)}</td>
      <td>
        <button class="btn btn-sm" onclick="resetPassword('${u.id}')">Reset</button>
        <button class="btn btn-sm" onclick="setPassword('${u.id}')">Set pwd</button>
        <button class="btn btn-sm btn-danger" onclick="delUser('${u.id}','${u.email}')">Delete</button>
      </td>`;
    tbody.appendChild(tr);
  }
}

async function setRole(id, role) {
  const r = await fetch(`${API}/${id}`, {
    method: "PATCH", credentials: "include",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({role}),
  });
  if (!r.ok) { alert("Failed: " + (await r.text())); }
  loadUsers();
}

async function toggleActive(id, active) {
  const path = active ? "activate" : "deactivate";
  const r = await fetch(`${API}/${id}/${path}`, {method: "POST", credentials: "include"});
  if (!r.ok) { alert("Failed: " + (await r.text())); }
  loadUsers();
}

async function resetPassword(id) {
  if (!confirm("Generate a password reset token?")) return;
  const r = await fetch(`${API}/${id}/reset-password`, {method: "POST", credentials: "include"});
  const data = await r.json();
  if (!r.ok) { alert("Failed: " + data.detail); return; }
  alert(`Reset token: ${data.reset_token}\nEmail sent: ${data.email_sent}`);
}

async function setPassword(id) {
  const pwd = prompt("New password (min 8 chars):");
  if (!pwd) return;
  const r = await fetch(`${API}/${id}/set-password`, {
    method: "POST", credentials: "include",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({password: pwd}),
  });
  if (!r.ok) { alert("Failed: " + (await r.text())); return; }
  alert("Password set.");
}

async function delUser(id, email) {
  if (!confirm(`Delete ${email}? This cannot be undone.`)) return;
  const r = await fetch(`${API}/${id}`, {method: "DELETE", credentials: "include"});
  if (!r.ok) { alert("Failed: " + (await r.text())); return; }
  loadUsers();
}

function openCreateModal() { document.getElementById("create-modal").style.display = "block"; }
function closeCreateModal() { document.getElementById("create-modal").style.display = "none"; }

async function createUser() {
  const email = document.getElementById("new-email").value;
  const name = document.getElementById("new-name").value;
  const role = document.getElementById("new-role").value;
  const r = await fetch(API, {
    method: "POST", credentials: "include",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({email, name: name || email.split("@")[0], role}),
  });
  if (!r.ok) { alert("Failed: " + (await r.text())); return; }
  closeCreateModal();
  loadUsers();
}

loadUsers();
</script>
{% endblock %}
```

- [ ] **Step 2: Add route in `app/web/router.py`**

After the existing `admin_permissions_page` route:

```python
@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    user: dict = Depends(require_role(Role.ADMIN)),
):
    """Admin page for user management."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_users.html", ctx)
```

- [ ] **Step 3: Add nav link in `app/web/templates/dashboard.html`**

Find the existing admin nav block (search for "admin/tables" or "admin/permissions") and add `/admin/users` as a sibling:

```html
{% if user.role == 'admin' %}
  <a class="nav-link" href="/admin/users">Users</a>
{% endif %}
```

(If no dashboard admin nav block exists, skip — the page is still reachable by URL and tests will cover it.)

- [ ] **Step 4: Test the route renders for admin**

Add to `tests/test_user_management.py`:

```python
def test_admin_users_page_renders_for_admin(app_client, fresh_db):
    admin_id, token = _seed_admin(fresh_db)
    resp = app_client.get(
        "/admin/users",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "User management" in resp.text


def test_admin_users_page_denies_non_admin(app_client, fresh_db):
    import uuid
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="a@test", name="A", role="analyst")
        token = create_access_token(user_id=uid, email="a@test", role="analyst")
    finally:
        conn.close()
    resp = app_client.get(
        "/admin/users",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
        follow_redirects=False,
    )
    # HTML request to admin-only page → 302 (to /login) for non-admin per Phase 0
    assert resp.status_code in (302, 403)
```

- [ ] **Step 5: Run**

Run: `pytest tests/test_user_management.py -v -k "admin_users_page"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/web/router.py app/web/templates/admin_users.html app/web/templates/dashboard.html tests/test_user_management.py
git commit -m "feat(ui): /admin/users management page (#11)"
```

### Task 1.8: Phase 1 integration — full test suite + review

- [ ] **Step 1: Run full suite**

Run: `pytest tests/ --timeout=30`
Expected: all green.

- [ ] **Step 2: Manual smoke**

Run locally: `uvicorn app.main:app --reload`, visit `/admin/users` as admin, create+edit+delete a test user, verify deactivated user's token is rejected on an API call.

- [ ] **Step 3: Request code review**

Dispatch `superpowers:code-reviewer` agent with diff of `git log --oneline main..HEAD` scope; fix any blockers.

- [ ] **Step 4: Optional squash/rebase**

If commits are noisy, rebase-interactive to a clean sequence. Otherwise leave as-is.

---

## Phase 2 — Personal Access Tokens (#12)

Scope: schema v6 s tabulkou `personal_access_tokens`, JWT rozšíření (`typ`, `jti`), endpointy `/auth/tokens` (session-only) + admin variant, CLI (`da auth token create|list|revoke`), profile UI s one-time revealem, aktualizace `cli/skills/security.md`.

### File Structure

- Modify: `src/db.py` — schema v6 + `personal_access_tokens`
- Create: `src/repositories/access_tokens.py`
- Modify: `app/auth/jwt.py` — `typ`, `jti` verification for PATs
- Modify: `app/auth/dependencies.py` — reject revoked/expired PATs, update `last_used_at`
- Create: `app/api/tokens.py` — `/auth/tokens` CRUD (session-only)
- Modify: `app/main.py` — register tokens router
- Create: `cli/commands/tokens.py`
- Modify: `cli/main.py` — add `token` sub-typer under `auth`
- Modify: `cli/commands/auth.py` — add `token` subcommand group hook
- Create: `app/web/templates/profile.html` (or `profile_tokens.html`)
- Modify: `app/web/router.py` — `/profile` route
- Modify: `cli/skills/security.md` — fix 24h vs 30d mismatch + add PAT section
- Test: `tests/test_pat.py` (new)

### Task 2.1: Schema v6 — personal_access_tokens table

**Files:**
- Modify: `src/db.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_pat.py
import os
import tempfile
import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def test_schema_v6_creates_pat_table(fresh_db):
    from src.db import get_system_db, get_schema_version, close_system_db
    conn = get_system_db()
    try:
        cols = conn.execute("PRAGMA table_info(personal_access_tokens)").fetchall()
        col_names = [c[1] for c in cols]
        for expected in ("id", "user_id", "name", "token_hash", "prefix",
                         "scopes", "created_at", "expires_at", "last_used_at", "revoked_at"):
            assert expected in col_names
        assert get_schema_version(conn) >= 6
    finally:
        conn.close()
        close_system_db()
```

- [ ] **Step 2: Run — fail**

Run: `pytest tests/test_pat.py::test_schema_v6_creates_pat_table -v`

- [ ] **Step 3: Bump schema and add DDL**

`src/db.py`:

```python
SCHEMA_VERSION = 6
```

Append to `_SYSTEM_SCHEMA` just before the closing `"""`:

```python
CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id           VARCHAR PRIMARY KEY,
    user_id      VARCHAR NOT NULL,
    name         VARCHAR NOT NULL,
    token_hash   VARCHAR NOT NULL,
    prefix       VARCHAR NOT NULL,
    scopes       VARCHAR,
    created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
    expires_at   TIMESTAMP,
    last_used_at TIMESTAMP,
    revoked_at   TIMESTAMP
);
```

Add migration list:

```python
_V5_TO_V6_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS personal_access_tokens (
        id           VARCHAR PRIMARY KEY,
        user_id      VARCHAR NOT NULL,
        name         VARCHAR NOT NULL,
        token_hash   VARCHAR NOT NULL,
        prefix       VARCHAR NOT NULL,
        scopes       VARCHAR,
        created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
        expires_at   TIMESTAMP,
        last_used_at TIMESTAMP,
        revoked_at   TIMESTAMP
    )
    """,
]
```

Extend dispatch:

```python
            if current < 6:
                for sql in _V5_TO_V6_MIGRATIONS:
                    conn.execute(sql)
```

- [ ] **Step 4: Run — pass**

Run: `pytest tests/test_pat.py::test_schema_v6_creates_pat_table -v`

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_pat.py
git commit -m "feat(db): schema v6 — personal_access_tokens (#12)"
```

### Task 2.2: AccessTokenRepository

**Files:**
- Create: `src/repositories/access_tokens.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_pat.py — append

def test_access_token_repo_create_and_lookup(fresh_db):
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.access_tokens import AccessTokenRepository

    conn = get_system_db()
    try:
        repo = AccessTokenRepository(conn)
        token_id = str(uuid.uuid4())
        raw = "abcdefgh" + "x" * 32
        repo.create(
            id=token_id,
            user_id="u1",
            name="laptop",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            prefix=raw[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        row = repo.get_by_id(token_id)
        assert row is not None
        assert row["name"] == "laptop"
        assert row["prefix"] == "abcdefgh"
        assert row["revoked_at"] is None

        rows = repo.list_for_user("u1")
        assert len(rows) == 1

        repo.revoke(token_id)
        assert repo.get_by_id(token_id)["revoked_at"] is not None
    finally:
        conn.close()
        close_system_db()


def test_access_token_repo_mark_used(fresh_db):
    import hashlib, uuid
    from datetime import datetime, timezone
    from src.db import get_system_db, close_system_db
    from src.repositories.access_tokens import AccessTokenRepository

    conn = get_system_db()
    try:
        repo = AccessTokenRepository(conn)
        tid = str(uuid.uuid4())
        repo.create(id=tid, user_id="u1", name="x",
                    token_hash=hashlib.sha256(b"r").hexdigest(), prefix="rrrrrrrr")
        assert repo.get_by_id(tid)["last_used_at"] is None
        repo.mark_used(tid)
        assert repo.get_by_id(tid)["last_used_at"] is not None
    finally:
        conn.close()
        close_system_db()
```

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Implement repository**

Create `src/repositories/access_tokens.py`:

```python
"""Repository for personal access tokens (#12)."""

from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class AccessTokenRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def create(
        self,
        id: str,
        user_id: str,
        name: str,
        token_hash: str,
        prefix: str,
        expires_at: Optional[datetime] = None,
        scopes: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO personal_access_tokens
            (id, user_id, name, token_hash, prefix, scopes, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [id, user_id, name, token_hash, prefix, scopes,
             datetime.now(timezone.utc), expires_at],
        )

    def get_by_id(self, token_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM personal_access_tokens WHERE id = ?", [token_id]
        ).fetchone()
        return self._row_to_dict(result)

    def list_for_user(self, user_id: str, include_revoked: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM personal_access_tokens WHERE user_id = ?"
        if not include_revoked:
            sql += " AND revoked_at IS NULL"
        sql += " ORDER BY created_at DESC"
        rows = self.conn.execute(sql, [user_id]).fetchall()
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def list_all(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM personal_access_tokens ORDER BY created_at DESC"
        ).fetchall()
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def revoke(self, token_id: str) -> None:
        self.conn.execute(
            "UPDATE personal_access_tokens SET revoked_at = ? WHERE id = ?",
            [datetime.now(timezone.utc), token_id],
        )

    def delete(self, token_id: str) -> None:
        self.conn.execute("DELETE FROM personal_access_tokens WHERE id = ?", [token_id])

    def mark_used(self, token_id: str) -> None:
        self.conn.execute(
            "UPDATE personal_access_tokens SET last_used_at = ? WHERE id = ?",
            [datetime.now(timezone.utc), token_id],
        )
```

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit**

```bash
git add src/repositories/access_tokens.py tests/test_pat.py
git commit -m "feat(users): access_tokens repository (#12)"
```

### Task 2.3: JWT — `typ` field, helper for PAT vs session

**Files:**
- Modify: `app/auth/jwt.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_pat.py — append

def test_pat_token_carries_typ_claim(fresh_db):
    from app.auth.jwt import create_access_token, verify_token
    token = create_access_token(
        user_id="u1", email="u@test", role="analyst",
        token_id="deadbeef-1234", typ="pat",
    )
    payload = verify_token(token)
    assert payload["typ"] == "pat"
    assert payload["jti"] == "deadbeef-1234"


def test_session_token_defaults_typ(fresh_db):
    from app.auth.jwt import create_access_token, verify_token
    token = create_access_token(user_id="u1", email="u@test", role="analyst")
    payload = verify_token(token)
    # Default typ is "session".
    assert payload.get("typ") == "session"
```

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Extend `create_access_token`**

Replace the function body in `app/auth/jwt.py`:

```python
def create_access_token(
    user_id: str,
    email: str,
    role: str = "analyst",
    expires_delta: Optional[timedelta] = None,
    token_id: Optional[str] = None,
    typ: str = "session",
) -> str:
    """Create a JWT. `typ` is "session" (interactive login) or "pat" (long-lived)."""
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    )
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "typ": typ,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": token_id or uuid.uuid4().hex,
    }
    return jwt.encode(payload, _get_cached_secret_key(), algorithm=ALGORITHM)
```

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit**

```bash
git add app/auth/jwt.py tests/test_pat.py
git commit -m "feat(auth): JWT carries typ (session|pat) and explicit jti (#12)"
```

### Task 2.4: Auth dependency — reject revoked/expired PATs, update last_used_at

**Files:**
- Modify: `app/auth/dependencies.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_pat.py — append

def test_revoked_pat_is_rejected(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        token_id = str(uuid.uuid4())
        raw = "secretXX" + "a" * 32
        AccessTokenRepository(conn).create(
            id=token_id, user_id=uid, name="ci",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            prefix=raw[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        jwt_token = create_access_token(
            user_id=uid, email="u@t", role="admin", token_id=token_id, typ="pat",
        )
        # Revoke
        AccessTokenRepository(conn).revoke(token_id)
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {jwt_token}", "Accept": "application/json"},
    )
    assert resp.status_code == 401


def test_expired_pat_is_rejected_from_db(fresh_db):
    """A PAT with a past expires_at in DB is rejected even if JWT exp is in future."""
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        tid = str(uuid.uuid4())
        # Past-dated expiry in DB
        AccessTokenRepository(conn).create(
            id=tid, user_id=uid, name="stale",
            token_hash=hashlib.sha256(b"whatever").hexdigest(), prefix=tid.replace("-","")[:8],
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        # JWT with much longer TTL so signature-level `exp` would pass
        pat = create_access_token(
            user_id=uid, email="u@t", role="admin",
            token_id=tid, typ="pat",
            expires_delta=timedelta(days=365),
        )
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"},
    )
    assert resp.status_code == 401
```

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Add PAT validation in `app/auth/dependencies.py`**

Inside `get_current_user`, after `payload = verify_token(token)`:

```python
    # PAT validation: check it's not revoked / expired / unknown in DB.
    if payload.get("typ") == "pat":
        from datetime import datetime, timezone
        import hashlib
        from src.repositories.access_tokens import AccessTokenRepository
        tokens_repo = AccessTokenRepository(conn)
        record = tokens_repo.get_by_id(payload.get("jti", ""))
        if not record:
            _fail("Token unknown")
        if record.get("revoked_at") is not None:
            _fail("Token revoked")
        exp_at = record.get("expires_at")
        if exp_at is not None:
            if isinstance(exp_at, str):
                exp_at = datetime.fromisoformat(exp_at)
            if exp_at.tzinfo is None:
                exp_at = exp_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_at:
                _fail("Token expired")
        # Defense-in-depth: stored token_hash must match sha256(bearer JWT).
        # Protects against a forged-but-unrevoked JWT using a stolen key.
        stored_hash = record.get("token_hash")
        if stored_hash:
            actual = hashlib.sha256(token.encode()).hexdigest()
            if actual != stored_hash:
                _fail("Token mismatch")
        # Record last_used_at synchronously — acceptable cost; can batch later.
        try:
            tokens_repo.mark_used(payload["jti"])
        except Exception:
            pass
```

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit**

```bash
git add app/auth/dependencies.py tests/test_pat.py
git commit -m "feat(auth): reject revoked/expired PATs; update last_used_at (#12)"
```

### Task 2.5: API — /auth/tokens CRUD (session-only)

**Files:**
- Create: `app/api/tokens.py`
- Modify: `app/main.py` — include router
- Modify: `app/auth/dependencies.py` — add `require_session_token` dep

- [ ] **Step 1: Failing test**

```python
# tests/test_pat.py — append

def test_create_pat_returns_raw_once(fresh_db):
    from fastapi.testclient import TestClient
    import uuid
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        sess_token = create_access_token(user_id=uid, email="u@t", role="admin")  # typ=session
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.post(
        "/auth/tokens",
        headers={"Authorization": f"Bearer {sess_token}"},
        json={"name": "laptop", "expires_in_days": 30},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "laptop"
    assert "token" in data and data["token"]  # raw token returned exactly once

    # Listing returns prefix, never raw.
    # Prefix is derived from the token id (jti), not the JWT string, to avoid
    # all tokens having the useless "eyJhbGci" JWT-header prefix.
    list_resp = client.get(
        "/auth/tokens", headers={"Authorization": f"Bearer {sess_token}"},
    )
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert len(rows) == 1
    assert "token" not in rows[0]
    assert rows[0]["prefix"] == data["prefix"]
    assert len(rows[0]["prefix"]) == 8
    assert not data["prefix"].startswith("eyJ")  # regression: not the JWT header


def test_pat_cannot_create_pat(fresh_db):
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        tid = str(uuid.uuid4())
        raw = "abcdefgh" + "x" * 32
        AccessTokenRepository(conn).create(
            id=tid, user_id=uid, name="x",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(), prefix=raw[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        pat = create_access_token(user_id=uid, email="u@t", role="admin", token_id=tid, typ="pat")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.post(
        "/auth/tokens",
        headers={"Authorization": f"Bearer {pat}"},
        json={"name": "bad", "expires_in_days": 30},
    )
    assert resp.status_code == 403
```

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Add `require_session_token` dependency**

In `app/auth/dependencies.py`, append:

```python
async def require_session_token(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """Like get_current_user but rejects PAT — for endpoints that must not
    be callable via a long-lived CI token (e.g. creating new tokens, changing password)."""
    auth = request.headers.get("authorization", "")
    token = None
    if auth.startswith("Bearer "):
        token = auth.removeprefix("Bearer ")
    if not token and request:
        token = request.cookies.get("access_token")
    if token:
        from app.auth.jwt import verify_token
        payload = verify_token(token) or {}
        if payload.get("typ") == "pat":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This endpoint requires an interactive session, not a PAT",
            )
    return user
```

- [ ] **Step 4: Create router `app/api/tokens.py`**

```python
"""Personal access token endpoints (#12)."""

import hashlib
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import require_session_token, require_role, Role, _get_db
from src.repositories.access_tokens import AccessTokenRepository
from src.repositories.audit import AuditRepository
from app.auth.jwt import create_access_token

router = APIRouter(prefix="/auth/tokens", tags=["tokens"])
admin_router = APIRouter(prefix="/auth/admin/tokens", tags=["tokens-admin"])


class CreateTokenRequest(BaseModel):
    name: str
    expires_in_days: Optional[int] = 90  # null = no expiry


class CreateTokenResponse(BaseModel):
    id: str
    name: str
    prefix: str
    token: str  # raw token — returned exactly once
    expires_at: Optional[str]
    created_at: str


class TokenListItem(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: str
    expires_at: Optional[str]
    last_used_at: Optional[str]
    revoked_at: Optional[str]


def _audit(conn, actor: str, action: str, target: str, params=None):
    try:
        AuditRepository(conn).log(user_id=actor, action=action,
                                  resource=f"token:{target}", params=params)
    except Exception:
        pass


def _row_to_item(row: dict) -> TokenListItem:
    return TokenListItem(
        id=row["id"], name=row["name"], prefix=row["prefix"],
        created_at=str(row.get("created_at") or ""),
        expires_at=str(row["expires_at"]) if row.get("expires_at") else None,
        last_used_at=str(row["last_used_at"]) if row.get("last_used_at") else None,
        revoked_at=str(row["revoked_at"]) if row.get("revoked_at") else None,
    )


@router.post("", response_model=CreateTokenResponse, status_code=201)
async def create_token(
    payload: CreateTokenRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    repo = AccessTokenRepository(conn)
    token_id = str(uuid.uuid4())
    expires_at = None
    expires_delta = None
    if payload.expires_in_days:
        expires_delta = timedelta(days=payload.expires_in_days)
        expires_at = datetime.now(timezone.utc) + expires_delta
    # Build the JWT that embeds jti=token_id and typ=pat
    jwt_token = create_access_token(
        user_id=user["id"], email=user["email"], role=user["role"],
        token_id=token_id, typ="pat", expires_delta=expires_delta,
    )
    # Prefix: first 8 chars of the jti (UUID) — uniquely identifies the token in UI
    # without exposing JWT headers (which all start with "eyJhbGci…" and are useless
    # for identification). The JWT itself is returned ONCE in the response body.
    prefix = token_id.replace("-", "")[:8]
    # token_hash = sha256(raw JWT). Used in verify_token as defense-in-depth.
    token_hash = hashlib.sha256(jwt_token.encode()).hexdigest()
    repo.create(
        id=token_id, user_id=user["id"], name=payload.name.strip(),
        token_hash=token_hash, prefix=prefix, expires_at=expires_at,
    )
    _audit(conn, user["id"], "token.create", token_id, {"name": payload.name})
    return CreateTokenResponse(
        id=token_id, name=payload.name.strip(), prefix=prefix,
        token=jwt_token,  # returned EXACTLY ONCE; never retrievable again
        expires_at=str(expires_at) if expires_at else None,
        created_at=str(datetime.now(timezone.utc)),
    )


@router.get("", response_model=List[TokenListItem])
async def list_tokens(
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    rows = AccessTokenRepository(conn).list_for_user(user["id"])
    return [_row_to_item(r) for r in rows]


@router.get("/{token_id}", response_model=TokenListItem)
async def get_token(
    token_id: str,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = AccessTokenRepository(conn).get_by_id(token_id)
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Token not found")
    return _row_to_item(row)


@router.delete("/{token_id}", status_code=204)
async def revoke_token(
    token_id: str,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = AccessTokenRepository(conn)
    row = repo.get_by_id(token_id)
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Token not found")
    repo.revoke(token_id)
    _audit(conn, user["id"], "token.revoke", token_id)


# Admin — list & revoke tokens across users (for incident response)

@admin_router.get("", response_model=List[TokenListItem])
async def admin_list_tokens(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return [_row_to_item(r) for r in AccessTokenRepository(conn).list_all()]


@admin_router.delete("/{token_id}", status_code=204)
async def admin_revoke_token(
    token_id: str,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = AccessTokenRepository(conn)
    row = repo.get_by_id(token_id)
    if not row:
        raise HTTPException(status_code=404, detail="Token not found")
    repo.revoke(token_id)
    _audit(conn, user["id"], "token.admin_revoke", token_id, {"owner_id": row["user_id"]})
```

- [ ] **Step 5: Register routers in `app/main.py`**

Add imports:

```python
from app.api.tokens import router as tokens_router, admin_router as tokens_admin_router
```

Add in `create_app` near other `include_router` calls:

```python
    app.include_router(tokens_router)
    app.include_router(tokens_admin_router)
```

- [ ] **Step 6: Run — pass**

Run: `pytest tests/test_pat.py -v`

- [ ] **Step 7: Commit**

```bash
git add app/api/tokens.py app/main.py app/auth/dependencies.py tests/test_pat.py
git commit -m "feat(api): /auth/tokens CRUD + admin revoke; session-only guard (#12)"
```

### Task 2.6: CLI — `da auth token create|list|revoke`

**Files:**
- Create: `cli/commands/tokens.py`
- Modify: `cli/commands/auth.py` — register `token` sub-typer
- Test: `tests/test_cli_auth.py` — append

- [ ] **Step 1: Failing test**

```python
# tests/test_cli_auth.py — append

def test_da_auth_token_create_calls_api(monkeypatch):
    import httpx
    from typer.testing import CliRunner
    from cli.commands.auth import auth_app
    from cli.commands import tokens as tok_mod

    captured = {}

    def fake_post(path, json=None, **kwargs):
        captured["path"] = path
        captured["json"] = json
        return httpx.Response(201, json={
            "id": "abc", "name": json["name"], "prefix": "XXXXXXXX",
            "token": "raw-token-once",
            "expires_at": None, "created_at": "2026-04-21T00:00:00+00:00",
        })

    monkeypatch.setattr(tok_mod, "api_post", fake_post, raising=False)

    runner = CliRunner()
    result = runner.invoke(auth_app, ["token", "create", "--name", "laptop", "--ttl", "30d"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/auth/tokens"
    assert captured["json"] == {"name": "laptop", "expires_in_days": 30}
    assert "raw-token-once" in result.output
```

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Create `cli/commands/tokens.py`**

```python
"""`da auth token` — manage personal access tokens (#12)."""

import json as _json
import re
from typing import Optional

import typer

from cli.client import api_post, api_get, api_delete

token_app = typer.Typer(help="Personal access tokens (long-lived CLI/CI auth)")


def _parse_ttl(ttl: Optional[str]) -> Optional[int]:
    """Parse "30d", "90d", "365d", "never" → days (int) or None."""
    if not ttl or ttl.lower() in ("never", "none", "no-expiry"):
        return None
    m = re.fullmatch(r"(\d+)d", ttl.lower().strip())
    if not m:
        raise typer.BadParameter(f"Invalid TTL: {ttl}. Use e.g. 30d, 90d, 365d, or 'never'.")
    return int(m.group(1))


@token_app.command("create")
def create(
    name: str = typer.Option(..., "--name", help="Human label for the token"),
    ttl: str = typer.Option("90d", "--ttl", help="Lifetime (e.g. 30d, 90d, 365d, never)"),
    raw: bool = typer.Option(False, "--raw", help="Print only the raw token (for CI)"),
):
    """Create a new personal access token."""
    body = {"name": name, "expires_in_days": _parse_ttl(ttl)}
    resp = api_post("/auth/tokens", json=body)
    if resp.status_code != 201:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)
    data = resp.json()
    if raw:
        typer.echo(data["token"])
        return
    typer.echo("Personal access token created — this is shown ONCE:")
    typer.echo("")
    typer.echo(f"    {data['token']}")
    typer.echo("")
    typer.echo(f"id:      {data['id']}")
    typer.echo(f"name:    {data['name']}")
    typer.echo(f"expires: {data.get('expires_at') or 'never'}")
    typer.echo("")
    typer.echo("Export it so `da` can use it:")
    typer.echo(f"    export DA_TOKEN={data['token']}")


@token_app.command("list")
def list_tokens(as_json: bool = typer.Option(False, "--json")):
    """List your personal access tokens."""
    resp = api_get("/auth/tokens")
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)
    rows = resp.json()
    if as_json:
        typer.echo(_json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No tokens yet. Create one with: da auth token create --name <label>")
        return
    typer.echo(f"{'ID':36s} {'NAME':20s} {'PREFIX':10s} {'EXPIRES':20s} {'LAST USED':20s} STATUS")
    for r in rows:
        status = "revoked" if r.get("revoked_at") else "active"
        typer.echo(
            f"{r['id']:36s} {r['name']:20s} {r['prefix']:10s} "
            f"{(r.get('expires_at') or 'never'):20s} "
            f"{(r.get('last_used_at') or '-'):20s} {status}"
        )


@token_app.command("revoke")
def revoke(
    ident: str = typer.Argument(..., help="Token id, prefix, or name"),
):
    """Revoke a token."""
    resp = api_get("/auth/tokens")
    if resp.status_code != 200:
        typer.echo(f"Failed to list tokens: {resp.text}", err=True)
        raise typer.Exit(1)
    rows = resp.json()
    match = next(
        (r for r in rows if r["id"] == ident or r["prefix"] == ident or r["name"] == ident),
        None,
    )
    if not match:
        typer.echo(f"No token matches {ident}", err=True)
        raise typer.Exit(1)
    del_resp = api_delete(f"/auth/tokens/{match['id']}")
    if del_resp.status_code != 204:
        typer.echo(f"Failed: {del_resp.text}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Revoked token {match['id']} ({match['name']})")
```

- [ ] **Step 4: Wire into `cli/commands/auth.py`**

At the end of `cli/commands/auth.py` add:

```python
from cli.commands.tokens import token_app
auth_app.add_typer(token_app, name="token")
```

- [ ] **Step 5: Run — pass**

Run: `pytest tests/test_cli_auth.py -v`

- [ ] **Step 6: Commit**

```bash
git add cli/commands/tokens.py cli/commands/auth.py tests/test_cli_auth.py
git commit -m "feat(cli): da auth token create/list/revoke (#12)"
```

### Task 2.7: UI — /profile page with PAT section

**Files:**
- Create: `app/web/templates/profile.html`
- Modify: `app/web/router.py` — add `/profile` route
- Modify: `app/web/templates/dashboard.html` — add "Profile" link in nav (any authenticated user)

- [ ] **Step 1: Create template**

Create `app/web/templates/profile.html`:

```html
{% extends "base.html" %}
{% block title %}Profile — {{ config.INSTANCE_NAME }}{% endblock %}

{% block content %}
<div class="container">
  <h2>Profile</h2>
  <p>Signed in as <strong>{{ user.email }}</strong> ({{ user.role }}).</p>

  <h3>Personal access tokens</h3>
  <p>Long-lived tokens for CLI, CI, and headless clients.
     <a href="/install">How to use a token with <code>da</code> CLI →</a>
  </p>

  <form id="create-form" onsubmit="createToken(event)">
    <input id="new-name" type="text" placeholder="Token name (e.g. laptop, github-ci)" required>
    <select id="new-ttl">
      <option value="30">30 days</option>
      <option value="90" selected>90 days</option>
      <option value="365">1 year</option>
      <option value="">never</option>
    </select>
    <button class="btn btn-primary" type="submit">Create token</button>
  </form>

  <div id="new-token-reveal" style="display:none; margin: 1em 0; padding: 1em; background: #fee3; border: 1px solid #ca0;">
    <strong>Copy your token now — it will not be shown again:</strong>
    <pre><code id="new-token-raw"></code></pre>
    <button class="btn btn-sm" onclick="copyNewToken()">Copy</button>
    <button class="btn btn-sm" onclick="dismissReveal()">Dismiss</button>
  </div>

  <table class="table" id="tokens-table" style="margin-top: 1em;">
    <thead>
      <tr><th>Name</th><th>Prefix</th><th>Created</th><th>Expires</th>
          <th>Last used</th><th>Status</th><th>Actions</th></tr>
    </thead>
    <tbody></tbody>
  </table>
</div>

<script>
async function loadTokens() {
  const r = await fetch("/auth/tokens", {credentials: "include"});
  if (!r.ok) { alert("Failed: " + r.status); return; }
  const rows = await r.json();
  const tbody = document.querySelector("#tokens-table tbody");
  tbody.innerHTML = "";
  for (const t of rows) {
    const status = t.revoked_at ? "revoked" : "active";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${t.name}</td>
      <td><code>${t.prefix}…</code></td>
      <td>${t.created_at.slice(0,19).replace("T"," ")}</td>
      <td>${t.expires_at ? t.expires_at.slice(0,19).replace("T"," ") : "never"}</td>
      <td>${t.last_used_at ? t.last_used_at.slice(0,19).replace("T"," ") : "-"}</td>
      <td>${status}</td>
      <td>${t.revoked_at ? "" :
        `<button class="btn btn-sm btn-danger" onclick="revokeToken('${t.id}')">Revoke</button>`}</td>`;
    tbody.appendChild(tr);
  }
}

async function createToken(ev) {
  ev.preventDefault();
  const name = document.getElementById("new-name").value;
  const ttl = document.getElementById("new-ttl").value;
  const body = {name, expires_in_days: ttl ? Number(ttl) : null};
  const r = await fetch("/auth/tokens", {
    method: "POST", credentials: "include",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(body),
  });
  if (!r.ok) { alert("Failed: " + (await r.text())); return; }
  const data = await r.json();
  document.getElementById("new-token-raw").textContent = data.token;
  document.getElementById("new-token-reveal").style.display = "block";
  document.getElementById("new-name").value = "";
  loadTokens();
}

async function revokeToken(id) {
  if (!confirm("Revoke this token?")) return;
  const r = await fetch(`/auth/tokens/${id}`, {method: "DELETE", credentials: "include"});
  if (!r.ok) { alert("Failed: " + (await r.text())); return; }
  loadTokens();
}

function copyNewToken() {
  const txt = document.getElementById("new-token-raw").textContent;
  navigator.clipboard.writeText(txt);
}
function dismissReveal() {
  document.getElementById("new-token-reveal").style.display = "none";
  document.getElementById("new-token-raw").textContent = "";
}

loadTokens();
</script>
{% endblock %}
```

- [ ] **Step 2: Add route in `app/web/router.py`**

```python
@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    user: dict = Depends(get_current_user),
):
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "profile.html", ctx)
```

- [ ] **Step 3: Test**

```python
# tests/test_pat.py — append

def test_profile_page_renders(fresh_db):
    from fastapi.testclient import TestClient
    import uuid
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="analyst")
        token = create_access_token(user_id=uid, email="u@t", role="analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/profile",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Personal access tokens" in resp.text
```

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit**

```bash
git add app/web/templates/profile.html app/web/router.py tests/test_pat.py
git commit -m "feat(ui): /profile page with PAT create/list/revoke (#12)"
```

### Task 2.8: Docs — fix cli/skills/security.md 24h/30d mismatch + PAT section

**Files:**
- Modify: `cli/skills/security.md`
- Create: `docs/HEADLESS_USAGE.md`

- [ ] **Step 1: Update `cli/skills/security.md`**

Find the line claiming "Issued on login, valid 30 days" and correct:

```markdown
Session tokens: issued on interactive login (`da login`), valid 24 hours.
For long-lived CLI / CI use, create a Personal Access Token via the UI
(`/profile` → Personal access tokens) or CLI (`da auth token create`).
PATs are revocable and auditable; session tokens are not.
```

- [ ] **Step 2: Create `docs/HEADLESS_USAGE.md`**

```markdown
# Headless / CI usage

For unattended clients (CI, cron, Claude Code), authenticate with a Personal Access Token (PAT) rather than an interactive session.

## Create a PAT

**Via UI:** sign in, open `/profile`, create a token. Copy the raw value — it is shown exactly once.

**Via CLI (requires an interactive session):**

```bash
da auth token create --name "github-actions" --ttl 365d --raw
```

The `--raw` flag prints only the token, suitable for piping into a secret store.

## Use the PAT

Set the `DA_TOKEN` env var:

```bash
export DA_TOKEN=<your-token>
da query "SELECT 1"
```

### GitHub Actions example

```yaml
- name: Sync data
  env:
    DA_TOKEN: ${{ secrets.AGNES_TOKEN }}
    DA_SERVER: https://agnes.example.com
  run: |
    pip install data-analyst
    da sync --all
```

## Revoke

```bash
da auth token list
da auth token revoke <id|prefix|name>
```

Or from `/profile` → Revoke.
```

- [ ] **Step 3: Commit**

```bash
git add cli/skills/security.md docs/HEADLESS_USAGE.md
git commit -m "docs: PAT usage and session/PAT TTL clarification (#12)"
```

### Task 2.9: Phase 2 integration

- [ ] **Step 1: Run full suite**

Run: `pytest tests/ --timeout=30`

- [ ] **Step 2: Smoke**

Start server, sign in, create a PAT from `/profile`, use it via `DA_TOKEN` to run `da query`. Revoke it. Verify a revoked PAT fails on next call.

- [ ] **Step 3: Request code review**

Dispatch `superpowers:code-reviewer` on Phase 2 diff. Fix blockers.

---

## Phase 3 — CLI Distribution (#9)

Scope: Dockerfile staví wheel + install skript, FastAPI vystavuje `/cli/download` a `/cli/install.sh` s base-URL zaplétaným do skriptu, `/install` HTML stránka s návodem, link v dashboardu, fix bugu `da login` nezadává heslo. Statické docs v image.

### File Structure

- Modify: `Dockerfile` — `uv build` + stash wheel at `/app/dist`
- Create: `app/api/cli_artifacts.py` — `/cli/download` + `/cli/install.sh`
- Modify: `app/main.py` — register router
- Create: `app/web/templates/install.html`
- Modify: `app/web/router.py` — add `/install` route
- Modify: `app/web/templates/dashboard.html` — link to `/install`
- Modify: `cli/commands/auth.py` — prompt for password, send in body
- Test: `tests/test_cli_artifacts.py` (new)
- Test: `tests/test_cli_auth.py` — extend

### Task 3.1: Dockerfile — build wheel + bake CLI version

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Replace Dockerfile**

```dockerfile
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ARG AGNES_VERSION=dev
ARG RELEASE_CHANNEL=dev
ARG AGNES_COMMIT_SHA=unknown
ENV AGNES_VERSION=${AGNES_VERSION}
ENV RELEASE_CHANNEL=${RELEASE_CHANNEL}
ENV AGNES_COMMIT_SHA=${AGNES_COMMIT_SHA}

WORKDIR /app

COPY . .

# Build wheel artifact (served at /cli/download)
RUN uv build --wheel --out-dir /app/dist

# Install production dependencies from pyproject.toml
RUN uv pip install --system --no-cache .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Local build verification**

Run: `docker build -t agnes:test .`
Then: `docker run --rm agnes:test ls -la /app/dist/`
Expected: `agnes_the_ai_analyst-*.whl` file exists.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "build(docker): produce wheel artifact for /cli/download (#9)"
```

### Task 3.2: FastAPI — /cli/download + /cli/install.sh

**Files:**
- Create: `app/api/cli_artifacts.py`
- Modify: `app/main.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_cli_artifacts.py
"""Tests for #9 — CLI artifact + install script endpoints."""

import os
from pathlib import Path
import tempfile


def test_cli_install_script_bakes_server_url(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, base_url="https://agnes.example.com")
    resp = client.get("/cli/install.sh", headers={"host": "agnes.example.com"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/")
    body = resp.text
    assert "https://agnes.example.com" in body or "agnes.example.com" in body
    assert "pip install" in body or "uv tool install" in body


def test_cli_download_returns_wheel_or_404(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/cli/download")
    # Either serve the wheel or return a clear 404 telling where to find it.
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        assert resp.headers["content-disposition"].startswith("attachment")


def test_cli_download_serves_wheel_when_present(monkeypatch, tmp_path):
    """Put a fake wheel and confirm the endpoint serves it."""
    wheel = tmp_path / "agnes_fake-1.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04fake-wheel-bytes")
    monkeypatch.setenv("AGNES_CLI_DIST_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/cli/download")
    assert resp.status_code == 200
    assert resp.content.startswith(b"PK")
```

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Implement `app/api/cli_artifacts.py`**

```python
"""CLI artifact download + install script endpoints (#9)."""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

router = APIRouter(tags=["cli"])


def _dist_dir() -> Path:
    return Path(os.environ.get("AGNES_CLI_DIST_DIR", "/app/dist"))


def _find_wheel() -> Path | None:
    d = _dist_dir()
    if not d.exists():
        return None
    wheels = sorted(d.glob("*.whl"))
    return wheels[-1] if wheels else None


@router.get("/cli/download")
async def cli_download():
    wheel = _find_wheel()
    if not wheel:
        raise HTTPException(
            status_code=404,
            detail=(
                "CLI wheel not found in dist dir. Build it with `uv build --wheel` "
                "or run the official docker image (which builds on image-build)."
            ),
        )
    return FileResponse(
        path=str(wheel),
        filename=wheel.name,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{wheel.name}"'},
    )


@router.get("/cli/install.sh", response_class=PlainTextResponse)
async def cli_install_script(request: Request):
    """Shell installer — bakes this server's URL into the generated config."""
    base_url = str(request.base_url).rstrip("/")
    version = os.environ.get("AGNES_VERSION", "dev")
    script = f"""#!/usr/bin/env bash
# Agnes CLI installer — server: {base_url}
set -euo pipefail

SERVER="{base_url}"
echo "Installing Agnes CLI from $SERVER (version: {version})"

# 1. Download the wheel
WHEEL=$(mktemp -t agnes_cli.XXXXXX.whl)
curl -fsSL "$SERVER/cli/download" -o "$WHEEL"

# 2. Install via pip (prefer uv tool install if available)
if command -v uv >/dev/null 2>&1; then
    uv tool install --force "$WHEEL"
else
    python3 -m pip install --user --force-reinstall "$WHEEL"
fi

rm -f "$WHEEL"

# 3. Seed the server URL in CLI config
CFG_DIR="${{DA_CONFIG_DIR:-$HOME/.config/da}}"
mkdir -p "$CFG_DIR"
cat > "$CFG_DIR/config.yaml" <<EOF
server: $SERVER
EOF

echo "Installed."
echo "Next steps:"
echo "  1. Sign in to $SERVER and create a personal access token at $SERVER/profile"
echo "  2. Export it:   export DA_TOKEN=<your-token>"
echo "  3. Verify:      da auth whoami"
"""
    return script
```

- [ ] **Step 4: Register in `app/main.py`**

```python
from app.api.cli_artifacts import router as cli_artifacts_router
# ...
    app.include_router(cli_artifacts_router)
```

- [ ] **Step 5: Run — pass**

Run: `pytest tests/test_cli_artifacts.py -v`

- [ ] **Step 6: Commit**

```bash
git add app/api/cli_artifacts.py app/main.py tests/test_cli_artifacts.py
git commit -m "feat(api): /cli/download wheel + /cli/install.sh with baked server URL (#9)"
```

### Task 3.3: /install HTML page

**Files:**
- Create: `app/web/templates/install.html`
- Modify: `app/web/router.py`

- [ ] **Step 1: Create template**

`app/web/templates/install.html`:

```html
{% extends "base.html" %}
{% block title %}Install CLI — {{ config.INSTANCE_NAME }}{% endblock %}

{% block content %}
<div class="container">
  <h2>Install the Agnes CLI</h2>
  <p>This server: <code>{{ server_url }}</code> (version <code>{{ agnes_version }}</code>)</p>

  <h3>One-liner (Linux / macOS)</h3>
  <pre><code>curl -fsSL {{ server_url }}/cli/install.sh | bash</code></pre>

  <h3>Manual</h3>
  <ol>
    <li>Download: <a href="/cli/download">{{ server_url }}/cli/download</a></li>
    <li>Install:
      <pre><code>uv tool install ./agnes-*.whl
# or
python3 -m pip install --user ./agnes-*.whl</code></pre>
    </li>
    <li>Seed server URL:
      <pre><code>mkdir -p ~/.config/da
echo "server: {{ server_url }}" > ~/.config/da/config.yaml</code></pre>
    </li>
  </ol>

  <h3>Connect</h3>
  <p>Create a personal access token (see <a href="/profile">/profile</a>) and export it:</p>
  <pre><code>export DA_TOKEN=&lt;your-token&gt;
da auth whoami</code></pre>

  <h3>Claude Code / MCP</h3>
  <p>
    Store your token in <code>~/.config/da/token.json</code> (Claude Code
    reads this automatically via the <code>da</code> entrypoint) or export
    <code>DA_TOKEN</code> in your shell.
  </p>

  <h3>CI / headless</h3>
  <p>See <a href="/docs/HEADLESS_USAGE.md" target="_blank">Headless usage guide</a>.</p>
</div>
{% endblock %}
```

- [ ] **Step 2: Add route**

In `app/web/router.py`:

```python
@router.get("/install", response_class=HTMLResponse)
async def install_page(request: Request):
    """Public install instructions for the CLI."""
    base_url = str(request.base_url).rstrip("/")
    ctx = _build_context(
        request,
        server_url=base_url,
        agnes_version=os.environ.get("AGNES_VERSION", "dev"),
    )
    return templates.TemplateResponse(request, "install.html", ctx)
```

- [ ] **Step 3: Dashboard link**

In `app/web/templates/dashboard.html`, find the primary nav and add:

```html
<a class="nav-link" href="/install">Install CLI</a>
```

- [ ] **Step 4: Test**

```python
# tests/test_cli_artifacts.py — append

def test_install_page_renders_with_server_url():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/install", headers={"host": "agnes.test", "Accept": "text/html"})
    assert resp.status_code == 200
    assert "agnes.test" in resp.text
    assert "da auth whoami" in resp.text
```

- [ ] **Step 5: Run — pass**

- [ ] **Step 6: Commit**

```bash
git add app/web/router.py app/web/templates/install.html app/web/templates/dashboard.html tests/test_cli_artifacts.py
git commit -m "feat(ui): /install page with per-deployment install instructions (#9)"
```

### Task 3.4: Fix `da login` password prompt bug

**Files:**
- Modify: `cli/commands/auth.py`
- Test: `tests/test_cli_auth.py` — append

- [ ] **Step 1: Failing test**

```python
# tests/test_cli_auth.py — append

def test_da_login_sends_password(monkeypatch):
    import httpx
    from typer.testing import CliRunner
    from cli.commands import auth as auth_mod

    captured = {}

    def fake_post(path, json=None, **kwargs):
        captured["path"] = path
        captured["json"] = json
        return httpx.Response(200, json={
            "access_token": "tok", "email": "u@t", "role": "analyst",
            "user_id": "u1", "token_type": "bearer",
        })

    monkeypatch.setattr(auth_mod, "api_post", fake_post, raising=False)

    runner = CliRunner()
    # Provide email and password via stdin (typer prompts)
    result = runner.invoke(auth_mod.auth_app, ["login"], input="u@t\nhunter2\n")
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/auth/token"
    assert captured["json"] == {"email": "u@t", "password": "hunter2"}
```

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Fix `login` command**

Replace in `cli/commands/auth.py`:

```python
@auth_app.command()
def login(
    email: str = typer.Option(..., prompt=True, help="Your email address"),
    password: str = typer.Option(
        "", prompt="Password (leave empty for magic-link / OAuth accounts)",
        hide_input=True, help="Your password (if the account has one)",
    ),
    server: str = typer.Option(None, help="Server URL override"),
):
    """Login and obtain a JWT token.

    Password-enabled accounts: enter the password when prompted.
    Magic-link / OAuth accounts: leave the password empty — the server will
    respond with guidance pointing you to the correct auth provider.
    """
    if server:
        import os
        os.environ["DA_SERVER"] = server

    body = {"email": email}
    if password:
        body["password"] = password

    try:
        resp = api_post("/auth/token", json=body)
        if resp.status_code == 200:
            data = resp.json()
            save_token(data["access_token"], data["email"], data["role"])
            typer.echo(f"Logged in as {data['email']} (role: {data['role']})")
            return
        # Helpful error for accounts that cannot login via password.
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        if resp.status_code == 401 and "external authentication" in str(detail).lower():
            typer.echo(
                "This account uses a magic link / OAuth provider. "
                "Sign in via the web UI, open /profile, and create a personal "
                "access token — then export it as DA_TOKEN.",
                err=True,
            )
        else:
            typer.echo(f"Login failed: {detail}", err=True)
        raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Connection error: {e}", err=True)
        raise typer.Exit(1)
```

- [ ] **Step 4: Run — pass**

Run: `pytest tests/test_cli_auth.py::test_da_login_sends_password -v`

- [ ] **Step 5: Commit**

```bash
git add cli/commands/auth.py tests/test_cli_auth.py
git commit -m "fix(cli): da login prompts for password and sends it in body (#9)"
```

### Task 3.5: Phase 3 integration

- [ ] **Step 1: Run full suite**

Run: `pytest tests/ --timeout=30`

- [ ] **Step 2: Smoke**

- `docker build` produces wheel in `/app/dist`.
- Hitting `/cli/install.sh` returns a shell script with the correct URL.
- Hitting `/install` renders install instructions with the correct base URL.
- `da login` now prompts for a password and succeeds against a password-enabled account.

- [ ] **Step 3: Request code review**

Dispatch `superpowers:code-reviewer` on Phase 3 diff.

---

## Final Integration

### Task F.1: Full suite + merge

- [ ] All three phases green: `pytest tests/ --timeout=30`
- [ ] Docker build succeeds and serves the wheel + install.sh
- [ ] Manual walkthrough: new user flow (create via UI → reset password → log in → create PAT → use PAT via CLI → revoke → verify PAT rejected).

### Task F.2: Coverage check against issues

Run a final verification agent that reads each of #9, #10, #11, #12 and the resulting diff, and reports any unmet acceptance criterion. Iterate until every bullet is green.

---

## Self-Review

- **Spec coverage for #10:** HTML redirect handled in Phase 0. API clients still get 401. ✅
- **Spec coverage for #11:**
  - Schema v5 (`active` + `deactivated_at/by`) ✅
  - `PATCH`, `POST /reset-password`, `POST /set-password`, `POST /activate`, `POST /deactivate`, audit log on every mutation ✅
  - Self-deactivate + last-admin safeguards ✅
  - `get_current_user` checks `active` ✅
  - CLI commands (set-role, activate, deactivate, reset-password, set-password) + extended `list-users` ✅
  - `/admin/users` UI ✅
  - Tests for every bullet ✅
- **Spec coverage for #12:**
  - `personal_access_tokens` DuckDB table (schema v6) ✅
  - JWT `typ`+`jti`; `verify_token` via DB for `typ==pat` ✅
  - `/auth/tokens` CRUD + admin variant ✅
  - CLI `da auth token create|list|revoke` ✅
  - UI profile page with one-time reveal ✅
  - Audit entries on create/revoke; `last_used_at` updated (sync) ✅
  - `cli/skills/security.md` correction + `docs/HEADLESS_USAGE.md` ✅
  - PAT cannot create new PATs (session-only guard) ✅
- **Spec coverage for #9:**
  - Wheel built in Dockerfile, stored at `/app/dist` ✅
  - `/cli/download` + `/cli/install.sh` with base URL baked-in ✅
  - `/install` page ✅
  - `da login` password bug fix ✅
  - Dashboard link ✅

- **Placeholder scan:** every code step has a full code block or exact command. No "TBD" or "implement later".
- **Type consistency:** `typ` ("session"|"pat"), `token_id` → stored as `id`/`jti`, repository field names consistent across Phase 2 tasks.

All spec bullets covered. Ready for execution handoff.
