"""Unit tests for the analyst-workspace CLAUDE.md renderer (src/claude_md.py)."""

import duckdb
import pytest
from jinja2 import TemplateError

from src.db import _ensure_schema
from src.repositories.claude_md_template import ClaudeMdTemplateRepository
from src.claude_md import (
    build_claude_md_context,
    compute_default_claude_md,
    render_claude_md,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db_path))
    _ensure_schema(c)
    # RBAC reads that go through the repository factory (e.g.
    # get_accessible_tables → data_packages_repo()) resolve their connection
    # via src.repositories.get_system_db, NOT the conn passed in here. Redirect
    # that name to this fixture's connection so the factory reads the same DB
    # the test seeds — otherwise the factory opens a separate (empty) system DB
    # and package-scoped grants resolve to nothing.
    monkeypatch.setattr("src.repositories.get_system_db", lambda: c)
    yield c
    c.close()


def _user(email="alice@example.com", is_admin=False):
    return {
        "id": "u1",
        "email": email,
        "name": "Alice",
        "is_admin": is_admin,
        "groups": ["Everyone"],
    }


# ---------------------------------------------------------------------------
# Default (no override) — renders a non-empty markdown string
# ---------------------------------------------------------------------------


def test_compute_default_returns_non_empty(conn):
    out = compute_default_claude_md(conn, user=_user(), server_url="https://example.com")
    assert out.strip() != ""


def test_default_contains_server_url(conn):
    out = compute_default_claude_md(conn, user=_user(), server_url="https://myagnes.example.com")
    assert "https://myagnes.example.com" in out


def test_default_contains_user_reference(conn):
    # The footer uses `user.name or user.email` — a user with no name falls back to email.
    user_no_name = {"id": "u1", "email": "bob@example.com", "name": "", "is_admin": False, "groups": []}
    out = compute_default_claude_md(conn, user=user_no_name, server_url="https://example.com")
    assert "bob@example.com" in out


def test_default_private_sessions_policy_is_user_only(conn):
    """Pin the private-sessions policy copy in the default workspace CLAUDE.md
    (config/claude_md_template.txt): transcript upload is designed behavior,
    and marking a session private is exclusively the analyst's own deliberate
    action — the agent may SUGGEST `/agnes-private`, never invoke it. Guards
    against the copy drifting back to auto-marking guidance."""
    out = compute_default_claude_md(conn, user=_user(), server_url="https://example.com")
    assert "the product's designed behavior" in out
    assert "scrubbed client-side" in out
    assert "they type `/agnes-private` themselves" in out
    assert "SUGGEST the command" in out
    assert "never mark a session private" in out
    assert "run `agnes mark-private`" in out
    assert "auto-mark" not in out


def test_render_uses_default_when_no_override(conn):
    out = render_claude_md(conn, user=_user(), server_url="https://example.com")
    assert out.strip() != ""


# ---------------------------------------------------------------------------
# Override renders correctly
# ---------------------------------------------------------------------------


def test_render_uses_override_when_set(conn):
    ClaudeMdTemplateRepository(conn).set(
        "# {{ instance.name }} Workspace\n\nHello {{ user.email }}.",
        updated_by="admin@example.com",
    )
    out = render_claude_md(conn, user=_user("charlie@example.com"), server_url="https://example.com")
    assert "charlie@example.com" in out


def test_render_with_conn_none_still_applies_override(conn):
    """`render_claude_md` — unlike `build_zip` — never gates the override
    check on `conn` truthiness; `resolve_prompt`/`build_claude_md_context`
    resolve everything through the backend-aware factory regardless. This
    locks in that `conn=None` (the app/main.py cloud-chat workdir path,
    which avoids force-opening the process-singleton DuckDB connection)
    still applies the admin's editor override, matching the conn-supplied
    call exactly."""
    ClaudeMdTemplateRepository(conn).set(
        "# {{ instance.name }} Workspace\n\nHello {{ user.email }}.",
        updated_by="admin@example.com",
    )
    with_conn = render_claude_md(conn, user=_user("charlie@example.com"), server_url="https://example.com")
    without_conn = render_claude_md(None, user=_user("charlie@example.com"), server_url="https://example.com")
    assert "charlie@example.com" in without_conn
    assert without_conn == with_conn


def test_render_override_tables_list(conn):
    # Seed a table registry entry and ensure the test user is an admin so
    # RBAC filtering does not hide the table.
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t1', 'orders', 'All orders', 'local', 'keboola')"
    )
    from src.repositories.users import UserRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    UserRepository(conn).create(id="u1", email="alice@example.com", name="Alice")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]
    UserGroupMembersRepository(conn).add_member("u1", admin_gid, source="admin")
    ClaudeMdTemplateRepository(conn).set(
        "{% for t in tables %}- {{ t.name }}: {{ t.description }}{% endfor %}",
        updated_by="admin@example.com",
    )
    out = render_claude_md(conn, user=_user(), server_url="https://example.com")
    assert "orders" in out
    assert "All orders" in out


def test_render_override_metrics_summary(conn):
    # Seed a metric definition — must include NOT NULL columns: display_name, sql
    conn.execute(
        "INSERT INTO metric_definitions (id, name, display_name, category, sql) "
        "VALUES ('m1', 'mrr', 'MRR', 'revenue', 'SELECT SUM(amount)')"
    )
    ClaudeMdTemplateRepository(conn).set(
        "Metrics: {{ metrics.count }}, cats: {{ metrics.categories | join(', ') }}",
        updated_by="admin@example.com",
    )
    out = render_claude_md(conn, user=_user(), server_url="https://example.com")
    assert "1" in out  # 1 metric
    assert "revenue" in out


# ---------------------------------------------------------------------------
# RBAC-filtered marketplaces — two users with different grants render differently
# ---------------------------------------------------------------------------


def test_marketplaces_empty_for_user_with_no_grants(conn):
    # No grants seeded — _marketplaces_for_user returns []
    ClaudeMdTemplateRepository(conn).set(
        "{% if marketplaces %}HAS_PLUGINS{% else %}NO_PLUGINS{% endif %}",
        updated_by="admin@example.com",
    )
    out = render_claude_md(conn, user=_user(), server_url="https://example.com")
    assert "NO_PLUGINS" in out


# ---------------------------------------------------------------------------
# Anonymous / minimal user context
# ---------------------------------------------------------------------------


def test_render_with_minimal_user_context(conn):
    """Templates referencing user fields must work with minimal user dict."""
    ClaudeMdTemplateRepository(conn).set(
        "User: {{ user.email }}, admin: {{ user.is_admin }}",
        updated_by="admin@example.com",
    )
    out = render_claude_md(conn, user=_user(), server_url="https://example.com")
    assert "alice@example.com" in out
    assert "False" in out


# ---------------------------------------------------------------------------
# Build context shape
# ---------------------------------------------------------------------------


def test_context_exposes_all_documented_keys(conn):
    ctx = build_claude_md_context(conn, user=_user(), server_url="https://example.com")
    for key in (
        "instance",
        "server",
        "sync_interval",
        "data_source",
        "tables",
        "metrics",
        "marketplaces",
        "user",
        "now",
        "today",
    ):
        assert key in ctx, f"missing context key: {key}"


def test_context_tables_is_list(conn):
    ctx = build_claude_md_context(conn, user=_user(), server_url="https://example.com")
    assert isinstance(ctx["tables"], list)


def test_context_metrics_shape(conn):
    ctx = build_claude_md_context(conn, user=_user(), server_url="https://example.com")
    assert "count" in ctx["metrics"]
    assert "categories" in ctx["metrics"]


def test_context_marketplaces_is_list(conn):
    ctx = build_claude_md_context(conn, user=_user(), server_url="https://example.com")
    assert isinstance(ctx["marketplaces"], list)


# ---------------------------------------------------------------------------
# Render failure raises (caller handles)
# ---------------------------------------------------------------------------


def test_render_raises_on_template_error(conn):
    ClaudeMdTemplateRepository(conn).set("{{ does_not_exist }}", updated_by="admin@example.com")
    with pytest.raises(TemplateError):
        render_claude_md(conn, user=_user(), server_url="https://example.com")


# ---------------------------------------------------------------------------
# RBAC-filtered tables — two users with different grants see different tables
# ---------------------------------------------------------------------------


def _make_user(conn, *, user_id: str, email: str) -> None:
    from src.repositories.users import UserRepository

    UserRepository(conn).create(id=user_id, email=email, name=email.split("@")[0])


def _make_group(conn, *, name: str) -> str:
    from src.repositories.user_groups import UserGroupsRepository

    return UserGroupsRepository(conn).create(name=name)["id"]


def _add_member(conn, *, user_id: str, group_id: str) -> None:
    from src.repositories.user_group_members import UserGroupMembersRepository

    UserGroupMembersRepository(conn).add_member(user_id, group_id, source="admin")


def _grant_table(conn, *, group_id: str, table_id: str) -> None:
    """Stack-gated RBAC: wrap ``table_id`` in an auto data_package and
    grant the package to ``group_id`` with ``requirement='required'``
    so every user in the group has the package in their stack."""
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.data_packages import DataPackagesRepository

    pkgs = DataPackagesRepository(conn)
    pkg_slug = f"_test-pkg-{table_id.lower()}"[:63]
    existing = pkgs.get_by_slug(pkg_slug)
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
    ResourceGrantsRepository(conn).create(
        group_id=group_id,
        resource_type="data_package",
        resource_id=pkg_id,
        requirement="required",
    )


def test_render_tables_filtered_by_rbac(conn):
    """Non-admin users see only tables granted to their groups."""
    # Seed two tables
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-a', 'orders', 'Order data', 'local', 'keboola')"
    )
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-b', 'revenue', 'Revenue data', 'local', 'keboola')"
    )

    # Two users, two groups
    _make_user(conn, user_id="ua", email="alice@example.com")
    _make_user(conn, user_id="ub", email="bob@example.com")
    gid_a = _make_group(conn, name="group-a")
    gid_b = _make_group(conn, name="group-b")
    _add_member(conn, user_id="ua", group_id=gid_a)
    _add_member(conn, user_id="ub", group_id=gid_b)

    # Grant: group-a → t-a, group-b → t-b
    _grant_table(conn, group_id=gid_a, table_id="t-a")
    _grant_table(conn, group_id=gid_b, table_id="t-b")

    user_a = {"id": "ua", "email": "alice@example.com", "name": "Alice", "is_admin": False, "groups": []}
    user_b = {"id": "ub", "email": "bob@example.com", "name": "Bob", "is_admin": False, "groups": []}

    ctx_a = build_claude_md_context(conn, user=user_a, server_url="https://example.com")
    table_names_a = {t["name"] for t in ctx_a["tables"]}
    assert "orders" in table_names_a
    assert "revenue" not in table_names_a

    ctx_b = build_claude_md_context(conn, user=user_b, server_url="https://example.com")
    table_names_b = {t["name"] for t in ctx_b["tables"]}
    assert "revenue" in table_names_b
    assert "orders" not in table_names_b


def test_render_tables_admin_sees_all(conn):
    """Admin users see all tables regardless of grants."""
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-x', 'alpha', 'Alpha table', 'local', 'keboola')"
    )
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-y', 'beta', 'Beta table', 'local', 'keboola')"
    )

    # Admin user: member of the Admin system group
    _make_user(conn, user_id="u-admin", email="admin@example.com")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]
    _add_member(conn, user_id="u-admin", group_id=admin_gid)

    user_admin = {"id": "u-admin", "email": "admin@example.com", "name": "Admin", "is_admin": True, "groups": []}
    ctx = build_claude_md_context(conn, user=user_admin, server_url="https://example.com")
    table_names = {t["name"] for t in ctx["tables"]}
    assert "alpha" in table_names
    assert "beta" in table_names


def test_render_tables_empty_for_user_with_no_grants(conn):
    """Non-admin with no grants sees no tables."""
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-z', 'secret', 'Secret table', 'local', 'keboola')"
    )
    _make_user(conn, user_id="u-none", email="none@example.com")
    user_none = {"id": "u-none", "email": "none@example.com", "name": "None", "is_admin": False, "groups": []}
    ctx = build_claude_md_context(conn, user=user_none, server_url="https://example.com")
    assert ctx["tables"] == []
