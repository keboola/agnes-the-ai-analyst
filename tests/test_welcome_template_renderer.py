"""Unit tests for the agent-setup-prompt renderer."""

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.welcome_template import WelcomeTemplateRepository
from src.welcome_template import (
    _sanitize_banner_html,
    build_context,
    compute_default_agent_prompt,
    render_agent_prompt_banner,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db_path))
    _ensure_schema(c)
    yield c
    c.close()


def _user(email="alice@example.com"):
    return {
        "id": "u1",
        "email": email,
        "name": "Alice",
        "is_admin": False,
        "groups": ["Everyone"],
    }


# ---------------------------------------------------------------------------
# Default (no override) → live setup script, not empty string
# ---------------------------------------------------------------------------

def test_returns_default_script_when_no_override(conn):
    """When no override is set, render_agent_prompt_banner returns the live
    setup script (not an empty string)."""
    out = render_agent_prompt_banner(conn, user=_user(), server_url="https://example.com")
    # Must be non-empty — the default IS the setup script
    assert out != ""
    # Must contain key markers from setup_instructions.resolve_lines()
    assert "da analyst setup" in out or "da auth" in out or "curl" in out


def test_compute_default_returns_setup_script(conn):
    """compute_default_agent_prompt returns a non-empty string with setup
    script markers including {server_url} and da-related commands."""
    out = compute_default_agent_prompt(conn, user=_user(), server_url="https://example.com")
    assert out != ""
    # {server_url} placeholder must survive (not replaced by Jinja2)
    assert "{server_url}" in out
    # Should reference da auth or CLI install
    assert "da auth" in out or "uv tool install" in out


def test_compute_default_server_url_placeholder_survives(conn):
    """{server_url} and {token} are single-brace JS placeholders.
    compute_default_agent_prompt must NOT replace them — they stay literal."""
    out = compute_default_agent_prompt(conn, user=_user(), server_url="https://example.com")
    assert "{server_url}" in out
    assert "{token}" in out


def test_returns_empty_for_none_user_with_no_override(conn):
    """Anonymous visitor with no override → still returns the default script."""
    out = render_agent_prompt_banner(conn, user=None, server_url="https://example.com")
    # No override → default (non-empty bash script)
    assert out != ""


# ---------------------------------------------------------------------------
# Override renders correctly
# ---------------------------------------------------------------------------

def test_renders_override(conn):
    WelcomeTemplateRepository(conn).set(
        "<p>Welcome to {{ instance.name }}!</p>",
        updated_by="admin@example.com",
    )
    out = render_agent_prompt_banner(conn, user=_user(), server_url="https://example.com")
    assert "<p>Welcome to" in out
    # instance.name comes from instance_config — any non-empty string is fine
    assert "!" in out


def test_renders_user_placeholder(conn):
    WelcomeTemplateRepository(conn).set(
        "<p>Hello {{ user.email }}</p>",
        updated_by="admin@example.com",
    )
    out = render_agent_prompt_banner(
        conn, user=_user("bob@example.com"), server_url="https://example.com"
    )
    assert "bob@example.com" in out


def test_renders_server_placeholder(conn):
    WelcomeTemplateRepository(conn).set(
        "<p>Server: {{ server.url }}</p>",
        updated_by="admin@example.com",
    )
    out = render_agent_prompt_banner(
        conn, user=_user(), server_url="https://myserver.example.com"
    )
    assert "https://myserver.example.com" in out


# ---------------------------------------------------------------------------
# Anonymous user (user=None)
# ---------------------------------------------------------------------------

def test_renders_with_anonymous_user(conn):
    WelcomeTemplateRepository(conn).set(
        "{% if user %}<p>Hi {{ user.email }}</p>{% else %}<p>Please sign in.</p>{% endif %}",
        updated_by="admin@example.com",
    )
    out = render_agent_prompt_banner(conn, user=None, server_url="https://example.com")
    assert "Please sign in." in out
    assert "Hi" not in out


# ---------------------------------------------------------------------------
# Build context shape
# ---------------------------------------------------------------------------

def test_context_exposes_documented_keys():
    ctx = build_context(user=_user(), server_url="https://example.com")
    for key in ("instance", "server", "user", "now", "today"):
        assert key in ctx, f"missing context key: {key}"
    assert "tables" not in ctx
    assert "metrics" not in ctx
    assert "marketplaces" not in ctx
    assert "sync_interval" not in ctx
    assert "data_source" not in ctx


def test_context_user_none():
    ctx = build_context(user=None, server_url="https://example.com")
    assert ctx["user"] is None


def test_context_instance_keys():
    ctx = build_context(user=_user(), server_url="https://example.com")
    assert "name" in ctx["instance"]
    assert "subtitle" in ctx["instance"]


def test_context_server_keys():
    ctx = build_context(user=_user(), server_url="https://example.com")
    assert ctx["server"]["url"] == "https://example.com"
    assert ctx["server"]["hostname"] == "example.com"


# ---------------------------------------------------------------------------
# HTML sanitization
# ---------------------------------------------------------------------------

def test_sanitize_strips_script_tag():
    html = '<p>Hello</p><script>alert("xss")</script>'
    result = _sanitize_banner_html(html)
    assert "<script>" not in result
    assert "alert" not in result
    assert "<p>Hello</p>" in result


def test_sanitize_strips_script_with_attributes():
    html = '<script type="text/javascript">evil()</script><p>ok</p>'
    result = _sanitize_banner_html(html)
    assert "evil" not in result
    assert "<p>ok</p>" in result


def test_sanitize_strips_iframe():
    html = '<p>text</p><iframe src="https://evil.example.com"></iframe>'
    result = _sanitize_banner_html(html)
    assert "<iframe" not in result
    assert "<p>text</p>" in result


def test_sanitize_strips_event_handlers():
    html = '<button onclick="evil()">Click me</button>'
    result = _sanitize_banner_html(html)
    assert "onclick" not in result
    assert "evil" not in result
    assert "Click me" in result


def test_sanitize_strips_onload_on_img():
    html = '<img src="x" onload="steal()" alt="test">'
    result = _sanitize_banner_html(html)
    assert "onload" not in result
    assert "steal" not in result


def test_sanitize_strips_javascript_uri():
    html = '<a href="javascript:alert(1)">click</a>'
    result = _sanitize_banner_html(html)
    assert "javascript:" not in result


def test_sanitize_allows_safe_html():
    html = "<p>VPN required. Contact <a href='https://support.example.com'>support</a>.</p>"
    result = _sanitize_banner_html(html)
    assert "<p>" in result
    assert "<a href" in result
    assert "support" in result


# ---------------------------------------------------------------------------
# Render failure → empty string (not exception)
# ---------------------------------------------------------------------------

def test_render_failure_falls_back_to_default_not_exception(conn):
    # StrictUndefined: referencing an unknown variable raises at render time.
    WelcomeTemplateRepository(conn).set(
        "{{ does_not_exist }}", updated_by="admin@example.com"
    )
    out = render_agent_prompt_banner(conn, user=_user(), server_url="https://example.com")
    # Must not raise — falls back to the live default script (non-empty)
    assert out != ""
    # Must contain default setup script markers
    assert "da auth" in out or "uv tool install" in out or "curl" in out


def test_sanitize_applied_after_render(conn):
    """A template that produces <script> output is sanitized before return."""
    WelcomeTemplateRepository(conn).set(
        "<script>evil()</script><p>safe content</p>",
        updated_by="admin@example.com",
    )
    out = render_agent_prompt_banner(conn, user=_user(), server_url="https://example.com")
    assert "<script>" not in out
    assert "evil" not in out
    assert "<p>safe content</p>" in out
