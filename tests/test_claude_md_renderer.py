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


def test_render_override_tables_list(conn):
    # Seed a table registry entry
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t1', 'orders', 'All orders', 'local', 'keboola')"
    )
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
    for key in ("instance", "server", "sync_interval", "data_source", "tables", "metrics", "marketplaces", "user", "now", "today"):
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
    ClaudeMdTemplateRepository(conn).set(
        "{{ does_not_exist }}", updated_by="admin@example.com"
    )
    with pytest.raises(TemplateError):
        render_claude_md(conn, user=_user(), server_url="https://example.com")
