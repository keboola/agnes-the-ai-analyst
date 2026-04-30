"""Unit tests for the welcome-prompt renderer."""

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
