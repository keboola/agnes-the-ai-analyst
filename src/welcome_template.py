"""Render the agent-setup-prompt for the /setup page.

The prompt is admin-editable at /admin/agent-prompt.  When no override is
set, the default content is the live output of
``app.web.setup_instructions.resolve_lines()`` — the full bash bootstrap
script (TLS trust, CLI install, login, marketplace, skills).  When an
override is saved it replaces the default everywhere: both the /setup page
display and the dashboard clipboard CTA.

Override content is a Jinja2 template (autoescape=False, StrictUndefined).
Available placeholders: instance.{name,subtitle}, server.{url,hostname},
user (may be None for anonymous visitors), now, today.

The bash default is **not** HTML-sanitized (it is bash, not HTML).  Override
content IS HTML-sanitized after render: script/iframe/event-handler strip.

See also: surfaced as the "Agent Setup Prompt" admin editor at /admin/agent-prompt.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import duckdb
from jinja2 import Environment, StrictUndefined, TemplateError

from app.instance_config import (
    get_instance_name,
    get_instance_subtitle,
)
from src.repositories.welcome_template import WelcomeTemplateRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML sanitization
# ---------------------------------------------------------------------------

_RE_SCRIPT = re.compile(
    r"<script[\s\S]*?</script>", re.IGNORECASE
)
_RE_IFRAME = re.compile(
    r"<iframe[\s\S]*?(?:</iframe>|/>)", re.IGNORECASE
)
_RE_ON_EVENT = re.compile(
    r"\s+on\w+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE
)
_RE_JS_URI = re.compile(
    r"""(?:href|src|action)\s*=\s*(?:"|')(javascript:|data:)""", re.IGNORECASE
)


def _sanitize_banner_html(html: str) -> str:
    """Strip dangerous constructs from admin-authored HTML.

    Defense-in-depth only — admins are trusted, but this prevents accidental
    XSS from copy-pasted snippets reaching the public /setup page.

    Strips:
    - <script>…</script> blocks (any content)
    - <iframe>…</iframe> tags
    - on*= event handler attributes (onclick=, onload=, etc.)
    - javascript: / data: URI schemes in href/src/action attributes
    """
    html = _RE_SCRIPT.sub("", html)
    html = _RE_IFRAME.sub("", html)
    html = _RE_ON_EVENT.sub("", html)
    html = _RE_JS_URI.sub(
        lambda m: m.group(0).replace(m.group(1), "#"), html
    )
    return html


# ---------------------------------------------------------------------------
# Render context
# ---------------------------------------------------------------------------

def build_context(
    *,
    user: dict[str, Any] | None,
    server_url: str,
) -> dict[str, Any]:
    """Compose the Jinja2 render context for the banner.

    Intentionally small: instance identity, server URL, and the requesting
    user (may be None for anonymous /setup visitors). No tables, metrics, or
    marketplaces — the banner is for org-operational notes, not data-catalog
    content.

    Note: ``now`` is tz-aware UTC.
    """
    now = datetime.now(timezone.utc)
    parsed = urlparse(server_url)
    user_ctx: dict[str, Any] | None = None
    if user:
        user_ctx = {
            "id": user.get("id", ""),
            "email": user.get("email", ""),
            "name": user.get("name") or "",
            "is_admin": bool(user.get("is_admin")),
            "groups": user.get("groups") or [],
        }
    return {
        "instance": {
            "name": get_instance_name(),
            "subtitle": get_instance_subtitle(),
        },
        "server": {
            "url": server_url,
            "hostname": parsed.hostname or "",
        },
        "user": user_ctx,
        "now": now,
        "today": date.today().isoformat(),
    }


# ---------------------------------------------------------------------------
# Default content — live setup script
# ---------------------------------------------------------------------------

def compute_default_agent_prompt(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: dict[str, Any] | None,
    server_url: str,
) -> str:
    """Return the live default setup script from setup_instructions.resolve_lines().

    This is the unified bash bootstrap prompt that /setup shows when no
    admin override is set. The returned string is bash (not HTML) —
    callers must NOT pass it through _sanitize_banner_html.

    ``conn`` and ``user`` are forwarded to resolve the RBAC-filtered plugin
    install list. The same RBAC pass runs for everyone (admin and
    non-admin alike): users with no plugin grants get the no-marketplace
    layout (Confirm = step 6); users with grants get the marketplace + plugins
    block inserted (Confirm = step 8). Anonymous visitors / no conn fall
    through to the no-marketplace layout.

    ``server_url`` is used to derive the server host for the marketplace
    block.
    """
    try:
        from app.web.setup_instructions import resolve_lines
        from app.api.cli_artifacts import _find_wheel

        _wheel = _find_wheel()
        _wheel_filename = _wheel.name if _wheel else "agnes.whl"

        # The install commands emitted in the marketplace block must match
        # exactly what /marketplace.zip + /marketplace.git/ serve. That's
        # the `resolve_user_marketplace` view: admin grants minus the
        # user's opt-outs, plus their Store installs (skills + agents
        # rolled up into the synth `agnes-store-bundle` plugin, plugin-
        # typed entities standalone). `resolve_allowed_plugins` was the
        # pre-store admin-only feed and would emit installs for plugins
        # the user has opted out of, while skipping the bundle entirely.
        #
        # Dedup by manifest_name handles the documented case where two
        # upstream marketplaces ship a plugin with the same name (see
        # CLAUDE.md "Same-named plugins ... collide in the catalog by
        # design"). The synth marketplace.json carries one entry per
        # name; a second `claude plugin install <name>@agnes` would be
        # a no-op anyway.
        plugin_install_names: list[str] = []
        if user and conn is not None:
            try:
                from src import marketplace_filter
                seen: set[str] = set()
                for p in marketplace_filter.resolve_user_marketplace(conn, user):
                    name = p["manifest_name"]
                    if name in seen:
                        continue
                    seen.add(name)
                    plugin_install_names.append(name)
            except Exception:
                logger.exception("compute_default_agent_prompt: marketplace plugin resolution failed")

        from urllib.parse import urlparse as _urlparse
        parsed = _urlparse(server_url)
        server_host = parsed.netloc or parsed.hostname or ""

        ca_pem: str | None = None
        try:
            from app.web.router import _read_agnes_ca_pem
            ca_pem = _read_agnes_ca_pem()
        except Exception:
            pass

        # Resolve connector prompts via the shared module so the bash
        # script's step-9 connector block uses the same operator-side
        # config (GWS OAuth credentials, admin email) as the /home tile
        # cards. Failure here falls back to the module's default empty
        # config — the unconfigured GCP-walkthrough branch renders, which
        # is the same behaviour as today on an instance with no
        # AGNES_GWS_CLIENT_ID / AGNES_GWS_CLIENT_SECRET set.
        connector_prompts: dict[str, str] | None = None
        try:
            from app.web.connector_prompts import all_connector_prompts
            from app.instance_config import (
                get_gws_oauth_credentials, get_instance_admin_email,
            )
            from app.instance_config import get_atlassian_base_url, get_instance_brand
            connector_prompts = all_connector_prompts(
                gws_oauth=get_gws_oauth_credentials(),
                instance_admin_email=get_instance_admin_email(),
                atlassian_base_url=get_atlassian_base_url(),
                instance_brand=get_instance_brand(),
            )
        except Exception:
            logger.exception("compute_default_agent_prompt: connector prompt resolution failed; using module defaults")

        from app.instance_config import get_instance_brand, get_workspace_dir_name
        lines = resolve_lines(
            _wheel_filename,
            plugin_install_names=plugin_install_names,
            server_host=server_host,
            ca_pem=ca_pem,
            connector_prompts=connector_prompts,
            instance_brand=get_instance_brand(),
            workspace_dir=get_workspace_dir_name(),
        )
        return "\n".join(lines)
    except Exception:
        logger.exception("compute_default_agent_prompt: unexpected error; returning empty")
        return ""


# ---------------------------------------------------------------------------
# Prompt renderer (override or default)
# ---------------------------------------------------------------------------

def render_agent_prompt_banner(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: dict[str, Any] | None,
    server_url: str,
) -> str:
    """Render the agent setup prompt for the /setup page.

    When an admin override is set:
      - Renders via Jinja2 (autoescape=True, StrictUndefined).
      - HTML-sanitizes the output.
      - Returns the sanitized HTML string.

    When no override is set:
      - Returns the live default from compute_default_agent_prompt() — the
        full bash bootstrap script.  This is bash, not HTML, so no
        sanitization is applied.

    Render failures on the override path are swallowed (logged) and fall back
    to the live default so a broken template never blocks /setup.
    """
    row = WelcomeTemplateRepository(conn).get()
    content = row.get("content")

    if content:
        # Admin-authored override — render as Jinja2, sanitize.
        # autoescape=False to match /setup rendering — the outer Jinja2 template
        # applies escaping where needed.
        try:
            env = Environment(undefined=StrictUndefined, autoescape=False)
            template = env.from_string(content)
            ctx = build_context(user=user, server_url=server_url)
            rendered = template.render(**ctx)
            return _sanitize_banner_html(rendered)
        except TemplateError as exc:
            logger.warning(
                "Agent-prompt banner render failed (template error): %s", exc
            )
            # Fall through to default
        except Exception:
            logger.exception("Agent-prompt banner render failed (unexpected)")
            # Fall through to default

    # No override (or broken override) — return live default bash script.
    # Same unified flow for everyone; admin-vs-analyst is no longer a
    # layout branch. The marketplace block is gated by the caller's
    # plugin grants in `resource_grants`, which `compute_default_agent_prompt`
    # resolves unconditionally.
    return compute_default_agent_prompt(
        conn, user=user, server_url=server_url,
    )
