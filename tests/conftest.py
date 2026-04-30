"""Shared test fixtures for E2E tests."""

import os
from pathlib import Path

import duckdb
import pytest

# Ensure consistent JWT secret across all workers (pytest-xdist).
# Set at import time so every worker process picks up the same values
# before any module-level code in app.auth.jwt caches the secret.
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

# Ensure DATA_DIR-derived directories exist for modules that read DATA_DIR
# at import time (e.g. services/telegram_bot/config.py builds NOTIFICATIONS_DIR
# eagerly). The bot itself logs to stdout — there is no FileHandler anymore —
# but the directory still has to exist for the JSON state files.
import tempfile as _tf

if "DATA_DIR" not in os.environ:
    os.environ["DATA_DIR"] = os.path.join(_tf.gettempdir(), ".agnes-test-data")
os.makedirs(os.path.join(os.environ["DATA_DIR"], "notifications"), exist_ok=True)
os.makedirs(os.path.join(os.environ["DATA_DIR"], "state"), exist_ok=True)


@pytest.fixture
def e2e_env(tmp_path, monkeypatch):
    """Set up complete E2E environment with DATA_DIR, create dirs."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    (tmp_path / "extracts").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "state").mkdir()

    yield {
        "data_dir": tmp_path,
        "extracts_dir": tmp_path / "extracts",
        "analytics_db": str(tmp_path / "analytics" / "server.duckdb"),
    }


def create_mock_extract(extracts_dir: Path, source_name: str, tables: list[dict]):
    """Create a mock extract.duckdb with _meta and data tables.

    tables: [{"name": "orders", "data": [{"id": "1", "total": "100"}], "query_mode": "local"}]
    """
    source_dir = extracts_dir / source_name
    source_dir.mkdir(exist_ok=True)
    data_dir = source_dir / "data"
    data_dir.mkdir(exist_ok=True)

    db_path = source_dir / "extract.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute("""CREATE TABLE IF NOT EXISTS _meta (
        table_name VARCHAR, description VARCHAR, rows BIGINT,
        size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR DEFAULT 'local'
    )""")
    # Delete existing meta rows to allow re-calling
    conn.execute("DELETE FROM _meta")

    for t in tables:
        name = t["name"]
        rows_data = t.get("data", [])
        query_mode = t.get("query_mode", "local")

        if rows_data and query_mode == "local":
            # Write actual parquet file
            pq_path = str(data_dir / f"{name}.parquet")
            # Build SQL from data
            selects = []
            for row in rows_data:
                vals = ", ".join(f"'{v}' AS {k}" for k, v in row.items())
                selects.append(f"SELECT {vals}")
            union_sql = " UNION ALL ".join(selects)
            conn.execute(f"COPY ({union_sql}) TO '{pq_path}' (FORMAT PARQUET)")

            rows = len(rows_data)
            size = os.path.getsize(pq_path)
            conn.execute(f"CREATE OR REPLACE VIEW \"{name}\" AS SELECT * FROM read_parquet('{pq_path}')")
            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, 'local')",
                [name, t.get("description", ""), rows, size],
            )
        else:
            # Remote or empty table
            conn.execute(f'CREATE TABLE IF NOT EXISTS "{name}" (id VARCHAR)')
            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, 0, 0, current_timestamp, ?)",
                [name, t.get("description", ""), query_mode],
            )

    conn.close()
    return db_path


def write_test_parquet(path: str, data: list[dict]):
    """Create a parquet file from list of dicts."""
    conn = duckdb.connect()
    selects = []
    for row in data:
        vals = ", ".join(f"'{v}' AS {k}" for k, v in row.items())
        selects.append(f"SELECT {vals}")
    union_sql = " UNION ALL ".join(selects)
    conn.execute(f"COPY ({union_sql}) TO '{path}' (FORMAT PARQUET)")
    conn.close()


@pytest.fixture
def seeded_app(e2e_env):
    """FastAPI TestClient with seeded users + JWT tokens for all four legacy
    role tokens (admin, km_admin, analyst, viewer).

    v13: roles are no longer the auth source of truth. The admin user is
    placed in the Admin user_group; the others are Everyone-only members.
    Tokens for km_admin and viewer are kept so role-gating regression tests
    that still reference them keep passing — gate semantics still match
    where it matters (admin bypass, dataset_permissions checks).
    """
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from app.main import create_app
    from fastapi.testclient import TestClient

    conn = get_system_db()
    repo = UserRepository(conn)
    repo.create(id="admin1", email="admin@test.com", name="Admin", role="admin")
    repo.create(id="km_admin1", email="km@test.com", name="KM Admin", role="km_admin")
    repo.create(id="analyst1", email="analyst@test.com", name="Analyst", role="analyst")
    repo.create(id="viewer1", email="viewer@test.com", name="Viewer", role="viewer")

    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member(
        "admin1",
        admin_gid,
        source="system_seed",
    )
    conn.close()

    app = create_app()
    client = TestClient(app)
    admin_token = create_access_token("admin1", "admin@test.com", "admin")
    km_admin_token = create_access_token("km_admin1", "km@test.com", "km_admin")
    analyst_token = create_access_token("analyst1", "analyst@test.com", "analyst")
    viewer_token = create_access_token("viewer1", "viewer@test.com", "viewer")

    return {
        "client": client,
        "admin_token": admin_token,
        "km_admin_token": km_admin_token,
        "analyst_token": analyst_token,
        "viewer_token": viewer_token,
        "env": e2e_env,
    }


@pytest.fixture
def mock_extract_factory(e2e_env):
    """Factory fixture for creating mock extract.duckdb files.

    Returns a callable: factory(source_name, tables, remote_attach=None)
      - source_name: str — name of the connector source directory
      - tables: list[dict] — same format as create_mock_extract
      - remote_attach: list[dict] | None — rows for _remote_attach table,
        each dict with keys: alias, extension, url, token_env
    """

    def _factory(source_name: str, tables: list[dict], remote_attach=None):
        db_path = create_mock_extract(e2e_env["extracts_dir"], source_name, tables)
        if remote_attach:
            conn = duckdb.connect(str(db_path))
            conn.execute("""CREATE TABLE IF NOT EXISTS _remote_attach (
                alias VARCHAR,
                extension VARCHAR,
                url VARCHAR,
                token_env VARCHAR
            )""")
            for row in remote_attach:
                conn.execute(
                    "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
                    [row["alias"], row["extension"], row["url"], row["token_env"]],
                )
            conn.close()
        return db_path

    return _factory


@pytest.fixture
def analyst_user(seeded_app):
    """Convenience fixture returning analyst auth headers dict."""
    token = seeded_app["analyst_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_user(seeded_app):
    """Convenience fixture returning admin auth headers dict."""
    token = seeded_app["admin_token"]
    return {"Authorization": f"Bearer {token}"}


import contextlib as _contextlib


@pytest.fixture
def bq_access():
    """Build a BqAccess with pluggable factories and override the FastAPI Depends.

    Usage:
        def test_x(bq_access):
            mock_client = MagicMock()
            bq = bq_access(client=mock_client)
            # endpoint test code

    Override is auto-cleared on fixture teardown.

    NOTE: `contextlib.nullcontext(duckdb_conn)` does NOT close the conn on exit.
    The production path closes via _default_duckdb_session_factory. Tests that
    care about close behavior should use that factory directly (see
    tests/test_bq_access.py::TestDefaultDuckdbSessionFactory).
    """
    from connectors.bigquery.access import BqAccess, BqProjects, get_bq_access
    from app.main import app

    def _build(*, client=None, duckdb_conn=None,
               billing="test-billing", data="test-data"):
        bq = BqAccess(
            BqProjects(billing=billing, data=data),
            client_factory=(lambda projects: client) if client is not None else None,
            duckdb_session_factory=(
                lambda projects: _contextlib.nullcontext(duckdb_conn)
            ) if duckdb_conn is not None else None,
        )
        app.dependency_overrides[get_bq_access] = lambda: bq
        return bq

    yield _build
    from app.main import app as _app
    _app.dependency_overrides.pop(get_bq_access, None)
