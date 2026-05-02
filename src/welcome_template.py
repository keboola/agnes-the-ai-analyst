"""Render the agent-setup-prompt banner shown on /setup.

The banner is a small HTML snippet admin-editable at /admin/agent-prompt.
It appears above the bash bootstrap commands on the /setup page and is
intended for org-specific operational notes (VPN warning, support channel,
data classification reminder, platform requirements).

Default: no banner (empty string). Admins override via the welcome_template
DB table (singleton, content TEXT).

Security: output is HTML-sanitized after render (script/iframe/event-handler
strip). The Jinja2 environment uses StrictUndefined with autoescape=True so
template typos raise immediately rather than silently emitting empty HTML.
"""
# See also: surfaced as the "Agent Setup Prompt" admin editor at /admin/agent-prompt.

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
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
# Banner renderer
# ---------------------------------------------------------------------------

def render_agent_prompt_banner(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: dict[str, Any] | None,
    server_url: str,
) -> str:
    """Render the admin-configured HTML banner for the /setup page.

    Returns an empty string when no override is set (default = no banner).
    Render failures are swallowed (logged) and return empty string so a
    broken template never blocks the /setup page from rendering.
    """
    row = WelcomeTemplateRepository(conn).get()
    content = row.get("content")
    if not content:
        return ""

    try:
        env = Environment(undefined=StrictUndefined, autoescape=True)
        template = env.from_string(content)
        ctx = build_context(user=user, server_url=server_url)
        rendered = template.render(**ctx)
        return _sanitize_banner_html(rendered)
    except TemplateError as exc:
        logger.warning(
            "Agent-prompt banner render failed (template error): %s", exc
        )
        return ""
    except Exception:
        logger.exception("Agent-prompt banner render failed (unexpected)")
        return ""
