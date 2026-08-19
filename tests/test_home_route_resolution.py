"""``get_home_route`` and the ``/`` redirect chain.

Resolution order is env > yaml > default ``/dashboard``. The env path is
the Terraform-overrideable knob — operators set ``AGNES_HOME_ROUTE`` on
the VM without forking instance.yaml. Bad values fall through to the
default rather than producing an external-host redirect.
"""

from __future__ import annotations

import re
import tempfile
import uuid

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        # Ensure the env-var override is unset between tests.
        monkeypatch.delenv("AGNES_HOME_ROUTE", raising=False)
        yield tmp


def _make_user_and_session(conn, email="u@example.com"):
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0])
    return uid, create_access_token(user_id=uid, email=email)


def _client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, follow_redirects=False)


def test_default_home_route_is_dashboard(fresh_db, monkeypatch):
    monkeypatch.delenv("AGNES_HOME_ROUTE", raising=False)
    from app.instance_config import get_home_route

    assert get_home_route() == "/dashboard"


def test_env_overrides_default(fresh_db, monkeypatch):
    monkeypatch.setenv("AGNES_HOME_ROUTE", "/home")
    from app.instance_config import get_home_route

    assert get_home_route() == "/home"


def test_env_rejects_external_redirect(fresh_db, monkeypatch):
    """An attacker controlling the env var (or a typo) must not pivot
    the root redirect to ``//evil.com`` or ``https://evil.com``."""
    monkeypatch.setenv("AGNES_HOME_ROUTE", "//evil.com/path")
    from app.instance_config import get_home_route

    assert get_home_route() == "/dashboard"

    monkeypatch.setenv("AGNES_HOME_ROUTE", "https://evil.com")
    assert get_home_route() == "/dashboard"


def test_retired_ask_route_coerced_to_dashboard(fresh_db, monkeypatch):
    """`/ask` is retired (#896) and 302s to `/`. An instance that had pinned
    `home_route: /ask` would loop `/` -> `/ask` -> `/`; get_home_route must
    coerce it to the default so such configs keep working (on rail the
    dashboard itself forwards to chat / My Stack)."""
    monkeypatch.setenv("AGNES_HOME_ROUTE", "/ask")
    from app.instance_config import get_home_route

    assert get_home_route() == "/dashboard"


def test_root_redirect_authed_user_uses_home_route(fresh_db, monkeypatch):
    """``GET /`` for an authenticated user redirects to the configured
    home route, not the hard-coded ``/dashboard``."""
    monkeypatch.setenv("AGNES_HOME_ROUTE", "/home")

    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/", cookies={"access_token": sess})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/home"


def test_root_redirect_unauthed_goes_to_login(fresh_db):
    c = _client()
    resp = c.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_instance_admin_email_default_empty(fresh_db, monkeypatch):
    """Unset env + unset YAML → empty string. Template branches on
    truthiness so empty hides the GWS Email-admin button cleanly."""
    monkeypatch.delenv("AGNES_INSTANCE_ADMIN_EMAIL", raising=False)
    from app.instance_config import get_instance_admin_email

    assert get_instance_admin_email() == ""


def test_instance_admin_email_env_overrides(fresh_db, monkeypatch):
    """env var takes precedence over YAML / default."""
    monkeypatch.setenv("AGNES_INSTANCE_ADMIN_EMAIL", "ops@example.com")
    from app.instance_config import get_instance_admin_email

    assert get_instance_admin_email() == "ops@example.com"


def test_instance_admin_email_strips_whitespace(fresh_db, monkeypatch):
    """Operator quoting habits ("` ops@example.com `") shouldn't break the
    mailto link — strip surrounding whitespace at the resolver."""
    monkeypatch.setenv("AGNES_INSTANCE_ADMIN_EMAIL", "  ops@example.com  ")
    from app.instance_config import get_instance_admin_email

    assert get_instance_admin_email() == "ops@example.com"


def test_instance_admin_email_empty_env_treated_as_unset(fresh_db, monkeypatch):
    """Empty-string env var is intentional opt-out, not garbage."""
    monkeypatch.setenv("AGNES_INSTANCE_ADMIN_EMAIL", "")
    from app.instance_config import get_instance_admin_email

    assert get_instance_admin_email() == ""


def test_gws_oauth_default_unset(fresh_db, monkeypatch):
    monkeypatch.delenv("AGNES_GWS_CLIENT_ID", raising=False)
    monkeypatch.delenv("AGNES_GWS_CLIENT_SECRET", raising=False)
    from app.instance_config import get_gws_oauth_credentials

    creds = get_gws_oauth_credentials()
    assert creds["configured"] is False
    assert creds["client_id"] == ""
    assert creds["client_secret"] == ""
    # OAUTHLIB_INSECURE_TRANSPORT defaults to "1" (gws CLI uses HTTP loopback)
    assert creds["oauthlib_insecure_transport"] == "1"


def test_gws_oauth_env_overrides(fresh_db, monkeypatch):
    monkeypatch.setenv("AGNES_GWS_CLIENT_ID", "abc.apps.googleusercontent.com")
    monkeypatch.setenv("AGNES_GWS_CLIENT_SECRET", "GOCSPX-secret")
    from app.instance_config import get_gws_oauth_credentials

    creds = get_gws_oauth_credentials()
    assert creds["configured"] is True
    assert creds["client_id"] == "abc.apps.googleusercontent.com"
    assert creds["client_secret"] == "GOCSPX-secret"


def test_gws_oauth_project_id_derived_from_client_id(fresh_db, monkeypatch):
    """Numeric project_id is the prefix of the client_id before the first '-'.
    Required by the gws CLI's client_secret.json schema (non-Option in Rust)."""
    monkeypatch.setenv("AGNES_GWS_CLIENT_ID", "123456789012-abcd5678efgh.apps.googleusercontent.com")
    monkeypatch.setenv("AGNES_GWS_CLIENT_SECRET", "GOCSPX-x")
    monkeypatch.delenv("AGNES_GWS_PROJECT_ID", raising=False)
    from app.instance_config import get_gws_oauth_credentials

    assert get_gws_oauth_credentials()["project_id"] == "123456789012"


def test_gws_oauth_project_id_explicit_override(fresh_db, monkeypatch):
    """Explicit AGNES_GWS_PROJECT_ID wins over the derived value — covers
    edge cases where the client_id doesn't contain a numeric prefix."""
    monkeypatch.setenv("AGNES_GWS_CLIENT_ID", "abc-x.apps.googleusercontent.com")
    monkeypatch.setenv("AGNES_GWS_CLIENT_SECRET", "GOCSPX-x")
    monkeypatch.setenv("AGNES_GWS_PROJECT_ID", "explicit-id")
    from app.instance_config import get_gws_oauth_credentials

    assert get_gws_oauth_credentials()["project_id"] == "explicit-id"


def test_gws_oauth_half_configured_falls_back(fresh_db, monkeypatch):
    """Only client_id, no secret → not configured. Half-configuration must
    not engage the shortcut branch."""
    monkeypatch.setenv("AGNES_GWS_CLIENT_ID", "abc.apps.googleusercontent.com")
    monkeypatch.delenv("AGNES_GWS_CLIENT_SECRET", raising=False)
    from app.instance_config import get_gws_oauth_credentials

    assert get_gws_oauth_credentials()["configured"] is False


def test_gws_body_describes_both_branches(fresh_db, monkeypatch):
    """The GWS SKILL.md body always describes BOTH branches
    (operator-OAuth-app and manual GCP walkthrough) — the skill checks
    `~/.claude/agnes/.env` at install time to pick the right one. The
    /home install prompt no longer inlines connector bodies (they are
    fetched via `agnes connectors show` / GET /api/connectors/{slug}/prompt),
    so the landmarks are pinned on the body loader the endpoint uses.

    Behaviour unchanged from A1.2 either way: literal client_id /
    client_secret values never render into HTML; they flow through
    `agnes init` into `<workspace>/.claude/agnes/.env`.
    """
    monkeypatch.setenv("AGNES_GWS_CLIENT_ID", "123456789012-abcd5678efgh.apps.googleusercontent.com")
    monkeypatch.setenv("AGNES_GWS_CLIENT_SECRET", "GOCSPX-secret-xyz")

    from src.connectors_manifest import load_connector_body

    loaded = load_connector_body("connector-gws")
    assert loaded is not None
    body, _source = loaded
    # Operator-OAuth-app branch landmark — the inlined client_secret.json
    # schema block references the per-tenant .env file.
    assert "~/.config/gws/client_secret.json" in body
    assert "AGNES_GWS_CLIENT_ID" in body
    # Full read+write scopes — no --readonly flag.
    assert "gws auth login --readonly" not in body
    # Manual branch present too.
    assert "offer the operator path" in body
    assert "run `gws auth setup` for me" in body
    # No leaked client_id placeholder.
    assert "GOOGLE_WORKSPACE_CLI_CLIENT_ID=" not in body
    # And the /home page itself must NOT inline the body (that was 76 %
    # of the install prompt) nor leak any secret value.
    import html as _html

    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    page = _html.unescape(resp.text)
    assert "GOCSPX-secret-xyz" not in page
    assert "~/.config/gws/client_secret.json" not in page
    assert "agnes connectors show connector-gws" in page


def test_home_automode_default_show(fresh_db, monkeypatch):
    monkeypatch.delenv("AGNES_HOME_SHOW_AUTOMODE", raising=False)
    from app.instance_config import get_home_automode_visibility

    assert get_home_automode_visibility() is True


def test_home_automode_env_can_hide(fresh_db, monkeypatch):
    monkeypatch.setenv("AGNES_HOME_SHOW_AUTOMODE", "0")
    from app.instance_config import get_home_automode_visibility

    assert get_home_automode_visibility() is False


def test_home_renders_automode_block_by_default(fresh_db, monkeypatch):
    """Step 4 is the credential-bearing step for the not-onboarded /home
    view: it saves the login token to ~/.agnes/token AND (in this
    home_automode=on branch) launches Claude in the same line, with the
    right flag for Step 5's ~20 shell commands. Label leads with the
    "hand it your login first" framing (Patch: PAT delivered out-of-band,
    not embedded in the install script). The launch command still
    recommends `claude --permission-mode auto` plus `--allowedTools`
    rules that pre-approve the Agnes CLI's own commands (explicit
    allow-rules resolve before the auto-mode classifier runs, so
    `agnes init` / `agnes refresh-marketplace` never pause the setup
    script); auto-accept-edits via Shift + Tab kept as the strict
    fallback for users who want to review each command. The YOLO flag
    (`--dangerously-skip-permissions`) is no longer surfaced on /home."""
    monkeypatch.delenv("AGNES_HOME_SHOW_AUTOMODE", raising=False)

    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    body = c.get("/home", cookies={"access_token": sess}).text
    assert "Launch Claude — we'll hand it your login first" in body
    # The masked command's data-cmd-template carries the real shape
    # (with a literal {TOKEN} marker, not a secret) — recommended path:
    # auto mode + the Agnes CLI allow-rules (both spellings — see
    # _AGNES_PERMISSION_ALLOW_RULES in cli/lib/hooks.py) plus the wheel
    # installer (`uv tool install`) so Step 1's CLI install isn't blocked
    # by a permission prompt.
    assert (
        'claude --permission-mode auto --allowedTools "Bash(agnes:*)" "Bash(agnes *)" "Bash(uv tool install:*)"' in body
    )
    # The YOLO flag is no longer recommended on /home.
    assert "--dangerously-skip-permissions" not in body
    # Strict fallback: Shift + Tab → auto-accept-edits.
    assert "Shift + Tab" in body
    # The launch command is masked — never the real token — and the
    # real-value substitution happens only in memory, client-side.
    assert "install-cmd-masked" in body
    assert "data-cmd-template=" in body
    assert "{TOKEN}" in body
    assert "eyJ" not in body
    # Step 1 is the bare official installer per OS — the self-healing
    # one-liner (already-installed check + in-session PATH fix) is gone.
    # Verify + one-time OAuth sign-in + /exit return + the
    # command-not-found remedy live as inline text, NOT a second copy-box
    # (a second dark box read as another thing to run).
    assert "irm https://claude.ai/install.ps1 | iex" in body
    assert "curl -fsSL https://claude.ai/install.sh | bash" in body
    assert "install-cmd-claude-verify" not in body
    assert "<code>claude --version</code>" in body
    assert "one-time OAuth in your browser" in body
    assert "<code>/exit</code>" in body
    assert "command not found" in body
    # Step 4 is guard-free: token write + launch only. No `command -v`
    # gate, no "CLI not found" else-branch.
    assert "command -v claude" not in body
    assert "Claude Code CLI not found" not in body


def test_home_hides_automode_launch_tail_when_env_off(fresh_db, monkeypatch):
    """With automode off, Step 4 still saves the token (its own step) but
    does NOT launch Claude in the same command — that branch's command
    has no `claude --permission-mode auto` tail."""
    monkeypatch.setenv("AGNES_HOME_SHOW_AUTOMODE", "0")

    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    body = c.get("/home", cookies={"access_token": sess}).text
    assert "Step 4 — Launch Claude with auto-approve on" not in body
    assert "Launch Claude — we'll hand it your login first" not in body
    # The token-only Step 4 still renders, without the launch tail.
    assert "Save your login token" in body
    assert "data-cmd-template=" in body
    assert "{TOKEN}" in body
    assert "claude --permission-mode auto" not in body
    assert "eyJ" not in body


def test_navbar_home_link_uses_home_route(fresh_db, monkeypatch):
    """The shared navbar's primary "Home" link respects
    ``AGNES_HOME_ROUTE`` so a single env flip routes it to /home or
    /dashboard. Tested by rendering an authed page and grepping the
    rendered HTML — keeps the assertion close to what users see."""
    monkeypatch.setenv("AGNES_HOME_ROUTE", "/home")

    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    # /home page itself renders the shared chrome.
    resp = c.get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    # The chrome's home link href reflects the RESOLVED home_route, not a
    # hard-coded /dashboard. That link is the rail logo: the topnav's labelled
    # "Home"/"Dashboard" nav item went with the topnav chrome (Wave 0,
    # 2026-08), so the label half of this assertion has no surface left — but
    # the half that matters (the resolver reaching the chrome) does.
    assert re.search(r'class="rail-logo" href="/home"', resp.text)


def test_navbar_home_link_follows_a_reconfigured_route(fresh_db, monkeypatch):
    """The other side of the same knob: point it elsewhere, the chrome follows.

    Was `test_navbar_dashboard_link_label`, which asked /dashboard for a 200
    and a "Dashboard" nav label. Both premises are gone — /dashboard is now an
    unconditional 302 into chat's pre-conversation state, and no chrome renders
    a labelled home item. Re-pinned onto a route that still renders, so the
    test still fails if home_route stops reaching the chrome.
    """
    monkeypatch.setenv("AGNES_HOME_ROUTE", "/library")

    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/library", cookies={"access_token": sess})
    assert resp.status_code == 200
    assert re.search(r'class="rail-logo" href="/library"', resp.text)


def test_dashboard_is_a_redirect_not_a_page(fresh_db, monkeypatch):
    """/dashboard stopped being a rendered surface in Wave 0 (2026-08).

    Kept as an explicit assertion because the old test above asserted a 200
    here; without this, deleting that premise would leave the redirect
    itself unguarded.
    """
    monkeypatch.setenv("AGNES_HOME_ROUTE", "/dashboard")

    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/dashboard", cookies={"access_token": sess}, follow_redirects=False)
    assert resp.status_code == 302
    # No chat grant in this fixture -> the Library landing.
    assert resp.headers["location"] == "/library"


# ---------------------------------------------------------------------------
# Atlassian base URL — operator-provisioned site root, Terraform-overrideable
# via AGNES_ATLASSIAN_BASE_URL.
# ---------------------------------------------------------------------------


def test_atlassian_base_url_default_empty(fresh_db, monkeypatch):
    """Unset env + unset YAML → empty string. Connector prompt falls
    back to asking the user for the site URL (the existing flow)."""
    monkeypatch.delenv("AGNES_ATLASSIAN_BASE_URL", raising=False)
    from app.instance_config import get_atlassian_base_url

    assert get_atlassian_base_url() == ""


def test_atlassian_base_url_env_overrides(fresh_db, monkeypatch):
    """Env var takes precedence over YAML / default."""
    monkeypatch.setenv("AGNES_ATLASSIAN_BASE_URL", "https://acme.atlassian.net")
    from app.instance_config import get_atlassian_base_url

    assert get_atlassian_base_url() == "https://acme.atlassian.net"


def test_atlassian_base_url_strips_trailing_slash(fresh_db, monkeypatch):
    """`https://acme.atlassian.net/` → `https://acme.atlassian.net`.
    Matches the per-user helper script's normalization at storage time
    (atlassian_prompt step 4 guard 2). Without this, $BASE_URL/rest/...
    becomes $BASE_URL//rest/... which some CDN paths reject."""
    monkeypatch.setenv("AGNES_ATLASSIAN_BASE_URL", "https://acme.atlassian.net/")
    from app.instance_config import get_atlassian_base_url

    assert get_atlassian_base_url() == "https://acme.atlassian.net"


def test_atlassian_base_url_strips_trailing_wiki(fresh_db, monkeypatch):
    """`https://acme.atlassian.net/wiki` (the Confluence path) →
    `https://acme.atlassian.net` (bare site root). The connector
    prompt's verify step probes both Jira (root) and Confluence
    (root + /wiki), so the canonical stored value is the root."""
    monkeypatch.setenv("AGNES_ATLASSIAN_BASE_URL", "https://acme.atlassian.net/wiki")
    from app.instance_config import get_atlassian_base_url

    assert get_atlassian_base_url() == "https://acme.atlassian.net"

    monkeypatch.setenv("AGNES_ATLASSIAN_BASE_URL", "https://acme.atlassian.net/wiki/")
    assert get_atlassian_base_url() == "https://acme.atlassian.net"


def test_atlassian_skill_asks_for_url_in_v1():
    """The Atlassian SKILL.md asks the user for their Atlassian Cloud
    site URL unconditionally (the operator-baked-URL feature moves to
    runtime via ~/.claude/agnes/.env). Pinned on the body loader that
    GET /api/connectors/{slug}/prompt serves — bodies are no longer
    inlined into the rendered install prompt."""
    from src.connectors_manifest import load_connector_body

    loaded = load_connector_body("connector-atlassian")
    assert loaded is not None
    body, _source = loaded
    assert "Ask me for my Atlassian Cloud site URL" in body
