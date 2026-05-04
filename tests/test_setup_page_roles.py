"""Tests for /setup role query-param branching.

Task 4 wires `?role=analyst|admin` through the /setup route handler so the
template can render two role tiles and the renderer can pick the right
layout (admin = full marketplace/skills/diagnose flow; analyst = trimmed
workspace-bootstrap flow). Default is `admin` to preserve existing behavior.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient against a freshly-built FastAPI app rooted at tmp_path.

    Mirrors the `web_client` fixture in tests/test_web_ui.py — we re-create
    the app so the DuckDB singleton picks up the per-test DATA_DIR rather
    than leaking state across tests on the same xdist worker.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db
    close_system_db()
    from app.main import create_app
    app = create_app()
    yield TestClient(app)
    close_system_db()


def test_setup_page_default_role_is_analyst(client):
    """No `role` query param → analyst layout (most users are analysts;
    admin layout is opt-in via the admin tile, which only renders to admins)."""
    resp = client.get("/setup", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    # Analyst tile rendered; analyst layout is what the unauthenticated /
    # non-admin caller gets by default.
    assert "Analyst workspace" in text
    assert "role=analyst" in text


def test_setup_page_analyst_role(client):
    """`?role=analyst` → analyst tile is the active one."""
    resp = client.get("/setup?role=analyst", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    assert "Analyst workspace" in text
    # The page must reflect the analyst selection somewhere — either via
    # the active-state CSS class or the `role=analyst` link being rendered.
    assert "role=analyst" in text


def test_setup_page_admin_tile_hidden_for_non_admin(client):
    """Non-admin caller (anonymous in this test) must NOT see the admin tile —
    the admin paste prompt references admin-only endpoints (marketplace
    registration, skills install) that a non-admin PAT can't authenticate
    against, so showing it would lead to a confusing failure.
    """
    resp = client.get("/setup", follow_redirects=True)
    assert resp.status_code == 200
    assert "Admin CLI" not in resp.text
    assert "role=admin" not in resp.text


def test_setup_page_admin_role_downgraded_for_non_admin(client):
    """Non-admin requesting `?role=admin` is silently downgraded to analyst.
    The page must NOT render admin instructions (no `claude plugin marketplace
    add` in the rendered prompt) for someone who can't execute them."""
    resp = client.get("/setup?role=admin", follow_redirects=True)
    assert resp.status_code == 200
    # Admin-only steps must NOT appear (would surface admin paste prompt).
    assert "claude plugin marketplace add" not in resp.text
    # Analyst-only step IS present (downgrade landed on analyst layout).
    assert "agnes init" in resp.text


def test_install_redirects_to_setup(client):
    """`/install` legacy path keeps redirecting to `/setup` (302/307)."""
    resp = client.get("/install", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "/setup" in resp.headers["location"]


def test_setup_page_invalid_role_falls_back(client):
    """Invalid role values must NOT 500 — either FastAPI's Literal
    validation rejects with 422, or the route quietly falls back to admin.
    Both are acceptable; what's not acceptable is an unhandled exception.
    """
    resp = client.get("/setup?role=hacker", follow_redirects=True)
    assert resp.status_code in (200, 422)


def test_setup_page_analyst_js_uses_bootstrap_scope(client):
    """Analyst tile's setupNewClaude JS must mint bootstrap-analyst PATs.

    The JS PAT mint must be role-aware: analyst gets a short-TTL
    bootstrap-analyst-scoped PAT (server clamps ttl ≤ 3600s), not the
    historical 90-day general PAT. Asserts the wiring at the rendered
    template level so we catch any regression in either the Jinja ctx
    plumbing or the JS branching.
    """
    resp = client.get("/setup?role=analyst", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    # The role variable must be set to analyst in JS scope.
    assert (
        'const ROLE = "analyst"' in text
        or 'ROLE = "analyst"' in text
        or 'data-role="analyst"' in text
    )
    # The bootstrap-analyst scope must appear in the JS PAT-mint body.
    assert "bootstrap-analyst" in text
    assert "ttl_seconds" in text


def test_setup_page_admin_js_uses_general_scope(client):
    """Admin tile's setupNewClaude JS must keep the existing 90-day
    expires_in_days behavior — byte-identical PAT mint shape so existing
    admin flows don't regress.
    """
    resp = client.get("/setup?role=admin", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    assert "expires_in_days" in text  # still present in the admin body


def test_setup_page_analyst_clipboard_renders_analyst_layout(client):
    """The clipboard text the analyst tile produces must be the analyst layout
    (`agnes init` + `agnes catalog`), NOT the admin layout (`claude plugin
    marketplace add` + admin-only flow).

    The rendered HTML embeds the role-aware `setup_instructions_lines` text
    into a JS array `SETUP_INSTRUCTIONS_TEMPLATE` (see
    `_claude_setup_instructions.jinja`); `renderSetupInstructions` substitutes
    `{server_url}` / `{token}` into that array at click time. So checking the
    embedded array against the served HTML is sufficient — if it carries the
    analyst layout, the clipboard payload will too.

    Pinning to the JS array block specifically (not the whole page) avoids
    false positives from chrome / preview-mode <pre> renders elsewhere.
    """
    import re

    resp = client.get("/setup?role=analyst", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text

    # Locate the JS array that holds the clipboard template body. The partial
    # emits `var SETUP_INSTRUCTIONS_TEMPLATE = [...].join("\n");` — match
    # everything between the `[` and the matching `]` so we can scope the
    # assertions to *just* what gets pasted into Claude Code, ignoring the
    # preview <pre> block that also embeds the lines.
    match = re.search(
        r"var\s+SETUP_INSTRUCTIONS_TEMPLATE\s*=\s*\[(.*?)\]\.join\(",
        text,
        re.DOTALL,
    )
    assert match, "SETUP_INSTRUCTIONS_TEMPLATE array not found in rendered HTML"
    clipboard_block = match.group(1)

    # Analyst layout markers MUST be in the clipboard block.
    assert "agnes init" in clipboard_block, (
        "Analyst clipboard payload missing `agnes init` — is "
        "compute_default_agent_prompt(role=role) being threaded into "
        "setup_instructions_lines on the /setup route?"
    )
    assert "agnes catalog" in clipboard_block, (
        "Analyst clipboard payload missing `agnes catalog` smoke verify"
    )

    # Admin-only markers MUST NOT be in the analyst clipboard block.
    # If they are, renderSetupInstructions is producing admin layout despite
    # the analyst tile being selected — the bug this test guards against.
    # `agnes auth import-token` is the admin login step (analyst uses
    # `agnes init` which bundles auth + workspace bootstrap).
    assert "agnes auth import-token" not in clipboard_block, (
        "Analyst clipboard block contains admin-only `agnes auth import-token` "
        "— role plumbing is broken; analyst sees admin instructions."
    )
    assert "agnes skills" not in clipboard_block, (
        "Analyst clipboard block contains admin-only skills setup step — "
        "analyst layout should not include skills management."
    )


def test_setup_page_admin_clipboard_renders_admin_layout(client, monkeypatch):
    """Counterpart to the analyst test — admin caller asking for `?role=admin`
    sees the full marketplace/plugins flow.

    Admin layout is now admin-gated (non-admins are silently downgraded to
    analyst). To exercise the admin path, monkeypatch `get_optional_user` to
    return an admin user dict. This avoids spinning up a full session-cookie
    fixture for one assertion.
    """
    import re
    from app.web.router import get_optional_user
    from fastapi import Request

    async def _admin_user(request: Request):  # type: ignore[no-redef]
        return {"id": "admin-1", "email": "admin@example.com",
                "is_admin": True, "name": "Admin"}

    # Override the FastAPI dependency on the running app.
    client.app.dependency_overrides[get_optional_user] = _admin_user
    try:
        resp = client.get("/setup?role=admin", follow_redirects=True)
    finally:
        client.app.dependency_overrides.pop(get_optional_user, None)

    assert resp.status_code == 200
    text = resp.text

    match = re.search(
        r"var\s+SETUP_INSTRUCTIONS_TEMPLATE\s*=\s*\[(.*?)\]\.join\(",
        text,
        re.DOTALL,
    )
    assert match, "SETUP_INSTRUCTIONS_TEMPLATE array not found in rendered HTML"
    clipboard_block = match.group(1)

    # Admin layout marker MUST be present.
    assert "agnes auth import-token" in clipboard_block, (
        "Admin clipboard payload missing `agnes auth import-token`"
    )
    assert "agnes skills" in clipboard_block, (
        "Admin clipboard payload missing the skills setup step"
    )
    # Analyst-only marker MUST NOT appear in admin layout.
    assert "agnes init" not in clipboard_block, (
        "Admin clipboard block leaked the analyst `agnes init` step"
    )


def test_setup_page_role_is_json_escaped(client):
    """The `ROLE` JS const must be injected via Jinja `tojson` (defense in
    depth) — not as a bare `"{{ role }}"` string interpolation. This makes
    JS string-escaping explicit and removes the dependency on Jinja
    autoescape semantics for JS contexts.
    """
    resp = client.get("/setup?role=analyst", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    # tojson always emits double-quoted JSON: the rendered output is exactly
    # `const ROLE = "analyst";` (note: no extra space inside the quotes).
    assert 'const ROLE = "analyst";' in text


def test_setup_page_js_ternary_keys_bootstrap_to_analyst(client):
    """Mutation-resistant assertion: the `bootstrap-analyst` scope must sit on
    the truthy branch of `ROLE === "analyst"`, not on the falsy branch.

    Without this, a silent inversion (`ROLE === "analyst"` ↔ `ROLE !== "analyst"`)
    would let analyst tile mint a general PAT and admin tile mint a
    bootstrap-scoped PAT — with both substrings still present in served HTML.
    The test passes whether the content is queried via either role URL since
    the JS ternary itself is identical across role responses.
    """
    import re
    resp = client.get("/setup?role=admin", follow_redirects=True)  # JS body is role-independent
    assert resp.status_code == 200
    text = resp.text
    # Match the ternary `ROLE === "analyst" ? <truthy_branch> : <falsy_branch>`.
    # Allow whitespace/newlines, ensure `bootstrap-analyst` is in the truthy branch.
    pattern = re.compile(
        r'ROLE\s*===\s*"analyst"\s*\?\s*\{[^}]*bootstrap-analyst[^}]*\}\s*:\s*\{[^}]*expires_in_days[^}]*\}',
        re.DOTALL,
    )
    assert pattern.search(text), (
        "JS PAT-mint ternary must have `bootstrap-analyst` on the analyst (truthy) "
        "branch and `expires_in_days` on the admin (falsy) branch. A silent inversion "
        "would let the analyst tile mint a general PAT — exactly the regression we want to catch."
    )
