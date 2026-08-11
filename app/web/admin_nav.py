"""Canonical inventory for the grouped `/admin` sidebar (`_admin_nav.html`).

Single source of truth for the sidebar's four sections — People & access,
Data, Content, System — mirroring the IA already proven in the topnav Admin
mega-menu (`_app_header.html`) and the rail's Admin flyout (`_app_rail.html`),
just collapsed from their seven finer-grained groups into the four the sidebar
mock (issue #896 follow-up) asked for. Kept as a plain Python module (not
inline in the Jinja partial) so `tests/test_web_admin_nav.py` can import it
directly and assert every `require_admin`-gated, template-rendering GET route
in `app/web/router.py` is reachable from some entry here — the guard fails
loudly the moment a new admin page ships without a nav entry.

Each item:
    label — sidebar row text.
    href  — the canonical URL the row links to.
    match — path prefixes that mark the row (and its detail sub-pages)
            active; a route "hits" an item when the current request path
            equals one of these or starts with one of them + "/".

Deliberately out of scope (not required-admin gated, or not registered in
``app/web/router.py``):
  - `/admin/studio` and `/admin/studio/{domain}` — the Studio authoring
    surface is available to every signed-in user (`get_current_user`, not
    `require_admin`); it happens to share the `/admin` URL prefix but is a
    different product surface, not an admin-only page.
  - `/admin/chat` — content-negotiated JSON/HTML route registered from
    `app/api/admin_chat.py`, not `app/web/router.py`.
  - Pure redirects (`/admin/usage`, `/admin/access`, `/admin/grants`,
    `/admin/scheduler-runs`, `/admin/agent-prompt`, `/admin/workspace-prompt`)
    — they 308 onto a page that already carries a nav entry.
"""

from __future__ import annotations

ADMIN_NAV_SECTIONS: list[dict] = [
    {
        "label": "People & access",
        "items": [
            {"label": "Users", "href": "/admin/users", "match": ["/admin/users"]},
            {
                "label": "Groups",
                "href": "/admin/groups",
                "match": ["/admin/groups", "/admin/access", "/admin/grants"],
            },
            {"label": "Tokens", "href": "/admin/tokens", "match": ["/admin/tokens"]},
        ],
    },
    {
        "label": "Data",
        "items": [
            {"label": "Tables", "href": "/admin/tables", "match": ["/admin/tables"]},
            {"label": "Sync", "href": "/admin/sync", "match": ["/admin/sync"]},
            {"label": "Data packages", "href": "/admin/data-packages", "match": ["/admin/data-packages"]},
            {"label": "Data sources", "href": "/admin/data-sources", "match": ["/admin/data-sources"]},
            {"label": "Semantic layer", "href": "/admin/semantic-layer", "match": ["/admin/semantic-layer"]},
            {
                "label": "MCP sources",
                "href": "/admin/mcp-sources",
                "match": ["/admin/mcp-sources", "/admin/mcp-tools"],
            },
            {
                "label": "Instance secrets",
                "href": "/admin/datasource-credentials",
                "match": ["/admin/datasource-credentials"],
            },
            {"label": "Linked apps", "href": "/admin/linked-apps", "match": ["/admin/linked-apps"]},
        ],
    },
    {
        "label": "Content",
        "items": [
            {"label": "Store moderation", "href": "/admin/store", "match": ["/admin/store"]},
            {
                "label": "Flea submissions",
                "href": "/admin/store/submissions",
                "match": ["/admin/store/submissions"],
            },
            {"label": "Store lint", "href": "/admin/store/lint", "match": ["/admin/store/lint"]},
            {
                "label": "Studio suggestions",
                "href": "/admin/studio/suggestions",
                "match": ["/admin/studio/suggestions"],
            },
            {"label": "Marketplaces", "href": "/admin/marketplaces", "match": ["/admin/marketplaces"]},
            {"label": "Knowledge digests", "href": "/admin/knowledge-digests", "match": ["/admin/knowledge-digests"]},
            {"label": "Corporate memory", "href": "/admin/corporate-memory", "match": ["/admin/corporate-memory"]},
            {"label": "News", "href": "/admin/news", "match": ["/admin/news"]},
            {"label": "Contribute a skill", "href": "/admin/contribute-skill", "match": ["/admin/contribute-skill"]},
        ],
    },
    {
        "label": "System",
        "items": [
            {"label": "Server config", "href": "/admin/server-config", "match": ["/admin/server-config"]},
            {"label": "Database backend", "href": "/admin/database", "match": ["/admin/database"]},
            {"label": "Audit log", "href": "/admin/activity", "match": ["/admin/activity"]},
            {"label": "Telemetry", "href": "/admin/telemetry", "match": ["/admin/telemetry", "/admin/usage"]},
            {"label": "Analyst sessions", "href": "/admin/sessions", "match": ["/admin/sessions"]},
            {"label": "Adoption", "href": "/admin/adoption", "match": ["/admin/adoption"]},
            {
                "label": "Initial workspace",
                "href": "/admin/initial-workspace",
                "match": ["/admin/initial-workspace"],
            },
            {
                "label": "Prompts",
                "href": "/admin/prompts",
                "match": ["/admin/prompts", "/admin/agent-prompt", "/admin/workspace-prompt"],
            },
        ],
    },
]


def _prefix_hit(path: str, prefix: str) -> bool:
    """Whether *path* is on *prefix* — exact match, or a sub-page
    (``prefix + "/..."``)."""
    return path == prefix or path.startswith(prefix + "/")


def resolve_active_href(path: str) -> str | None:
    """The href of the ONE nav item that should render active for *path*.

    Several items' ``match`` prefixes can textually overlap (``/admin/store``
    the moderation hub vs. ``/admin/store/submissions`` its own row) — the
    LONGEST matching prefix wins, so a sub-page never leaves its own parent
    lit at the same time. Returns ``None`` when nothing matches (e.g. the
    ``/admin`` hub page itself, which has no section entry — see the "Admin"
    title link in ``_admin_nav.html``).
    """
    best_href: str | None = None
    best_len = -1
    for section in ADMIN_NAV_SECTIONS:
        for item in section["items"]:
            for prefix in item["match"]:
                if _prefix_hit(path, prefix) and len(prefix) > best_len:
                    best_len = len(prefix)
                    best_href = item["href"]
    return best_href
