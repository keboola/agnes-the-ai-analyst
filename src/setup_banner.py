"""Render the admin-editable setup-page banner.

Smaller surface than welcome_template — only instance/server/user context.
Setup banner is for organization-specific operational notes (VPN, support,
data classification), not for analyst-side content.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import duckdb
from jinja2 import Environment, StrictUndefined, TemplateError

from app.instance_config import get_instance_name, get_instance_subtitle
from src.repositories.setup_banner import SetupBannerRepository

_logger = logging.getLogger(__name__)

# Patterns used by _sanitize_banner_html.
_RE_SCRIPT = re.compile(r"<\s*script[\s\S]*?(?:</\s*script\s*>|$)", re.IGNORECASE)
_RE_IFRAME = re.compile(r"<\s*iframe[\s\S]*?(?:</\s*iframe\s*>|$)", re.IGNORECASE)
_RE_ON_ATTR = re.compile(r'\s+on\w+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]*)', re.IGNORECASE)
_RE_JS_URI = re.compile(
    r'((?:href|src)\s*=\s*["\'])(?:javascript|data):[^"\']*(["\'])',
    re.IGNORECASE,
)


def _sanitize_banner_html(html: str) -> str:
    """Strip the most dangerous markup patterns from rendered banner HTML.

    Threat model: admins are trusted to author banner content, but mistakes
    happen (copy-paste from untrusted sources, accidental script inclusion).
    This is defense-in-depth, NOT a full XSS defense — for that, render
    markdown only or add a strict Content-Security-Policy. The whitelist of
    bad patterns is intentionally narrow so legitimate admin HTML is not
    mangled.

    What is stripped:
    - ``<script>...</script>`` blocks (case-insensitive, including unclosed).
    - ``<iframe>...</iframe>`` blocks.
    - ``on*=`` event-handler attributes (e.g. onclick, onload, onerror).
    - ``javascript:`` and ``data:`` URI schemes in href/src attributes.
    """
    html = _RE_SCRIPT.sub("", html)
    html = _RE_IFRAME.sub("", html)
    html = _RE_ON_ATTR.sub("", html)
    html = _RE_JS_URI.sub(lambda m: m.group(1) + "#" + m.group(2), html)
    return html


def build_setup_banner_context(
    *,
    user: Optional[dict],
    server_url: str,
) -> dict[str, Any]:
    """Compose the Jinja2 render context for the setup banner.

    ``user`` may be None on the anonymous path of /setup (the page is partly
    public — anonymous visitors get the curl-install one-liner). Templates
    must guard for that with ``{% if user %}``.
    """
    parsed = urlparse(server_url)
    return {
        "instance": {
            "name": get_instance_name(),
            "subtitle": get_instance_subtitle(),
        },
        "server": {
            "url": server_url,
            "hostname": parsed.hostname or "",
        },
        "user": (
            {
                "id": user.get("id", ""),
                "email": user.get("email", ""),
                "name": user.get("name") or "",
                "is_admin": bool(user.get("is_admin")),
            }
            if user
            else None
        ),
        "now": datetime.now(timezone.utc),
        "today": date.today().isoformat(),
    }


def render_setup_banner(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: Optional[dict],
    server_url: str,
) -> str:
    """Render the banner. Returns "" when no override is set or render fails.

    Render failures are swallowed (logged) — a broken admin banner must NOT
    break /setup for analysts. The /admin/setup-banner editor catches Jinja
    errors at PUT time anyway, so this is defense-in-depth.
    """
    row = SetupBannerRepository(conn).get()
    source = row.get("content")
    if not source:
        return ""
    env = Environment(undefined=StrictUndefined, autoescape=True)
    try:
        template = env.from_string(source)
        rendered = template.render(**build_setup_banner_context(user=user, server_url=server_url))
        return _sanitize_banner_html(rendered)
    except TemplateError:
        _logger.warning(
            "setup_banner render failed; returning empty banner. "
            "Admin can fix at /admin/setup-banner."
        )
        return ""
