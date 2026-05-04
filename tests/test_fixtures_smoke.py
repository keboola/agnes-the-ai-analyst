"""Smoke tests for the clean-bootstrap fixtures.

Verifies the fixtures defined in `tests/fixtures/analyst_bootstrap.py`
actually boot a FastAPI server, authenticate sessions, mint usable PATs,
and run `agnes init` end-to-end. Tasks 21 and 22 layer their reader/init
matrices on top of these primitives.
"""

from __future__ import annotations

from pathlib import Path

import httpx


def test_server_boots(fastapi_test_server):
    """The subprocess uvicorn answers /api/health with 200."""
    resp = httpx.get(f"{fastapi_test_server.url}/api/health")
    assert resp.status_code == 200, resp.text


def test_web_session_authenticates(web_session, fastapi_test_server):
    """Admin cookie session can hit an admin-only endpoint.

    GET /api/users requires `require_admin`. A 200 here proves the
    session cookie carries through; a 401/403 would mean the form-login
    fixture is broken.
    """
    resp = web_session.get(f"{fastapi_test_server.url}/api/users")
    assert resp.status_code == 200, (
        f"expected 200, got {resp.status_code}: {resp.text[:300]}"
    )
    payload = resp.json()
    assert isinstance(payload, list)
    emails = {u.get("email") for u in payload}
    # Both seeded users appear in the admin list.
    assert "admin@example.com" in emails
    assert "analyst@example.com" in emails


def test_test_pat_minted(test_pat):
    """test_pat is a non-empty JWT-looking string."""
    assert isinstance(test_pat, str)
    assert len(test_pat) > 20
    # JWT (3 dot-separated base64 segments) — we issue a `typ=pat` JWT.
    assert test_pat.count(".") == 2, "PAT does not look like a JWT"


def test_test_pat_no_grants_minted(test_pat_no_grants):
    """test_pat_no_grants also returns a usable JWT string."""
    assert isinstance(test_pat_no_grants, str)
    assert len(test_pat_no_grants) > 20
    assert test_pat_no_grants.count(".") == 2


def test_test_pat_authenticates_against_server(fastapi_test_server, test_pat):
    """The minted PAT successfully authorizes a /api/catalog/tables call.

    /api/catalog/tables is the same endpoint `agnes init` step 2 hits to
    verify the token, so this is the exact contract the bootstrap path
    needs.
    """
    resp = httpx.get(
        f"{fastapi_test_server.url}/api/catalog/tables",
        headers={"Authorization": f"Bearer {test_pat}"},
    )
    assert resp.status_code == 200, resp.text


def test_zero_grants_workspace_minimal(zero_grants_workspace):
    """`agnes init` with a no-grants PAT produces a minimal workspace.

    Expected files (always written):
    - CLAUDE.md (rendered from /api/welcome)
    - AGNES_WORKSPACE.md (client-side template)
    - .claude/settings.json (model + permissions seed)
    - user/duckdb/analytics.duckdb (load-bearing artifact for downstream
      readers, even with zero parquets)

    Expected absences (no grants → empty manifest):
    - server/parquet/ — lazy mkdir, only created when a parquet is
      written (none with zero grants).
    - .claude/rules/ — lazy mkdir, only created when the memory bundle
      has at least one mandatory item or non-empty approved list.
    """
    ws = Path(zero_grants_workspace)
    assert (ws / "CLAUDE.md").exists(), "CLAUDE.md missing"
    assert (ws / "AGNES_WORKSPACE.md").exists(), "AGNES_WORKSPACE.md missing"
    assert (ws / ".claude" / "settings.json").exists(), "settings.json missing"
    assert (ws / "user" / "duckdb" / "analytics.duckdb").exists(), (
        "analytics.duckdb missing — downstream `agnes query` won't work"
    )

    assert not (ws / "server" / "parquet").exists(), (
        "zero grants should produce no parquets"
    )
    assert not (ws / ".claude" / "rules").exists(), (
        "zero rules should leave the rules directory absent"
    )
