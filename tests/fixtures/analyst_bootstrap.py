"""Test fixtures for the clean-bootstrap test suite (Task 20).

Boots a real FastAPI server via uvicorn subprocess so end-to-end paths
exercise the same wsgi/asgi stack as production (cookie sessions, PAT
verify, JWT, DB locks). Pre-seeds two users + three tables in the system
DB before the subprocess starts so the test can authenticate immediately.

Subprocess (not in-thread uvicorn): isolates the test's `DATA_DIR` and
the load-bearing module-level singletons in `src.db` (cached system DB
connection) and `app.instance_config` from the parent test runner. The
existing E2E pattern in tests/test_e2e_corporate_memory.py uses the same
shape; we reuse it here for the same reasons.

Public API:
- `fastapi_test_server` — yields `_ServerHandle` with `.url` + `.shutdown()`.
- `web_session` — `httpx.Client` authenticated as admin via the form-login
  endpoint (cookie session).
- `test_pat` — string PAT for analyst with grants to the `local` and
  `materialized` test tables.
- `test_pat_no_grants` — string PAT for analyst with zero grants.
- `zero_grants_workspace` — `Path` to a workspace where `agnes init` has
  run with `test_pat_no_grants` (no parquets, no rules).
- `NONEXISTENT_TABLE` — module constant for Task 21's smoke matrix.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import httpx
import pytest


NONEXISTENT_TABLE = "__nonexistent__"

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "test-admin-password-123"
ANALYST_EMAIL = "analyst@example.com"
ANALYST_PASSWORD = "test-analyst-password-123"

# Test table fixtures — one per query_mode the manifest filter cares about.
LOCAL_TABLE_ID = "local_tbl"
MATERIALIZED_TABLE_ID = "materialized_tbl"
REMOTE_TABLE_ID = "remote_tbl"


# ---------------------------------------------------------------------------
# Server handle
# ---------------------------------------------------------------------------


@dataclass
class _ServerHandle:
    url: str
    data_dir: Path
    proc: subprocess.Popen
    admin_user_id: str
    analyst_user_id: str
    everyone_group_id: str
    admin_group_id: str

    def shutdown(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed_db(data_dir: Path) -> dict:
    """Open system.duckdb under DATA_DIR, run migrations, seed users + tables.

    Done before the uvicorn subprocess boots so:
    1. The test can immediately log in as admin/analyst (passwords set up).
    2. The subprocess inherits a ready DB on first request — no race between
       startup migrations and the test's first HTTP call.
    3. We close the parent's connection at the end so the subprocess can
       acquire DuckDB's file lock.
    """
    # Restrict the system-db path resolution to our tmp path. _seed_db
    # mutates module state in src.db; the caller's get_system_db() cache
    # is reset by tests/conftest._reset_module_caches but we still want
    # the subprocess child to see the same DATA_DIR.
    os.environ["DATA_DIR"] = str(data_dir)
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "analytics").mkdir(parents=True, exist_ok=True)
    (data_dir / "extracts").mkdir(parents=True, exist_ok=True)

    # Defer imports until after DATA_DIR is set so any module that reads
    # the env at import time picks up our path.
    import uuid
    from argon2 import PasswordHasher

    from src.db import (
        SYSTEM_ADMIN_GROUP,
        SYSTEM_EVERYONE_GROUP,
        close_system_db,
        get_system_db,
    )
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        ph = PasswordHasher()

        # --- Users -----------------------------------------------------
        users = UserRepository(conn)
        admin_id = str(uuid.uuid4())
        analyst_id = str(uuid.uuid4())
        users.create(
            id=admin_id, email=ADMIN_EMAIL, name="Admin Tester",
            password_hash=ph.hash(ADMIN_PASSWORD),
        )
        users.create(
            id=analyst_id, email=ANALYST_EMAIL, name="Analyst Tester",
            password_hash=ph.hash(ANALYST_PASSWORD),
        )

        # --- System groups (Admin / Everyone are seeded by _ensure_schema).
        admin_group_row = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP],
        ).fetchone()
        everyone_group_row = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?",
            [SYSTEM_EVERYONE_GROUP],
        ).fetchone()
        assert admin_group_row, "Admin system group not seeded"
        assert everyone_group_row, "Everyone system group not seeded"
        admin_group_id = admin_group_row[0]
        everyone_group_id = everyone_group_row[0]

        members = UserGroupMembersRepository(conn)
        members.add_member(admin_id, admin_group_id, source="system_seed")
        # The analyst is in Everyone so RBAC checks against the Everyone
        # group resolve cleanly. Admin is implicitly in Everyone for
        # historical reasons but the marketplace filter treats Admin as a
        # regular group, so we add it explicitly to be unambiguous.
        members.add_member(admin_id, everyone_group_id, source="system_seed")
        members.add_member(analyst_id, everyone_group_id, source="system_seed")

        # --- Tables (one per query_mode) -------------------------------
        tables = TableRegistryRepository(conn)
        tables.register(
            id=LOCAL_TABLE_ID, name=LOCAL_TABLE_ID,
            source_type="keboola", bucket="test",
            source_table=LOCAL_TABLE_ID, query_mode="local",
        )
        tables.register(
            id=MATERIALIZED_TABLE_ID, name=MATERIALIZED_TABLE_ID,
            source_type="bigquery", bucket="test",
            source_table=MATERIALIZED_TABLE_ID, query_mode="materialized",
        )
        tables.register(
            id=REMOTE_TABLE_ID, name=REMOTE_TABLE_ID,
            source_type="bigquery", bucket="test",
            source_table=REMOTE_TABLE_ID, query_mode="remote",
        )

        # --- Parquet files + sync_state for non-remote tables -----------
        # The manifest builder iterates `sync_state` (not table_registry) and
        # `/api/data/{tid}/download` looks up parquet files under
        # `data_dir/extracts/.../data/`. Seeding both lets `agnes init`
        # exercise the full download path, not just the registry-only stub.
        # Each parquet is a single-row DuckDB COPY — minimal but valid (PAR1
        # magic + metadata) so client-side `_is_valid_parquet` passes.
        from src.repositories.sync_state import SyncStateRepository
        from datetime import datetime, timezone
        sync_repo = SyncStateRepository(conn)
        extracts_data = data_dir / "extracts" / "test" / "data"
        extracts_data.mkdir(parents=True, exist_ok=True)
        for tid in (LOCAL_TABLE_ID, MATERIALIZED_TABLE_ID):
            parquet_path = extracts_data / f"{tid}.parquet"
            # COPY ... TO creates a real parquet via DuckDB's writer.
            conn.execute(
                f"COPY (SELECT 1 AS id, 'sample' AS label) "
                f"TO '{parquet_path}' (FORMAT PARQUET)"
            )
            # Compute MD5 the same way `app/api/sync.py:_file_hash` and
            # `cli/lib/pull.py:_file_md5` do — chunked 8k reads.
            import hashlib
            h = hashlib.md5()
            with open(parquet_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(8192), b""):
                    h.update(chunk)
            sync_repo.update_sync(
                table_id=tid,
                rows=1,
                file_size_bytes=parquet_path.stat().st_size,
                hash=h.hexdigest(),
            )
    finally:
        conn.close()

    # CRITICAL: release DuckDB's file lock so the uvicorn subprocess can
    # open the DB. The parent's cached connection is held by src.db at
    # module level; without close_system_db() the child blocks forever
    # on its first get_system_db() call.
    close_system_db()

    return {
        "admin_user_id": admin_id,
        "analyst_user_id": analyst_id,
        "admin_group_id": admin_group_id,
        "everyone_group_id": everyone_group_id,
    }


def _wait_for_server(url: str, timeout_s: float = 30.0) -> None:
    """Poll /api/health until the server answers 200 or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{url}/api/health", timeout=1.0)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Server at {url} not ready within {timeout_s}s")


def _login_session(server_url: str, email: str, password: str) -> httpx.Client:
    """Form-login and return an httpx.Client with the access cookie set.

    The /auth/password/login/web endpoint sets `access_token` as an
    HttpOnly cookie and 302s to /dashboard. Using `follow_redirects=False`
    means we capture the cookie without chasing the redirect chain.
    """
    client = httpx.Client(base_url=server_url, follow_redirects=False, timeout=10.0)
    resp = client.post(
        "/auth/password/login/web",
        data={"email": email, "password": password},
    )
    assert resp.status_code == 302, (
        f"login expected 302 redirect, got {resp.status_code}: {resp.text[:300]}"
    )
    # Sanity: the redirect Location must NOT carry ?error=invalid (form-login
    # bounces back to /login/password on bad creds with status 302 too).
    target = resp.headers.get("location", "")
    assert "error=" not in target, f"login failed: redirected to {target}"
    return client


def _mint_pat(server_url: str, email: str, password: str, *, name: str) -> str:
    """Log in as the user via web-form, then POST /auth/tokens.

    Returns the raw JWT (returned exactly once by the create endpoint).
    PATs cannot mint other PATs (require_session_token), so we must use a
    cookie session, not a previously-minted PAT.
    """
    session = _login_session(server_url, email, password)
    try:
        resp = session.post(
            "/auth/tokens",
            json={"name": name, "ttl_seconds": 3600},
        )
        assert resp.status_code == 201, (
            f"PAT mint failed: {resp.status_code} {resp.text[:300]}"
        )
        token = resp.json().get("token")
        assert token and isinstance(token, str), f"no token in response: {resp.text}"
        return token
    finally:
        session.close()


def _grant_table_access(web_session: httpx.Client, group_id: str, table_id: str) -> None:
    """POST /api/admin/grants for `(group, "table", table_id)`.

    Idempotent: a 409 from the unique constraint is swallowed so the
    fixture can be reused with a pre-existing grant.
    """
    resp = web_session.post(
        "/api/admin/grants",
        json={
            "group_id": group_id,
            "resource_type": "table",
            "resource_id": table_id,
        },
    )
    if resp.status_code not in (201, 409):
        raise AssertionError(
            f"grant create failed: {resp.status_code} {resp.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fastapi_test_server(tmp_path: Path) -> Iterator[_ServerHandle]:
    """Boot a real FastAPI server in a uvicorn subprocess against tmp_path DATA_DIR.

    Pre-seeds:
    - Two users: admin@example.com (Admin group) and analyst@example.com
      (Everyone group only). Both have argon2-hashed passwords usable via
      `/auth/password/login/web`.
    - Two system groups (Admin, Everyone) — created by `_ensure_schema`.
    - Three tables in `table_registry`, one per query_mode (local,
      materialized, remote).

    The subprocess inherits `DATA_DIR=tmp_path/agnes-data` plus whatever
    `JWT_SECRET_KEY` / `TESTING` is in the environment, so the parent
    process can verify JWTs against the same secret it issued.

    Port is allocated via `_find_free_port` so xdist workers don't
    collide. Server is shut down via SIGTERM in the fixture teardown.
    """
    data_dir = tmp_path / "agnes-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    seeded = _seed_db(data_dir)

    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["DATA_DIR"] = str(data_dir)
    env["TESTING"] = "1"
    env["JWT_SECRET_KEY"] = os.environ.get(
        "JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!"
    )
    # Disable LOCAL_DEV_MODE — the smoke test must exercise real auth.
    env.pop("LOCAL_DEV_MODE", None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    try:
        _wait_for_server(url)
    except RuntimeError:
        proc.terminate()
        try:
            stdout = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
        except Exception:
            stdout = "<unreadable>"
        proc.wait(timeout=2)
        pytest.fail(f"fastapi_test_server failed to start on {url}\nstdout:\n{stdout[:3000]}")

    handle = _ServerHandle(
        url=url,
        data_dir=data_dir,
        proc=proc,
        admin_user_id=seeded["admin_user_id"],
        analyst_user_id=seeded["analyst_user_id"],
        admin_group_id=seeded["admin_group_id"],
        everyone_group_id=seeded["everyone_group_id"],
    )
    try:
        yield handle
    finally:
        handle.shutdown()


@pytest.fixture
def web_session(fastapi_test_server: _ServerHandle) -> Iterator[httpx.Client]:
    """Authenticated httpx.Client (cookie session) for admin@example.com.

    Cookies persist across requests on the same client, so subsequent
    requests against admin-gated endpoints (e.g. POST /api/admin/grants)
    succeed without re-attaching the JWT.
    """
    client = _login_session(
        fastapi_test_server.url, ADMIN_EMAIL, ADMIN_PASSWORD,
    )
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def test_pat(
    fastapi_test_server: _ServerHandle,
    web_session: httpx.Client,
) -> str:
    """Mint a PAT for analyst@example.com with two table grants.

    Grants applied (to the Everyone group, which the analyst is a member
    of): `local_tbl` and `materialized_tbl`. The third seeded table
    (`remote_tbl`) is intentionally left ungranted so smoke matrices can
    distinguish "remote skip" from "no access".

    Memory items / mandatory rules: not seeded by this fixture; Tasks 21
    and 22 will add them when needed via the same `web_session` admin
    client. Keeping memory off the critical path makes the fixture
    cheaper and the failure surface smaller.
    """
    everyone_id = fastapi_test_server.everyone_group_id
    _grant_table_access(web_session, everyone_id, LOCAL_TABLE_ID)
    _grant_table_access(web_session, everyone_id, MATERIALIZED_TABLE_ID)

    return _mint_pat(
        fastapi_test_server.url,
        ANALYST_EMAIL, ANALYST_PASSWORD,
        name="test-pat-with-grants",
    )


@pytest.fixture
def test_pat_no_grants(fastapi_test_server: _ServerHandle) -> str:
    """Mint a PAT for analyst@example.com with no resource_grants.

    The analyst is still in the Everyone group (so they can authenticate
    and call /api/sync/manifest), but no group they belong to has any
    table grants. The manifest will return zero tables; `agnes init`
    completes (no manifest_unauthorized error) with an empty workspace.
    """
    return _mint_pat(
        fastapi_test_server.url,
        ANALYST_EMAIL, ANALYST_PASSWORD,
        name="test-pat-no-grants",
    )


@pytest.fixture
def zero_grants_workspace(
    tmp_path: Path,
    fastapi_test_server: _ServerHandle,
    test_pat_no_grants: str,
) -> Path:
    """Run `agnes init` with the no-grants PAT; return the workspace path.

    Subprocess invocation (not in-process Typer call) so the test
    exercises the same path the paste-prompt installer uses. The CLI
    binary is the editable install at `.venv/bin/agnes`; we pass
    `AGNES_CONFIG_DIR=<tmp>/agnes-config` so this test does not stomp on
    the developer's `~/.config/agnes/`.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_dir = tmp_path / "agnes-config"
    config_dir.mkdir()

    # Always invoke via `python -m cli.main` rather than the .venv/bin/agnes
    # console-script shim. The shim reads `from cli.main import app`, which
    # depends on `_editable_impl_agnes_the_ai_analyst.pth` being on disk and
    # discoverable. On macOS + iCloud Drive, the leading-underscore .pth
    # files get re-hidden by the system between unrelated tasks and the
    # shim then fails with `ModuleNotFoundError: No module named 'cli'`.
    # `python -m` uses the same interpreter without depending on .pth-file
    # visibility for CLI dispatch, so it is robust against that race.
    cmd: list[str] = [sys.executable, "-m", "cli.main"]

    env = os.environ.copy()
    env["AGNES_CONFIG_DIR"] = str(config_dir)
    env["AGNES_LOCAL_DIR"] = str(workspace)

    result = subprocess.run(
        cmd + [
            "init",
            "--server-url", fastapi_test_server.url,
            "--token", test_pat_no_grants,
            "--workspace", str(workspace),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"agnes init failed (exit={result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    return workspace
