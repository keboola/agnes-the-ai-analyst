"""Canonical inventory for the grouped `/admin` sidebar (`_admin_nav.html`).

Single source of truth for the sidebar's seven sections — People & access,
Data, Connections, Moderation, Content, Instance, Insights — a follow-up on
the original four-section shape (issue #896 mock) that grouped routes closer
to "what the table underneath is" than to what an admin is trying to do; the
seven below are grab-bag-free (each section is one job: manage who can get
in, manage the data plumbing, manage outbound connections, moderate
submitted content, curate what analysts see, configure the instance itself,
watch what's happening). Kept as a plain Python module (not inline in the
Jinja partial) so `tests/test_web_admin_nav.py` can import it directly and
assert every `require_admin`-gated, template-rendering GET route in
`app/web/router.py` is reachable from some entry here — the guard fails
loudly the moment a new admin page ships without a nav entry.

Each section:
    key   — stable slug used as the collapse-state key (localStorage) and as
            the DOM id suffix for the section's item list / collapsed-mode
            flyout. Never reuse ANOTHER section's key and never rename an
            existing one casually — it would silently reset every browser's
            stored open/closed preference for that section.
    label — sidebar section heading.
    icon  — name passed to `macros/_icon.html`'s `icon()` macro for the
            collapsed-sidebar icon strip (one icon stands in for the whole
            section when the sidebar is collapsed to its narrow rail).
    items — the section's rows; see below.

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
        "key": "people",
        "label": "People & access",
        "icon": "users",
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
        "key": "data",
        "label": "Data",
        "icon": "data",
        "items": [
            {"label": "Data sources", "href": "/admin/data-sources", "match": ["/admin/data-sources"]},
            {"label": "Tables", "href": "/admin/tables", "match": ["/admin/tables"]},
            {"label": "Sync", "href": "/admin/sync", "match": ["/admin/sync"]},
            {"label": "Data packages", "href": "/admin/data-packages", "match": ["/admin/data-packages"]},
            {"label": "Semantic layer", "href": "/admin/semantic-layer", "match": ["/admin/semantic-layer"]},
        ],
    },
    {
        "key": "connections",
        "label": "Connections",
        "icon": "link",
        "items": [
            {
                "label": "MCP sources",
                "href": "/admin/mcp-sources",
                "match": ["/admin/mcp-sources", "/admin/mcp-tools"],
            },
            {"label": "Linked apps", "href": "/admin/linked-apps", "match": ["/admin/linked-apps"]},
            {
                "label": "Instance secrets",
                "href": "/admin/datasource-credentials",
                "match": ["/admin/datasource-credentials"],
            },
        ],
    },
    {
        "key": "moderation",
        "label": "Moderation",
        "icon": "shield-check",
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
        ],
    },
    {
        "key": "content",
        "label": "Content",
        "icon": "package",
        "items": [
            {"label": "Marketplaces", "href": "/admin/marketplaces", "match": ["/admin/marketplaces"]},
            {"label": "Knowledge digests", "href": "/admin/knowledge-digests", "match": ["/admin/knowledge-digests"]},
            {"label": "Corporate memory", "href": "/admin/corporate-memory", "match": ["/admin/corporate-memory"]},
            {"label": "News", "href": "/admin/news", "match": ["/admin/news"]},
            {"label": "Contribute a skill", "href": "/admin/contribute-skill", "match": ["/admin/contribute-skill"]},
        ],
    },
    {
        "key": "instance",
        "label": "Instance",
        "icon": "tools",
        "items": [
            {"label": "Server config", "href": "/admin/server-config", "match": ["/admin/server-config"]},
            {"label": "Database backend", "href": "/admin/database", "match": ["/admin/database"]},
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
    {
        "key": "insights",
        "label": "Insights",
        "icon": "rows",
        "items": [
            {"label": "Audit log", "href": "/admin/activity", "match": ["/admin/activity"]},
            {"label": "Telemetry", "href": "/admin/telemetry", "match": ["/admin/telemetry", "/admin/usage"]},
            {"label": "Analyst sessions", "href": "/admin/sessions", "match": ["/admin/sessions"]},
            {"label": "Adoption", "href": "/admin/adoption", "match": ["/admin/adoption"]},
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


def resolve_active_section_key(path: str) -> str | None:
    """The ``key`` of the ONE section that should render expanded BY DEFAULT
    for *path* — the section containing whichever item ``resolve_active_href``
    picked. This is the value `_admin_nav.html` renders server-side (the
    section's body has no ``hidden`` attribute, its header button carries
    ``aria-expanded="true"``) so a first paint — before any client JS runs —
    never shows a fully-expanded 28-row list, nor collapses the very section
    the caller is standing in. Returns ``None`` for the ``/admin`` hub itself,
    where every section renders collapsed.
    """
    href = resolve_active_href(path)
    if href is None:
        return None
    for section in ADMIN_NAV_SECTIONS:
        for item in section["items"]:
            if item["href"] == href:
                return section["key"]
    return None
