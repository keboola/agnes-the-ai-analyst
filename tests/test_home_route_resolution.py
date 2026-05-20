"""``get_home_route`` and the ``/`` redirect chain.

Resolution order is env > yaml > default ``/dashboard``. The env path is
the Terraform-overrideable knob — operators set ``AGNES_HOME_ROUTE`` on
the VM without forking instance.yaml. Bad values fall through to the
default rather than producing an external-host redirect.
"""

from __future__ import annotations

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
    monkeypatch.setenv(
        "AGNES_GWS_CLIENT_ID", "123456789012-abcd5678efgh.apps.googleusercontent.com"
    )
    monkeypatch.setenv("AGNES_GWS_CLIENT_SECRET", "GOCSPX-x")
    monkeypatch.delenv("AGNES_GWS_PROJECT_ID", raising=False)
    from app.instance_config import get_gws_oauth_credentials
    assert get_gws_oauth_credentials()["project_id"] == "123456789012"


def test_gws_oauth_project_id_explicit_override(fresh_db, monkeypatch):
    """Explicit AGNES_GWS_PROJECT_ID wins over the derived value — covers
    edge cases where the client_id doesn't contain a numeric prefix."""
    monkeypatch.setenv(
        "AGNES_GWS_CLIENT_ID", "abc-x.apps.googleusercontent.com"
    )
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


def test_home_renders_configured_gws_branch(fresh_db, monkeypatch):
    """Configured branch writes ~/.config/gws/client_secret.json directly
    instead of exporting env vars. Claude Code's security layer redacts
    env vars whose name contains 'SECRET', so the file-write path is the
    only reliable way to seed the OAuth app credentials.

    The gws prompt body now flows through Jinja's autoescape (the template
    moved from inline `<code>` text to a `{{ connector_prompts.gws }}`
    expression after the connector-prompts extraction). That means `"`
    characters render as `&quot;` in the served HTML — the browser
    un-escapes them on read, but the raw response body has the entity-
    encoded form. So the test un-escapes before substring-matching."""
    import html as _html

    monkeypatch.setenv(
        "AGNES_GWS_CLIENT_ID", "123456789012-abcd5678efgh.apps.googleusercontent.com"
    )
    monkeypatch.setenv("AGNES_GWS_CLIENT_SECRET", "GOCSPX-secret-xyz")

    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    body = _html.unescape(resp.text)
    # Configured branch — JSON file path
    assert "~/.config/gws/client_secret.json" in body
    assert '"client_id": "123456789012-abcd5678efgh.apps.googleusercontent.com"' in body
    assert '"client_secret": "GOCSPX-secret-xyz"' in body
    # Project ID derived from client_id prefix
    assert '"project_id": "123456789012"' in body
    # Full read+write scopes — no --readonly flag (Agnes needs Drive/Gmail
    # write so the agent can create, edit, and send on the user's behalf).
    assert "gws auth login --readonly" not in body
    assert "OAUTHLIB_INSECURE_TRANSPORT=1 gws auth login" in body
    # Manual-setup walkthrough should NOT appear in the configured branch
    assert "Run `gws auth setup` for me" not in body
    # Old env-var approach should not leak back in
    assert "export GOOGLE_WORKSPACE_CLI_CLIENT_SECRET=" not in body


def test_home_renders_manual_gws_branch_when_unset(fresh_db, monkeypatch):
    monkeypatch.delenv("AGNES_GWS_CLIENT_ID", raising=False)
    monkeypatch.delenv("AGNES_GWS_CLIENT_SECRET", raising=False)

    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    body = resp.text
    # Manual setup walkthrough renders
    assert "Run `gws auth setup` for me" in body
    # No leaked client_id placeholder
    assert "GOOGLE_WORKSPACE_CLI_CLIENT_ID=" not in body


def test_home_automode_default_show(fresh_db, monkeypatch):
    monkeypatch.delenv("AGNES_HOME_SHOW_AUTOMODE", raising=False)
    from app.instance_config import get_home_automode_visibility
    assert get_home_automode_visibility() is True


def test_home_automode_env_can_hide(fresh_db, monkeypatch):
    monkeypatch.setenv("AGNES_HOME_SHOW_AUTOMODE", "0")
    from app.instance_config import get_home_automode_visibility
    assert get_home_automode_visibility() is False


def test_home_renders_automode_block_by_default(fresh_db, monkeypatch):
    """The permission-mode step renders by default for the not-onboarded
    /home view. The block is Step 3 (folder creation moved up to Step 2
    so the user mkdir+cd's first, then this step launches Claude in that
    directory with the right flag for Step 4's ~20 shell commands).
    Label primarily recommends `claude --dangerously-skip-permissions`
    via the standard `.install-cmd` + copy-button affordance; auto-
    accept-edits via Shift + Tab kept as the strict fallback for users
    who want to review each command."""
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
    # The `Step N —` prefix was dropped from labels (the step-number
    # badge already carries the number); match the bare label text.
    assert "Launch Claude with auto-approve on" in body
    # Recommended path: `claude --dangerously-skip-permissions`.
    assert "claude --dangerously-skip-permissions" in body
    # Strict fallback: Shift + Tab → auto-accept-edits.
    assert "Shift + Tab" in body


def test_home_hides_automode_block_when_env_off(fresh_db, monkeypatch):
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
    # /home page itself renders the shared header.
    resp = c.get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    # Navbar link href reflects the resolved home_route, not hard-coded /dashboard.
    # Label is "Home" (was "Dashboard" before the nav reorg).
    assert 'href="/home">Home' in resp.text


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


def test_atlassian_prompt_uses_base_url_when_set():
    """The atlassian connector prompt bakes the operator's base URL into
    the helper script instead of asking the user. Saves a chat round-
    trip and avoids the "guess your org's Atlassian URL" footgun."""
    from app.web.connector_prompts import atlassian_prompt

    p = atlassian_prompt(base_url="https://acme.atlassian.net")
    # The literal URL is baked into the prompt body.
    assert "https://acme.atlassian.net" in p
    # The "ask me for the site URL" step disappears.
    assert "Ask me for my Atlassian Cloud site URL" not in p
    # The placeholder in step 4's helper-script body is replaced with the literal.
    assert "<the site URL I gave you>" not in p
    # The new "operator baked it in" wording appears in step 1.
    assert "already provisioned by the Agnes operator" in p


def test_atlassian_prompt_asks_user_when_base_url_empty():
    """When no operator override is set, prompt falls back to the
    existing "ask me for the site URL" flow — no regression for OSS
    instances that don't set the env var."""
    from app.web.connector_prompts import atlassian_prompt

    p = atlassian_prompt(base_url="")
    assert "Ask me for my Atlassian Cloud site URL" in p
    assert "<the site URL I gave you>" in p
    assert "already provisioned by the Agnes operator" not in p


def test_all_connector_prompts_threads_atlassian_base_url():
    """all_connector_prompts() must forward the atlassian_base_url
    kwarg to atlassian_prompt — otherwise the operator's Terraform
    override never reaches the rendered text."""
    from app.web.connector_prompts import all_connector_prompts

    out = all_connector_prompts(atlassian_base_url="https://acme.atlassian.net")
    assert "https://acme.atlassian.net" in out["atlassian"]
    assert "Ask me for my Atlassian Cloud site URL" not in out["atlassian"]
