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
import os
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

    This is the full bash bootstrap prompt that /setup shows when no admin
    override is set.  The returned string is bash (not HTML) — callers must
    NOT pass it through _sanitize_banner_html.

    ``conn`` and ``user`` are forwarded to resolve the RBAC-filtered plugin
    install list (anonymous visitors / no conn get the no-marketplace layout).
    ``server_url`` is used to derive the server host for the marketplace block.
    """
    try:
        from app.web.setup_instructions import resolve_lines
        from app.api.cli_artifacts import _find_wheel

        _wheel = _find_wheel()
        _wheel_filename = _wheel.name if _wheel else "agnes.whl"

        plugin_install_names: list[str] = []
        if user and conn is not None:
            try:
                from src import marketplace_filter
                plugin_install_names = [
                    p["manifest_name"]
                    for p in marketplace_filter.resolve_allowed_plugins(conn, user)
                ]
            except Exception:
                logger.exception("compute_default_agent_prompt: marketplace plugin resolution failed")

        self_signed_tls = os.environ.get("AGNES_DEBUG_AUTH", "").strip().lower() in (
            "1", "true", "yes",
        )
        from urllib.parse import urlparse as _urlparse
        parsed = _urlparse(server_url)
        server_host = parsed.netloc or parsed.hostname or ""

        ca_pem: str | None = None
        try:
            from app.web.router import _read_agnes_ca_pem
            ca_pem = _read_agnes_ca_pem()
        except Exception:
            pass

        lines = resolve_lines(
            _wheel_filename,
            plugin_install_names=plugin_install_names,
            self_signed_tls=self_signed_tls,
            server_host=server_host,
            ca_pem=ca_pem,
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
    return compute_default_agent_prompt(conn, user=user, server_url=server_url)
