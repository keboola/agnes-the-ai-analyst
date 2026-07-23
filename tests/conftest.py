"""Shared test fixtures for E2E tests."""

import contextlib as _contextlib
import hashlib as _hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock

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

# Per-xdist-worker isolation: the on-disk system.duckdb takes an exclusive
# file lock, so two workers sharing one DATA_DIR race on every
# get_system_db() open (sporadic "Could not set lock" failures whose
# incidence depends on test scheduling). The xdist controller imports this
# conftest first and its DATA_DIR is INHERITED by worker processes, so the
# worker suffix must be applied even when DATA_DIR is already set — but only
# when it points at our shared default, never at an operator-provided path.
_default_data_dir = os.path.join(_tf.gettempdir(), ".agnes-test-data")
_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER", "")
if "DATA_DIR" not in os.environ:
    os.environ["DATA_DIR"] = _default_data_dir
if _xdist_worker and os.path.normpath(os.environ["DATA_DIR"]) == _default_data_dir:
    os.environ["DATA_DIR"] = os.path.join(_default_data_dir, _xdist_worker)
os.makedirs(os.path.join(os.environ["DATA_DIR"], "notifications"), exist_ok=True)
os.makedirs(os.path.join(os.environ["DATA_DIR"], "state"), exist_ok=True)

# Real-home shell configs that `agnes init` (cli/lib/shortcut.py) can append
# launcher blocks to. Resolved at import time — before any test monkeypatches
# HOME — so the guard below always watches the developer's *actual* rc files,
# not a per-test fake home.

_REAL_HOME = Path(os.path.expanduser("~"))
_GUARDED_SHELL_CONFIGS = (
    _REAL_HOME / ".zshrc",
    _REAL_HOME / ".bashrc",
    _REAL_HOME / ".bash_profile",
    _REAL_HOME / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
    _REAL_HOME / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
)
# The launcher install dir (`~/.local/bin`) is watched by *name listing* only:
# a leaking test drops a new script named after its tmp workspace there.
_REAL_LOCAL_BIN = _REAL_HOME / ".local" / "bin"


def _shell_config_fingerprints() -> dict:
    fps = {}
    for path in _GUARDED_SHELL_CONFIGS:
        try:
            fps[path] = _hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            fps[path] = None
        except OSError:
            # Unreadable (permissions, etc.) — treat as opaque-but-stable.
            fps[path] = "<unreadable>"
    try:
        fps[_REAL_LOCAL_BIN] = tuple(sorted(os.listdir(_REAL_LOCAL_BIN)))
    except OSError:
        fps[_REAL_LOCAL_BIN] = None
    return fps


@pytest.fixture(autouse=True)
def _guard_real_shell_config():
    """Fail any test that mutates the developer's real shell rc files.

    Tests that exercise `agnes init` / `install_launcher_shortcut` must
    redirect writes into tmp (monkeypatch.setenv("HOME", ...) for in-process
    calls, env["HOME"] = <tmp> for subprocess calls) or pass --no-shortcut.
    Forgetting either silently appends per-test launcher blocks to the
    developer's real ~/.zshrc — this guard turns that leak into a loud
    failure at the offending test.

    Under pytest-xdist a leak in a concurrently running test on another
    worker can, rarely, be blamed on the wrong test — but any failure here
    still means some test in the run is leaking.
    """
    before = _shell_config_fingerprints()
    yield
    after = _shell_config_fingerprints()
    changed = [str(p) for p in before if before[p] != after[p]]
    if changed:
        pytest.fail(
            "This test wrote to the developer's REAL shell config / launcher dir: "
            + ", ".join(changed)
            + ". Redirect HOME into tmp (monkeypatch.setenv('HOME', str(tmp_path)) "
            "or env['HOME'] for subprocesses) or pass --no-shortcut to `agnes init`."
        )


@pytest.fixture(autouse=True)
def _flea_guardrails_disabled_by_default(monkeypatch):
    """Default flea-market upload pipeline to OFF for every test.

    Post-v45 publish-gate refactor split operator intent
    (``guardrails.enabled`` in instance.yaml) from provider readiness
    (``ANTHROPIC_API_KEY`` in env). Both default to True/False in a
    test env that has no instance.yaml + no key — so the gate is now
    ``enabled=True, ready=False`` and every upload sits at
    ``visibility_status='pending'`` waiting on a non-existent LLM
    call. That breaks every legacy test that uploads a bundle and
    expects v1 to be live.

    Default both to False here so legacy tests keep working. Tests
    that exercise the guardrail-on path override per-test with
    ``monkeypatch.setattr("app.api.store.get_guardrails_enabled",
    lambda: True)`` + the matching ``..._llm_provider_ready`` line.
    """
    try:
        # `app.api.store` does a top-level import — patch the bound
        # symbol there. Existing per-test overrides target the same path.
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: False,
        )
    except (AttributeError, ImportError):
        # app.api.store may not be importable in some test contexts
        # (e.g. tests that exercise migrations without the full app).
        pass
    try:
        # `app.api.admin` does a function-local import — patch the
        # source so per-call lookups see the override.
        monkeypatch.setattr(
            "app.instance_config.get_guardrails_enabled",
            lambda: False,
        )
    except (AttributeError, ImportError):
        pass


@pytest.fixture(autouse=True)
def _disable_auth_rate_limit_in_tests():
    """Disable the slowapi auth rate limiter for every test by default.

    Production limits (e.g. 10/minute on /auth/password/login) would otherwise
    bleed into test files that hammer auth endpoints in tight loops — those
    tests existed long before the limiter and shouldn't have to know about
    its bucket sizes. The dedicated rate-limit test in test_auth_rate_limit.py
    flips ``limiter.enabled = True`` and resets state inside its own scope.
    """
    from app.auth.rate_limit import limiter

    was_enabled = limiter.enabled
    limiter.enabled = False
    try:
        limiter.reset()
    except Exception:
        # In-memory backend always resets cleanly; defensive guard for
        # third-party storage backends operators might wire in later.
        pass
    yield
    limiter.enabled = was_enabled


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Reset module-level caches that survive across tests on the same
    pytest-xdist worker process. Without this, a test that populates
    `app.instance_config._instance_config` (e.g. via `runpy.run_module`
    in test_bigquery_extractor's __main__ tests, or via any path that
    calls `app.instance_config.get_value`) leaves stale config visible
    to the next test on that worker — including config that points at
    a different DATA_DIR than the next test's e2e_env set.

    Caches reset:
    - app.instance_config._instance_config — instance.yaml deep-merge cache
    - get_bq_access (functools.cache) — BqAccess(BqProjects(...)) lru
    - app.api.v2_quota._quota_singleton — per-user quota tracker

    Pre-existing flakiness; surfaced by issue #160 PR #168 shifting the
    test bucket distribution on xdist worker gw2.
    """
    try:
        import app.instance_config as _ic

        _ic._instance_config = None
        try:
            from connectors.bigquery.access import get_bq_access

            get_bq_access.cache_clear()
        except (ImportError, AttributeError):
            pass
    except ImportError:
        pass
    try:
        import app.api.v2_quota as _q

        _q._quota_singleton = None
    except ImportError:
        pass
    # Backend-state parse-once cache — process-global, so a test
    # that reads/writes one overlay would otherwise leave a stale
    # (BackendState, url) visible to the next test on this xdist worker.
    try:
        from src.db_state_machine import reset_backend_state_cache

        reset_backend_state_cache()
    except ImportError:
        pass
    try:
        from app.api import v2_catalog as _vc

        _vc._table_rows_cache.clear()
    except (ImportError, AttributeError):
        pass
    try:
        import app.api.cache_warmup as _cw

        _cw.WARMUP_STATE = None
    except (ImportError, AttributeError):
        pass
    yield
    try:
        from app.api import v2_catalog as _vc

        _vc._table_rows_cache.clear()
    except (ImportError, AttributeError):
        pass
    try:
        import app.api.cache_warmup as _cw

        _cw.WARMUP_STATE = None
    except (ImportError, AttributeError):
        pass


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
    repo.create(id="admin1", email="admin@test.com", name="Admin")
    repo.create(id="km_admin1", email="km@test.com", name="KM Admin")
    repo.create(id="analyst1", email="analyst@test.com", name="Analyst")
    repo.create(id="viewer1", email="viewer@test.com", name="Viewer")

    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member(
        "admin1",
        admin_gid,
        source="system_seed",
    )
    conn.close()

    app = create_app()
    client = TestClient(app)
    admin_token = create_access_token("admin1", "admin@test.com")
    km_admin_token = create_access_token("km_admin1", "km@test.com")
    analyst_token = create_access_token("analyst1", "analyst@test.com")
    viewer_token = create_access_token("viewer1", "viewer@test.com")

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

    def _build(*, client=None, duckdb_conn=None, billing="test-billing", data="test-data"):
        bq = BqAccess(
            BqProjects(billing=billing, data=data),
            client_factory=(lambda projects: client) if client is not None else None,
            duckdb_session_factory=(lambda projects: _contextlib.nullcontext(duckdb_conn))
            if duckdb_conn is not None
            else None,
        )
        app.dependency_overrides[get_bq_access] = lambda: bq
        return bq

    yield _build
    from app.main import app as _app

    _app.dependency_overrides.pop(get_bq_access, None)


# ---------------------------------------------------------------------------
# Clean-bootstrap test suite (Task 20).
#
# Re-export the analyst-bootstrap fixtures so individual test modules can
# request them by name without an explicit import. Imported at module level
# so pytest collection sees the names; the fixtures themselves don't run
# until a test pulls them in.
# ---------------------------------------------------------------------------
from tests.fixtures.analyst_bootstrap import (  # noqa: E402,F401
    NONEXISTENT_TABLE,
    fastapi_test_server,
    test_pat,
    test_pat_no_grants,
    web_session,
    zero_grants_workspace,
)


@pytest.fixture
def bq_instance(monkeypatch):
    """Force instance.yaml to look like a BigQuery deployment for the
    duration of one test. Patches the cached load_instance_config so
    /admin/server-config reads / get_value('data_source.bigquery.project')
    return what we want, without touching the on-disk instance.yaml.

    Tests that need BigQuery-specific admin API behaviour (project_id
    validation, materialized source_query checks, etc.) depend on this
    fixture. Yields the fake config dict so callers can inspect it.

    Note: several test files (test_admin_bq_register.py,
    test_admin_tables_ui_materialized.py, …) define their own local
    ``bq_instance`` fixture. Those local definitions shadow this one
    inside those files — the conftest copy is the canonical provider for
    any new test file that imports from this module."""
    fake_cfg = {
        "data_source": {
            "type": "bigquery",
            "bigquery": {"project": "my-test-project", "location": "us"},
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    yield fake_cfg
    reset_cache()


@pytest.fixture
def stub_bq_extractor(monkeypatch):
    """Mirror tests/test_admin_bq_register.py — bypasses real-BQ traffic
    in the post-register rebuild path so the test stays offline. Required
    whenever the test seeds a remote-mode BQ row via the HTTP API.

    Patches:
    - ``connectors.bigquery.extractor.rebuild_from_registry`` — returns a
      minimal success dict so the admin register endpoint's 200/201 path
      completes without touching a real BQ project.
    - ``src.orchestrator.SyncOrchestrator`` — replaced with a no-op mock so
      the post-register orchestrator.rebuild() call doesn't scan the
      (empty) extracts directory during tests.

    Returns the ``rebuild_from_registry`` MagicMock directly so callers
    that only need the side-effect patcher can ignore the return value,
    and callers that want to assert call args can inspect it."""
    rebuild_mock = MagicMock(
        return_value={
            "project_id": "my-test-project",
            "tables_registered": 1,
            "errors": [],
            "skipped": False,
        }
    )
    monkeypatch.setattr(
        "connectors.bigquery.extractor.rebuild_from_registry",
        rebuild_mock,
    )
    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator",
        lambda *a, **kw: MagicMock(),
    )
    return rebuild_mock


def grant_table_via_package(
    conn,
    table_id: str,
    user_id: str,
    *,
    group_name: str = "analyst-pkg-grants",
    requirement: str = "required",
) -> str:
    """Test helper — wrap a single table in an auto-named data_package and
    grant the package to a custom group the user belongs to.

    Replaces the legacy "per-table resource_grants" pattern: stack-gated
    RBAC routes all analyst visibility through data_packages, so a
    standalone TABLE grant no longer surfaces the table to the analyst.
    Returns the data_package id so callers can revoke (DELETE package
    → tables_in_package + grants cascade) or assert membership.

    Defaults to ``requirement='required'`` so the wrapping package
    lands in the user's stack automatically — every existing test that
    just asserted "table visible after grant" stays correct without
    needing an explicit subscribe step.
    """
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.data_packages import DataPackagesRepository

    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name)
    if not grp:
        grp = groups.create(
            name=group_name,
            description="test",
            created_by="test",
        )
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(
            user_id,
            grp["id"],
            source="admin",
            added_by="test",
        )

    pkgs = DataPackagesRepository(conn)
    pkg_slug = f"_test-pkg-{table_id.lower()}"[:63]
    existing = pkgs.get_by_slug(pkg_slug) if hasattr(pkgs, "get_by_slug") else None
    if existing:
        pkg_id = existing["id"]
    else:
        pkg_id = pkgs.create(
            name=f"Test wrap {table_id}",
            slug=pkg_slug,
            description=None,
            icon=None,
            color=None,
            created_by="test",
        )
    pkgs.add_table(pkg_id, table_id, added_by="test")

    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "data_package", pkg_id):
        grants.create(
            group_id=grp["id"],
            resource_type="data_package",
            resource_id=pkg_id,
            assigned_by="test",
            requirement=requirement,
        )
    return pkg_id


def revoke_table_via_package(conn, table_id: str) -> None:
    """Mirror of :func:`grant_table_via_package` — drops the wrapping
    data_packages (and via FK cascade the junction + grants) for every
    auto-package that wraps this table.
    """
    rows = conn.execute(
        "SELECT DISTINCT package_id FROM data_package_tables WHERE table_id = ?",
        [table_id],
    ).fetchall()
    for r in rows:
        # Hard-delete via raw SQL so the test fixture doesn't leak rows
        # across tests sharing the seeded_app DB.
        conn.execute(
            "DELETE FROM resource_grants WHERE resource_type = 'data_package' AND resource_id = ?",
            [r[0]],
        )
        conn.execute(
            "DELETE FROM data_package_tables WHERE package_id = ?",
            [r[0]],
        )
        conn.execute("DELETE FROM data_packages WHERE id = ?", [r[0]])
