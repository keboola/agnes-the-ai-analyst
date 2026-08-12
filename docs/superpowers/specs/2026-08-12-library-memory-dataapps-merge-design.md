# Library absorbs the Memory browse; Data apps become a Library band (rail-only)

**Date:** 2026-08-12
**Status:** approved
**Follow-up to:** PR #1276 (rail-chrome reachability fixes)

## Problem

Under the rail chrome the Library is the one browse destination, yet two
resource kinds still live on standalone pages that the rail either links as
extra nav rows (post-#1276) or historically did not link at all:

- `/corporate-memory` overlaps the Library's Memory band almost entirely.
  Both render the same `StackResolver.browse()` set; the Library rows even
  carry the add-to-stack affordance (`/api/stack/subscribe`). What the
  standalone page still has over the band: per-domain item/required counts,
  hiding of empty domains, a My Stack tab, and (classic mode) an admin
  god-view of all domains.
- `/apps` (hosted data apps) is not represented in the Library at all, even
  though the Files band already carries a "Data apps coming soon" badge
  (`_SECTION_SOON` in `app/web/router.py`) — the plan of record was always
  for data apps to land in the Library.

Two overlapping surfaces per kind cost nav space (two rail rows added by
#1276 as an interim fix) and split the "what do I have / what can I add"
answer across pages.

## Decision

Merge both kinds into the Library **under the rail chrome only**, and retire
the standalone pages there via redirects. Default (topnav) instances keep
the pre-redesign pages byte-for-byte.

## Constraints (non-negotiable)

- **Default-chrome parity.** Topnav instances must render exactly what they
  render today. The `/library` route forks to `library_legacy.html` before
  the sections pipeline runs (`get_ui_layout() != "rail"` early-return), so
  new bands are rail-only by construction. Redirects are keyed on
  `get_ui_layout() == "rail"`. Guard: `tests/test_ui_layout_theme.py::
  TestDefaultContentParity` must pass unchanged.
- **Feature gate.** Everything data-apps respects `data_apps_enabled()`;
  with the feature off there is no band, no redirect target section, and
  `/apps` keeps its current explanatory empty-state behavior.
- **PR #1276 lands as-is.** This work is a follow-up branch that removes the
  interim rail rows (Memory, Apps) it added.

## Design

### 1. Data apps band

A new Library section "Data apps" (`type_key="data_app"`), sibling of
Plugins/Memory, built from exactly the `/apps` page's logic — reuse
`app.api.data_apps._can_view` / `_serialize` via the same imports the
`data_apps_list_page` handler uses (`data_apps_repo().list(
include_drafts=False)`, exclude `state == "linked_hidden"`):

- Row: title = app name, description, `href=/apps/detail/{slug}`,
  `type_label="Data app"`, meta text = app state (running/stopped/linked),
  owner label = owner email; origin "yours" for the owner, "Shared with you"
  otherwise.
- No stack membership: rows are not addable/removable (`stack_state` stays
  empty), matching the fact that data-app access is grant-driven, not
  stack-driven.
- Band renders only when `data_apps_enabled()` — same Jinja-global gate the
  chrome uses.
- The Files band's "Data apps coming soon" badge (`_SECTION_SOON["files"]`)
  is removed — the promise is kept.
- `_SECTION_LABELS` gains `"data_app": "Data apps"`, plus a one-line
  `_SECTION_HINTS` entry.

### 2. Memory band absorbs the standalone browse

The Library's memory-domain rows gain what only `/corporate-memory` cards
had:

- **Counts in meta:** "N items · M required" (from
  `memory_domains_repo().list_items_of_domain`), computed once per domain
  the way the standalone route does.
- **Empty domains hidden** unless `requirement == "required"` — the same
  `_has_content` rule the standalone page applies (admins manage empty
  placeholders at `/admin/corporate-memory#domains`).

Deliberately **not** carried over:

- Classic-mode admin god-view (`browse_admin`, all domains regardless of
  grants). That is an audit surface; it lives at
  `/admin/corporate-memory#domains`, which the rail admin flyout links.
- The admin pending-review banner — same home.
- The standalone page's domain-level search box; the Library's own search
  covers rows, and item-level search is out of scope (below).

### 3. Redirects under rail

- `GET /corporate-memory` → `302 /library?section=memory_domain`
- `GET /apps` → `302 /library?section=data_app` — only when
  `data_apps_enabled()`; with the feature off the page keeps its current
  explanatory empty state (a redirect would land a bookmark on a Library
  with no Data apps section and no explanation).

Both only when `get_ui_layout() == "rail"`; topnav serves today's pages.
302 (not 308) so a later layout flip is not cached permanently — the same
reasoning as the `/dashboard` → `/chat` rail redirect. Detail pages
(`/memory/d/{slug}`, `/apps/detail/{slug}`) stay live in both chromes.

### 4. Rail nav and back links

- The interim rail rows **Memory** and **Apps** (added by #1276) come out of
  `_app_rail.html`; the Library is the way in.
- `_RAIL_DETAIL_BACK["memory_domain"]` returns to
  `("/library?section=memory_domain", "All memory")` — reverting the
  interim #1276 change that pointed it at the standalone page, since that
  page now redirects. `?source=library` arrivals keep their existing
  override.
- No `_RAIL_DETAIL_BACK` entry for data apps: `data_app_detail.html`
  carries no back link today (verified), so there is nothing to reroute.

### 5. Guards and tests

- `tests/test_web_nav_user_parity.py`: new documented set
  `REDIRECTED_UNDER_RAIL = {"/corporate-memory", "/apps"}` excluded from the
  static reachability diff, **plus** a behavioral test asserting each entry
  really 302s to its Library section under rail and still 200s under topnav
  — the set must never become a silent allowlist.
- Rendered tests (rail): Data apps band renders rows when the feature is on
  and a visible app exists; no band when off; memory rows carry counts;
  an empty non-required domain is absent; a required empty domain is
  present.
- `tests/test_web_memory_domain_detail.py` back-link test returns to
  asserting `/library?section=memory_domain`.
- `TestDefaultContentParity` unchanged and green.

## Out of scope

- Item-level (cross-domain) knowledge search in the Library — the rail
  currently has no global search; restoring one is its own decision.
- `/stack` retirement (#1088), Studio reachability, any topnav change.
- Data-app lifecycle actions (start/stop/logs) in Library rows — the detail
  page owns those.
