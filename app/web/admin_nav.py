"""Canonical inventory for the grouped `/admin` sidebar (`_admin_nav.html`).

Single source of truth for the sidebar's sections. The IA is the admin's
JOBS, in the order the work happens — the shape decided by the admin
redesign (docs/superpowers/specs/2026-08-12-admin-redesign-exploration.md):

    Overview                     where am I, what needs me, what's next
    People                       accounts · groups · tokens
    Data                         sources · tables · packages · sync · semantics
    ── maintain ──
    Library                      what analysts can find: curation + moderation
    Instance                     the machine: config, secrets, connections
    Activity                     what's happening: audit, telemetry, adoption

Two intent sections up front (getting people in, getting data to them — with
access edited from both of those: a group's Access tab and a package's Share
editor), three maintenance sections behind a divider. This replaced a
seven-section shape (People & access / Data / Connections / Moderation /
Content / Instance / Insights) that grouped routes closer to "what the table
underneath is" than to what an admin is trying to do — Moderation and Content
were both really "the Library, from two directions", and Connections was
plumbing that belongs to the Instance. A dedicated Access surface (the
group-x-package matrix with simulate) is designed but not built; when it
ships it becomes the third intent section. Kept as a plain Python module
(not inline in the Jinja partial) so `tests/test_web_admin_nav.py` can import
it directly and assert every `require_admin`-gated, template-rendering GET
route in `app/web/router.py` is reachable from some entry here — the guard
fails loudly the moment a new admin page ships without a nav entry.

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

This column is now the ONLY navigation for the admin area. `/admin` used to
render a second copy of this inventory as a card grid; that grid is gone (the
hub is a real dashboard — see `admin_signals.py`), so anything reachable only
from the old grid had to move here or become unreachable. Two entries below
exist for that reason and carry constraints worth knowing before editing:

  - `/admin/studio` — the Studio authoring surface is available to every
    signed-in user (`get_current_user`, not `require_admin`); it shares the
    `/admin` URL prefix without being an admin-only page. It carries
    ``"when": "can_studio"``, the ONLY conditional item mechanism here: the
    partial drops the row when that context flag is falsey, matching the gate
    the old hub grid applied. `can_studio` is `get_studio_enabled()`, set on
    every context by `_build_context`.
  - `/admin/chat` — genuinely `require_admin`, but registered from
    `app/api/admin_chat.py` (a router with `prefix="/admin/chat"`), not
    `app/web/router.py`. `tests/test_web_admin_nav.py` reads BOTH modules for
    exactly this row; a nav entry pointing at a route defined in a third
    module would fail its reverse guard until that module is added there too.

Deliberately out of scope:
  - Pure redirects (`/admin/usage`, `/admin/access`, `/admin/grants`,
    `/admin/scheduler-runs`, `/admin/agent-prompt`, `/admin/workspace-prompt`)
    — they 308 onto a page that already carries a nav entry.
  - The API-documentation links, which are not `/admin/*` routes at all — see
    `ADMIN_NAV_DOCS` at the bottom of this module.
"""

from __future__ import annotations

# The hub, as the sidebar's FIRST ROW — above the sections, in none of
# them. `/admin` is the landing surface every admin area is reachable from
# (its card grid is the long form of this whole sidebar), so it is a
# destination in its own right, not a heading.
#
# It used to be reachable only by clicking the column's "Admin" TITLE, which
# is not a control anyone reads as a link, and the rail compensated by hanging
# a hover flyout of admin areas off its own Admin row — a second, hand-written
# copy of this inventory that had already drifted (different labels, different
# grouping, and three `/documentation` links that are not admin pages at all:
# that route is gated by `get_current_user`, not `require_admin`). Both are
# retired: the rail's Admin is a plain link here, and here is where the areas
# are listed. See `_app_rail.html`.
#
# NOT a member of ADMIN_NAV_SECTIONS on purpose — a section's `key` drives the
# stored open/closed preference, and this row has no children to disclose. It
# also keeps `resolve_active_href` returning None for the hub, so landing on
# `/admin` still expands no section.
#
# No `icon`: rows in this column are label-only (the icons belong to the
# primary rail, one tier up — see admin-nav.css's header).
ADMIN_NAV_HOME: dict = {"label": "Overview", "href": "/admin"}

# `divider_before` — the section opens the MAINTAIN half of the column. The
# partial renders a small labelled rule above it; the flag lives here rather
# than in the template so the split is part of the pinned IA, not styling.
ADMIN_NAV_SECTIONS: list[dict] = [
    {
        "key": "people",
        "label": "People",
        "icon": "users",
        "items": [
            {"label": "Users", "href": "/admin/users", "match": ["/admin/users"]},
            # A group's page carries its Access tab — the grant editor — which
            # is why /admin/access and /admin/grants fold in here.
            {"label": "Groups", "href": "/admin/groups", "match": ["/admin/groups", "/admin/access", "/admin/grants"]},
            {"label": "Tokens", "href": "/admin/tokens", "match": ["/admin/tokens"]},
        ],
    },
    {
        "key": "data",
        "label": "Data",
        "icon": "data",
        "items": [
            # Source → tables → packages: the order the work happens. Sync and
            # the semantic layer are per-source states, kept as rows until the
            # source-panel surface (spec §3.7) absorbs them as tabs.
            {"label": "Data sources", "href": "/admin/data-sources", "match": ["/admin/data-sources"]},
            {"label": "Tables", "href": "/admin/tables", "match": ["/admin/tables"]},
            {"label": "Data packages", "href": "/admin/data-packages", "match": ["/admin/data-packages"]},
            {"label": "Sync", "href": "/admin/sync", "match": ["/admin/sync"]},
            {"label": "Semantic layer", "href": "/admin/semantic-layer", "match": ["/admin/semantic-layer"]},
        ],
    },
    {
        "key": "library",
        "label": "Library",
        "icon": "package",
        "divider_before": True,
        "items": [
            # What analysts can find — curation and moderation are the same
            # job from two directions, so the old Moderation + Content
            # sections merge here.
            {"label": "Marketplaces", "href": "/admin/marketplaces", "match": ["/admin/marketplaces"]},
            {"label": "Store moderation", "href": "/admin/store", "match": ["/admin/store"]},
            {"label": "Flea submissions", "href": "/admin/store/submissions", "match": ["/admin/store/submissions"]},
            {"label": "Store lint", "href": "/admin/store/lint", "match": ["/admin/store/lint"]},
            {
                "label": "Studio suggestions",
                "href": "/admin/studio/suggestions",
                "match": ["/admin/studio/suggestions"],
            },
            {"label": "Corporate memory", "href": "/admin/corporate-memory", "match": ["/admin/corporate-memory"]},
            {"label": "Knowledge digests", "href": "/admin/knowledge-digests", "match": ["/admin/knowledge-digests"]},
            {"label": "News", "href": "/admin/news", "match": ["/admin/news"]},
            {"label": "Contribute a skill", "href": "/admin/contribute-skill", "match": ["/admin/contribute-skill"]},
            # Conditional — see the module docstring. `match` stays the bare
            # prefix: `/admin/studio/suggestions` is its own row above and
            # wins on longest-prefix, so the two never light together.
            {
                "label": "Studio",
                "href": "/admin/studio",
                "match": ["/admin/studio"],
                "when": "can_studio",
            },
        ],
    },
    {
        "key": "instance",
        "label": "Instance",
        "icon": "tools",
        "items": [
            # The machine itself — configuration, secrets, and outbound
            # connections (the old Connections section was Instance plumbing
            # wearing its own heading).
            {"label": "Server config", "href": "/admin/server-config", "match": ["/admin/server-config"]},
            {"label": "Database backend", "href": "/admin/database", "match": ["/admin/database"]},
            {"label": "Initial workspace", "href": "/admin/initial-workspace", "match": ["/admin/initial-workspace"]},
            {
                "label": "Prompts",
                "href": "/admin/prompts",
                "match": ["/admin/prompts", "/admin/agent-prompt", "/admin/workspace-prompt"],
            },
            {
                "label": "Instance secrets",
                "href": "/admin/datasource-credentials",
                "match": ["/admin/datasource-credentials"],
            },
            {"label": "MCP sources", "href": "/admin/mcp-sources", "match": ["/admin/mcp-sources", "/admin/mcp-tools"]},
            {"label": "Linked apps", "href": "/admin/linked-apps", "match": ["/admin/linked-apps"]},
        ],
    },
    {
        "key": "activity",
        "label": "Activity",
        "icon": "rows",
        "items": [
            {"label": "Audit log", "href": "/admin/activity", "match": ["/admin/activity"]},
            {"label": "Telemetry", "href": "/admin/telemetry", "match": ["/admin/telemetry", "/admin/usage"]},
            {"label": "Analyst sessions", "href": "/admin/sessions", "match": ["/admin/sessions"]},
            {"label": "Chat sessions", "href": "/admin/chat", "match": ["/admin/chat"]},
            {"label": "Adoption", "href": "/admin/adoption", "match": ["/admin/adoption"]},
        ],
    },
]

# API documentation — a footer strip in `_admin_nav.html`, NOT a
# section. These are not `/admin/*` routes (`/documentation/api` is
# `get_current_user`-gated; `/docs` and `/redoc` are FastAPI's own), so they
# cannot be section items without widening
# `tests/test_web_admin_nav.py::test_every_nav_href_is_a_real_admin_route`
# past the job it exists to do — and another section would break the
# deliberate IA that `test_exactly_the_decided_sections_in_order` pins. They landed here when `/admin`'s card grid was replaced by the
# dashboard; the grid was the only place they were linked from.
#
# No `match` key: these never render active. The admin column is not visible
# on any of the three destinations.
ADMIN_NAV_DOCS: list[dict] = [
    {"label": "API guide", "href": "/documentation/api"},
    {"label": "Interactive API", "href": "/docs"},
    {"label": "API reference", "href": "/redoc"},
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


def resolve_home_active(path: str) -> bool:
    """Whether the sidebar's first row (``ADMIN_NAV_HOME``) is the active one.

    EXACT match on ``/admin`` — not a prefix. Every ``/admin/*`` path belongs
    to one of the sections, and a prefix rule here would light the hub
    row on all of them, giving the column two active rows at once.
    """
    return path.rstrip("/") == "/admin"
