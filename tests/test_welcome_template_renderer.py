"""Unit tests for the welcome-prompt renderer."""

import uuid
from pathlib import Path

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.welcome_template import WelcomeTemplateRepository
from src.welcome_template import build_context, render_welcome


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db_path))
    _ensure_schema(c)
    yield c
    c.close()


def _user(email="alice@example.com"):
    return {"id": "u1", "email": email, "name": "Alice", "is_admin": False, "groups": ["Everyone"]}


def test_renders_default_when_no_override(conn):
    out = render_welcome(conn, user=_user(), server_url="https://example.com")
    assert "AI Data Analyst" in out
    assert "https://example.com" in out
    assert "Alice" in out


def test_renders_override(conn):
    WelcomeTemplateRepository(conn).set(
        "# {{ instance.name }} for {{ user.email }}",
        updated_by="admin@example.com",
    )
    out = render_welcome(conn, user=_user(), server_url="https://example.com")
    assert out.startswith("# AI Data Analyst for alice@example.com")


def test_strict_undefined_raises_on_missing_placeholder(conn):
    WelcomeTemplateRepository(conn).set(
        "{{ does_not_exist }}", updated_by="admin@example.com"
    )
    with pytest.raises(Exception) as exc_info:
        render_welcome(conn, user=_user(), server_url="https://example.com")
    assert "does_not_exist" in str(exc_info.value)


def test_context_exposes_documented_keys(conn):
    ctx = build_context(conn, user=_user(), server_url="https://example.com")
    for top in ("instance", "server", "sync_interval", "data_source",
                "tables", "metrics", "marketplaces", "user", "now", "today"):
        assert top in ctx, f"missing top-level key: {top}"


def test_render_marketplaces_filtered_by_rbac(conn, monkeypatch):
    """Two users with different group memberships render different marketplace lists."""
    from app.resource_types import ResourceType

    # ── Seed two marketplaces ────────────────────────────────────────────
    conn.execute(
        """INSERT INTO marketplace_registry (id, name, url) VALUES
           ('mkt-a', 'Marketplace A', 'https://github.com/example/mkt-a'),
           ('mkt-b', 'Marketplace B', 'https://github.com/example/mkt-b')"""
    )
    # Two plugins per marketplace
    for mkt, plugins in [("mkt-a", ["plugin-1", "plugin-2"]), ("mkt-b", ["plugin-3", "plugin-4"])]:
        for p in plugins:
            conn.execute(
                "INSERT INTO marketplace_plugins (marketplace_id, name) VALUES (?, ?)",
                [mkt, p],
            )

    # ── Seed two non-system groups ──────────────────────────────────────
    gid_a = str(uuid.uuid4())
    gid_b = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO user_groups (id, name) VALUES (?, ?), (?, ?)",
        [gid_a, "group-a", gid_b, "group-b"],
    )

    # ── Grant mkt-a/* to group-a and mkt-b/* to group-b ─────────────────
    rtype = ResourceType.MARKETPLACE_PLUGIN.value
    for mkt, gid, plugins in [
        ("mkt-a", gid_a, ["plugin-1", "plugin-2"]),
        ("mkt-b", gid_b, ["plugin-3", "plugin-4"]),
    ]:
        for p in plugins:
            conn.execute(
                "INSERT INTO resource_grants (id, group_id, resource_type, resource_id) "
                "VALUES (?, ?, ?, ?)",
                [str(uuid.uuid4()), gid, rtype, f"{mkt}/{p}"],
            )

    # ── Seed two users, each in their own group + Everyone ───────────────
    everyone_gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = 'Everyone'"
    ).fetchone()[0]

    conn.execute(
        "INSERT INTO users (id, email, name, active) VALUES "
        "('user-a', 'user-a@example.com', 'User A', TRUE), "
        "('user-b', 'user-b@example.com', 'User B', TRUE)"
    )
    for uid, gid in [("user-a", gid_a), ("user-b", gid_b)]:
        conn.execute(
            "INSERT INTO user_group_members (user_id, group_id, source) VALUES (?, ?, ?)",
            [uid, gid, "admin"],
        )
        conn.execute(
            "INSERT INTO user_group_members (user_id, group_id, source) VALUES (?, ?, ?)",
            [uid, everyone_gid, "system_seed"],
        )

    # ── Render for each user ─────────────────────────────────────────────
    WelcomeTemplateRepository(conn).set(
        "{% for m in marketplaces %}{{ m.slug }}: "
        "{% for p in m.plugins %}{{ p.name }} {% endfor %}{% endfor %}",
        updated_by="admin@example.com",
    )

    user_a = {"id": "user-a", "email": "user-a@example.com", "name": "User A", "is_admin": False, "groups": ["group-a"]}
    user_b = {"id": "user-b", "email": "user-b@example.com", "name": "User B", "is_admin": False, "groups": ["group-b"]}

    out_a = render_welcome(conn, user=user_a, server_url="https://example.com")
    out_b = render_welcome(conn, user=user_b, server_url="https://example.com")

    # user-a sees mkt-a plugins only
    assert "mkt-a" in out_a
    assert "plugin-1" in out_a
    assert "mkt-b" not in out_a
    assert "plugin-3" not in out_a

    # user-b sees mkt-b plugins only
    assert "mkt-b" in out_b
    assert "plugin-3" in out_b
    assert "mkt-a" not in out_b
    assert "plugin-1" not in out_b


def test_render_tolerates_missing_optional_tables(tmp_path, monkeypatch):
    """A bare DuckDB without table_registry / marketplace_registry must still render."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "bare.duckdb"
    bare = duckdb.connect(str(db_path))
    # Only seed the welcome_template singleton manually; no other tables.
    bare.execute(
        """CREATE TABLE welcome_template (
            id INTEGER PRIMARY KEY DEFAULT 1,
            content TEXT,
            updated_at TIMESTAMP,
            updated_by VARCHAR
        )"""
    )
    bare.execute("INSERT INTO welcome_template (id, content) VALUES (1, NULL)")

    out = render_welcome(bare, user=_user(), server_url="https://example.com")
    bare.close()
    assert "AI Data Analyst" in out  # default template still renders
    # No tables → "_No tables registered yet_" branch from the default template
    assert "No tables registered yet" in out
