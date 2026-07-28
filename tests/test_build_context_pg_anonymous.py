"""Regression: unauthenticated pages must not leak the admin install-prompt
override on Postgres (PR #878 review).

`_build_context` renders the "Setup a new Claude Code" preview. The DB-backed
path (``render_agent_prompt_banner`` → ``resolve_prompt``) honours an admin
override; the anonymous path (``resolve_lines``) never does. On Postgres a
supplied request conn is ALWAYS None (the system DuckDB is never opened), so the
branch must key off whether the caller *supplied* the conn kwarg — not off
``conn is not None`` — otherwise unauthenticated pages (/login, /first-time-setup,
/login/password), which omit conn, would enter the override path on Postgres
while staying anonymous on DuckDB.

The parity contract asserted here:
  * caller omits conn  → anonymous default on BOTH backends (no override)
  * caller supplies conn (even None, as on Postgres) → DB-backed/override path
"""

from starlette.requests import Request

import app.web.router as router


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/login",
            "headers": [(b"host", b"example.com")],
            "server": ("example.com", 443),
            "scheme": "https",
            "query_string": b"",
        }
    )


def _patch_common(monkeypatch, tmp_path, *, use_pg: bool):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    calls = {"banner": 0}

    def _fake_banner(conn, *, user, server_url):
        calls["banner"] += 1
        return "OVERRIDE_MARKER\nsecond_line"

    # DB-backed override path (the branch we must NOT hit for anonymous pages).
    monkeypatch.setattr("src.welcome_template.render_agent_prompt_banner", _fake_banner)
    # Anonymous default path — return a distinct marker, no heavy machinery.
    monkeypatch.setattr("app.web.setup_instructions.resolve_lines", lambda *a, **k: ["ANON_DEFAULT"])
    monkeypatch.setattr(router, "load_manifest", lambda *a, **k: [])
    monkeypatch.setattr(router, "_read_agnes_ca_pem", lambda *a, **k: None)
    monkeypatch.setattr("app.api.cli_artifacts._find_wheel", lambda *a, **k: None)
    monkeypatch.setattr("src.repositories.use_pg", lambda: use_pg)
    return calls


def test_pg_unauthenticated_page_uses_anonymous_default_not_override(tmp_path, monkeypatch):
    """Postgres + caller omitted conn → anonymous default, override NOT rendered."""
    calls = _patch_common(monkeypatch, tmp_path, use_pg=True)

    ctx = router._build_context(_request())

    assert ctx["setup_instructions_lines"] == ["ANON_DEFAULT"]
    assert calls["banner"] == 0, "override path must not run for an unauthenticated page on Postgres"


def test_pg_conn_supplied_uses_db_backed_override_path(tmp_path, monkeypatch):
    """Postgres + caller supplied conn (None on PG) → DB-backed/override path runs."""
    calls = _patch_common(monkeypatch, tmp_path, use_pg=True)

    ctx = router._build_context(_request(), conn=None)

    assert ctx["setup_instructions_lines"] == ["OVERRIDE_MARKER", "second_line"]
    assert calls["banner"] == 1


def test_duckdb_unauthenticated_page_uses_anonymous_default(tmp_path, monkeypatch):
    """DuckDB + caller omitted conn → anonymous default (parity with Postgres)."""
    calls = _patch_common(monkeypatch, tmp_path, use_pg=False)

    ctx = router._build_context(_request())

    assert ctx["setup_instructions_lines"] == ["ANON_DEFAULT"]
    assert calls["banner"] == 0
