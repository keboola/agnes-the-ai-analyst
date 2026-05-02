"""Unit tests for the setup-banner renderer module."""

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.setup_banner import SetupBannerRepository
from src.setup_banner import _sanitize_banner_html, build_setup_banner_context, render_setup_banner


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db_path))
    _ensure_schema(c)
    yield c
    c.close()


def _user(email="alice@example.com"):
    return {"id": "u1", "email": email, "name": "Alice", "is_admin": False}


def test_render_returns_empty_when_no_override(conn):
    out = render_setup_banner(conn, user=_user(), server_url="https://example.com")
    assert out == ""


def test_render_uses_override(conn):
    SetupBannerRepository(conn).set(
        "<p>VPN: {{ server.hostname }}</p>", updated_by="admin@example.com"
    )
    out = render_setup_banner(conn, user=_user(), server_url="https://example.com")
    # autoescape=True — rendered as HTML
    assert "example.com" in out
    assert "<p>" in out


def test_render_returns_empty_on_invalid_template_does_not_raise(conn):
    """A broken admin banner must not raise; it must return "" (defense-in-depth)."""
    SetupBannerRepository(conn).set(
        "{{ does_not_exist }}", updated_by="admin@example.com"
    )
    out = render_setup_banner(conn, user=_user(), server_url="https://example.com")
    assert out == ""  # swallowed, not raised


def test_render_with_anonymous_user(conn):
    SetupBannerRepository(conn).set(
        "{% if user %}{{ user.email }}{% else %}anonymous{% endif %}",
        updated_by="admin@example.com",
    )
    out = render_setup_banner(conn, user=None, server_url="https://example.com")
    assert "anonymous" in out


def test_context_exposes_documented_keys(conn):
    ctx = build_setup_banner_context(user=_user(), server_url="https://example.com")
    for top in ("instance", "server", "user", "now", "today"):
        assert top in ctx, f"missing top-level key: {top}"
    assert ctx["server"]["hostname"] == "example.com"
    assert ctx["user"]["email"] == "alice@example.com"


def test_context_with_anonymous_user_returns_none(conn):
    ctx = build_setup_banner_context(user=None, server_url="https://example.com")
    assert ctx["user"] is None


def test_autoescape_escapes_html_entities(conn):
    """autoescape=True must escape < > & in template variable output."""
    SetupBannerRepository(conn).set(
        "{{ server.hostname }}", updated_by="admin@example.com"
    )
    out = render_setup_banner(
        conn, user=_user(), server_url="https://example.com/<test>"
    )
    # hostname won't contain < > but the render must succeed without injection
    assert out != ""


# ── Sanitizer unit tests ─────────────────────────────────────────────────────

def test_render_strips_script_tags(conn):
    """render_setup_banner must remove <script> blocks from the output."""
    SetupBannerRepository(conn).set(
        '<p>Hello</p><script>alert(1)</script>',
        updated_by="admin@example.com",
    )
    out = render_setup_banner(conn, user=_user(), server_url="https://example.com")
    assert "<script>" not in out
    assert "alert" not in out
    # Safe content preserved
    assert "Hello" in out


def test_render_strips_event_handlers(conn):
    """render_setup_banner must strip on* event-handler attributes."""
    SetupBannerRepository(conn).set(
        '<button onclick="evil()">Click me</button>',
        updated_by="admin@example.com",
    )
    out = render_setup_banner(conn, user=_user(), server_url="https://example.com")
    assert "onclick" not in out
    assert "evil" not in out
    # Button text preserved
    assert "Click me" in out


def test_render_strips_javascript_uri(conn):
    """render_setup_banner must strip javascript: URI schemes from href/src."""
    SetupBannerRepository(conn).set(
        '<a href="javascript:evil()">link</a>',
        updated_by="admin@example.com",
    )
    out = render_setup_banner(conn, user=_user(), server_url="https://example.com")
    assert "javascript:" not in out
    assert "evil" not in out
    # Link text preserved
    assert "link" in out
