"""Render the RBAC-filtered workspace prompt a sandbox gets as ``CLAUDE.md``.

One renderer, two runtimes. Agnes's native chat sandbox seeds it through
``WorkdirManager`` (``app/main.py`` injects this as ``render_workspace_prompt``);
the embedded ``kai-agent`` turn engine receives it inside the workspace tarball
(``app/api/kai.py``). An admin who edits the Workspace Prompt in ``/admin``
expects both sandboxes to pick it up, so the rendering lives here rather than
being written twice — the second copy is the one that silently keeps shipping
the shipped default after someone changes the first.

The analyst-facing sibling is ``app/api/claude_md.py`` (``GET /api/welcome``),
which renders the same document with ``is_sandbox=False`` for a real laptop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


def render_sandbox_workspace_prompt(
    user_email: str,
    *,
    server_url: Optional[str] = None,
    conn: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Render the analyst CLAUDE.md (admin Workspace Prompt override or shipped
    default), RBAC-filtered for this user — the same content ``agnes init``
    writes on a laptop via ``GET /api/welcome``.

    Returns ``None`` on any failure so a caller falls back to the bundled
    static CLAUDE.md rather than failing to prepare a workspace at all.

    ``server_url`` defaults to ``agnes_server_url()``, the same fallback chain
    the sandbox env uses (``SERVER_URL`` → ``AGNES_INTERNAL_URL`` → loopback).

    ``conn`` is optional and stays optional on purpose: this module opens no
    connection of its own. ``resolve_prompt`` binds a supplied DuckDB
    connection so a caller's in-flight read sees its own transaction, and
    resolves through the repository factory when given nothing — which is the
    right default for a fresh read, and the reason this file needs no entry on
    the system-DuckDB grandfather list in
    ``tests/test_backend_split_guard.py``. A caller that already holds a
    request-scoped connection may pass it; on Postgres it must not.

    ``now`` pins the rendered clock. Leave it unset for a document written once
    at workspace init. Pin it when the SAME bytes must come out for the same
    inputs: the shipped template ends with "generated {{ today }}", so an
    unpinned render changes at every UTC date rollover.
    """
    try:
        from app.chat.manager import agnes_server_url
        from src.claude_md import render_claude_md
        from src.repositories import users_repo

        # User read via the factory so it honors use_pg() — a direct
        # UserRepository(conn) read the frozen DuckDB system file on Postgres
        # instances (#518).
        u = users_repo().get_by_email(user_email)
        if not u:
            return None
        effective_url = agnes_server_url() if server_url is None else server_url
        # This is a chat sandbox — its filesystem is ephemeral and not the
        # analyst's own machine, so the rendered prompt must use the sandbox
        # wording (e.g. the Charts section's inline-SVG-only rule).
        return render_claude_md(conn, user=u, server_url=effective_url, is_sandbox=True, now=now)
    except Exception:
        logger.exception("render workspace prompt failed for %s", user_email)
        return None
