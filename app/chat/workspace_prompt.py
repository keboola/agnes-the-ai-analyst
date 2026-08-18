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
from typing import Optional

logger = logging.getLogger(__name__)


def render_sandbox_workspace_prompt(user_email: str, *, server_url: Optional[str] = None) -> Optional[str]:
    """Render the analyst CLAUDE.md (admin Workspace Prompt override or shipped
    default), RBAC-filtered for this user — the same content ``agnes init``
    writes on a laptop via ``GET /api/welcome``.

    Returns ``None`` on any failure so a caller falls back to the bundled
    static CLAUDE.md rather than failing to prepare a workspace at all.

    ``server_url`` defaults to ``agnes_server_url()``, the same fallback chain
    the sandbox env uses (``SERVER_URL`` → ``AGNES_INTERNAL_URL`` → loopback).
    """
    try:
        from app.chat.manager import agnes_server_url
        from src.claude_md import render_claude_md
        from src.db import get_system_db
        from src.repositories import use_pg, users_repo

        # User read via the factory so it honors use_pg() — a direct
        # UserRepository(conn) read the frozen DuckDB system file on Postgres
        # instances (#518). The conn below is the DuckDB-mode path handed to
        # render_claude_md, which routes its own state reads through the
        # factory; on Postgres it is None so the system DuckDB is never opened
        # (forbidden invariant).
        u = users_repo().get_by_email(user_email)
        if not u:
            return None
        effective_url = agnes_server_url() if server_url is None else server_url
        conn = None if use_pg() else get_system_db()
        try:
            # This is a chat sandbox — its filesystem is ephemeral and not the
            # analyst's own machine, so the rendered prompt must use the
            # sandbox wording (e.g. the Charts section's inline-SVG-only rule).
            return render_claude_md(conn, user=u, server_url=effective_url, is_sandbox=True)
        finally:
            if conn is not None:
                conn.close()
    except Exception:
        logger.exception("render workspace prompt failed for %s", user_email)
        return None
