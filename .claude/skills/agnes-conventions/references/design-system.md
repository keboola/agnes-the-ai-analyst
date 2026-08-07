# Reference: Agnes design system (tokens, themes, layouts)

The binding visual standard for ANY agent touching web UI. The look is
token-driven and theme-switched — you never hardcode a palette, and you
never change the default chrome for existing instances.

## Architecture in one paragraph

Every page extends `base_ds.html` (or `base_page.html` on top of it).
The base stamps two attributes on `<html>`: `data-theme` (palette —
`blue` default | `navy` | `dark` | `auto` | `paper`) and
`data-ui-layout` (chrome — `topnav` default | `rail`). All colors,
type, radii, shadows, and motion come from `--ds-*` custom properties
declared in `app/web/static/css/design-tokens.css`; each theme is a
`:root[data-theme="…"]` override block there. Structural chrome is an
include switch in the bases: `_app_header.html` (topnav) vs
`_app_rail.html` (rail). Operators pick via `instance.theme` /
`instance.ui_layout` (env: `AGNES_INSTANCE_THEME` / `AGNES_UI_LAYOUT`).

## The paper theme (issue #896 prototype)

`paper` + `rail` together reproduce the prototype look: warm paper
canvas (`--ds-bg`), white panels, ONE emerald accent (`--ds-primary`),
Inter-first type with tight negative headline tracking, pill CTAs,
hairline slate borders, calm shadows, left-rail navigation.
Shape/typography rules that aren't expressible as color tokens live in
`app/web/static/css/paper-skin.css` — every selector there is scoped
to `[data-theme="paper"]`; the rail chrome CSS is
`app/web/static/css/rail.css`, scoped to `html[data-ui-layout="rail"]`.

## Non-negotiable rules for agents

1. **Tokens only.** No raw hex in templates (contract-tested), and in
   CSS reach for an existing `--ds-*` token before inventing a value.
   Legacy `var(--primary)` is banned in new code — use
   `var(--ds-primary)`.
2. **Never restyle the default.** `blue` + `topnav` is what existing
   instances render; visual changes ship as opt-in theme/skin blocks
   (`[data-theme="paper"] …`, `html[data-ui-layout="rail"] …`).
   `tests/test_ui_layout_theme.py` guards this — the default page must
   keep `.app-header` and `data-theme="blue"`.
3. **Scoped skin sheets.** Anything paper-specific goes in
   `paper-skin.css` under a `[data-theme="paper"]` selector; anything
   rail-specific in `rail.css` under `html[data-ui-layout="rail"]`.
   Both sheets are loaded globally and MUST stay inert for default
   instances (scoping is contract-tested).
4. **One accent vocabulary per meaning:**
   - `--ds-primary` family — the ONE brand action color (primary CTA,
     active nav, selected states). Never for category labels.
   - `--ds-kind-{data,plugin,memory,library,recipe}` + `-soft` — the
     categorical "sticker" palette for entity-kind tags. Never the
     brand primary, so categories can't be mistaken for actions.
   - `--ds-kai` / `--ds-kai-dark` / `--ds-kai-soft` / `--ds-kai-line` —
     the assistant **Kai's** identity accent (sky blue). The ONE accent
     for "Kai said / suggests this / is showing you around" surfaces:
     the guided tour, "Ask Kai" affordances, the "Ask Kai in Agnes"
     card, assistant voice cards, the "Kai is using…" pill. Deliberately
     distinct from `--ds-primary` (green brand) so Kai reads as its own
     voice inside the Agnes platform. Never reused for structural UI.
   - `--ds-agnes` / `--ds-agnes-soft` / `--ds-agnes-line` — the green
     platform/brand accent kept for the user's own chat surfaces (e.g.
     the user's message bubble) and legacy assistant callouts not yet
     migrated to `--ds-kai`. Never reused for structural UI.
   - Status: `--ds-accent-{info,warn,success,danger}-{bg,ink,line}`.
5. **Shape contrast is meaningful.** Every labelled button — primary,
   secondary, ghost, toolbar CTA, detail-header CTA — wears
   `--ds-radius-btn` (9px), the same radius as the toolbar controls and
   inputs it stands beside. Pill radius (`--ds-radius-pill`) is the
   BADGE language: category tags, status chips, counters, avatars, dots,
   and circular icon-only buttons. Never on a labelled button, never on
   an input. Dense per-row actions keep tight ~8px corners. One carve-out:
   the two connect-banner CTAs (`.cbn-cta`, `.klb-cta`) are fully round
   under every theme — the pill is what marks the product-model banner as
   marketing surface rather than page chrome. Don't extend it further.

   **Corollary — "you can change this" is a FORM signal, not a hue.** Where a
   control and a read-out sit in the same strip, the container separates them:
   *filled* = the action, *outlined* = a state you can change, *bare text* = a
   state you cannot. Add a chevron to anything that opens a dialog or menu, so
   the cue survives colour-blindness and greyscale (WCAG 1.4.1), and give it a
   ≥24px box (WCAG 2.2 SC 2.5.8) — an adjacent control of similar size denies it
   the spacing exemption. Do NOT reach for `--ds-primary` at rest to mean
   "interactive" on a surface that already spends primary on hover/focus: on a
   card whose `:hover` warms the title and whose `:focus-within` draws a primary
   ring, primary ink reads as "the pointer is here", not as "this is a control",
   and the local hover then has to out-shout the ambient one. Answer hover with a
   *background* change instead — a different channel from a card's elevation
   hover, so the two can't be confused. Worked example: `.fbar-card__access` in
   `filter_toolbar.css` (grid card) beside `.lib-vis` in `library.html` (table
   badge) — same anatomy and same words, tinted in the table where the chip owns
   a column, outlined on the card where it shares a bar with the primary action.
6. **Heroes:** content pages use the canonical `.page-header--hero` /
   `.stack-hero` (light card under paper, dark gradient elsewhere).
   The `--ds-hero-*` family stays DARK under paper (the one "night"
   moment: /home install hero, terminal mockups). Don't hand-build
   heroes; don't assume ink color — use tokens.
6b. **Resource detail pages are ONE template, never a family of
   lookalikes.** Every entity with a detail page — data package, plugin,
   skill, agent, file, collection, upload, memory domain, recipe, table —
   renders through `templates/macros/_detail.html` + `css/detail-page.css`.
   The reference implementation is `catalog_package_detail.html`; read it
   before building a new one. The contract:

   - **Header:** `detail.hero(...)` and nothing hand-written. It carries
     the title, the `type_label` badge, the trust marker, and exactly ONE
     prominent action; everything else goes in `detail.menu(...)`. A
     client-hydrated page uses `name_id` / `icon_id` / `body_slot` for its
     JS hooks — the marketplace pages hand-wrote their header for exactly
     four hooks and lost the badge, the rail and the menu doing it.
   - **Container language:** `detail--panels` is the shared DEFAULT
     (`hero(panels=False)` to escape it, and only for a surface designed
     against the borderless rhythm). One rule: *a section is a panel;
     everything inside a panel is whitespace.* No cards inside panels.
   - **Main column:** the subject — what it is / does, then the objects it
     contains (`detail.objects(...)` for any "what is inside this" list),
     then examples (`detail.questions(...)`).
   - **Rail:** the facts, always in this order, each skipped when empty —
     About prose → the metadata read-out (`side_rows`, **unlabelled**: the
     rows name their own values) → Owner/Publisher → Sharing →
     Availability → Versions. Put an entity-specific block where it belongs
     in that order; don't invent a new position for it.
   - **Owner/admin tools are not a block in the flow.** The action ladder is
     `store_menu()` in the header's overflow menu, history is
     `version_timeline()` in the rail, and only an alert about the page
     (the quarantine banner) sits above the content.
   - **Shared concepts use the shared component**, always:
     `visibility_chip` / `side_sharing`, `version_timeline`, `store_menu`,
     `owner`, `status`, `side_panel_*`, `related`, `objects`, `empty`. If
     you are about to write page-local CSS for one of these, the component
     is missing a parameter — add it there instead.
   - **Gating.** The whole redesign is opt-in. `cols_open` / `aside_open` /
     `cols_close` emit nothing on the legacy path, but any content that
     MOVED (the rail's blocks, a re-worded or relocated main section) must
     sit behind `{% if detail.redesign %}` with the legacy markup kept
     verbatim in the `{% else %}`. Guarded by
     `tests/test_ui_layout_theme.py::TestDetailPageTemplateIsShared`.

7. **Motion:** use `--ds-motion-{fast,med,slow}` +
   `--ds-ease-{standard,enter}`; honor `prefers-reduced-motion` on
   anything that moves.
8. **Both chromes must keep working.** Grant gating (`can_chat`),
   admin sections, `data-tour` anchors, and the JS id contract
   (`#global-search`, `#userMenu`, `#themeToggle`) exist in BOTH
   `_app_header.html` and `_app_rail.html` — if you touch one, mirror
   the other (`tests/test_ui_layout_theme.py::TestRailOptIn` asserts
   the rail side). The two chromes deliberately differ in IA: topnav
   keeps the flat link row (Home · Chat · Marketplace · Data Packages
   · Library · Memory); the rail is **two fixed zones with the
   conversation list between them** — top: New chat + the newest 5
   chats (no "Chats" heading; `View all chats` expands the list, which
   scrolls inside `.rail-history-body`); bottom: Library · Agents, then
   Admin behind the nav's only divider, then the onboarding card
   (`Set up Agnes` → `Continue setup`, tinted `--ds-accent-info-*`, gone
   at 5/5), then the profile. Neither zone may move when the list grows.
   Admin's seven areas are fixed subitem rows whose links open in a
   flyout BESIDE the rail (`.rail-admin-flyout`, absolutely positioned,
   revealed by `:hover` / `:focus-within`) — nothing in the rail may
   expand inline, or the zones drift page to page. Note the two traps:
   a closed `<details>` cannot host a hover-revealed panel (Chrome's
   `::details-content { content-visibility: hidden }` beats any author
   `display`), and the rail's only script is chat-gated, so admin
   chrome must work with zero JS.
   Every row shares one height (`--rail-row-h`) and the active
   destination is the ONLY tinted row — never add a standing CTA tint.
   The Studio dropdown, the Marketplace entry and the `.rail-sub-i`
   subcategory tree under Catalog are all retired, and My Stack is
   demoted out of the rail (#1088) — `/stack` stays live, reached from
   the Library header, the chat hero counts and the `/catalog` lede. A
   new content surface reaches the caller through an existing
   destination (the Library's "+ New" menu, chat suggestions, search),
   not by growing the rail.
9. **Verify visually.** After any UI change, run the app with both
   configs and screenshot: default (nothing set) and
   `AGNES_INSTANCE_THEME=paper AGNES_UI_LAYOUT=rail`. A page that
   only looks right in one mode is not done. (Chrome context: routes
   must spread `_chrome_ctx(request, user)` or the page renders bare.)

## Where things live

| Concern | File |
|---|---|
| Token palettes (all themes) | `app/web/static/css/design-tokens.css` |
| Paper shape/type skin | `app/web/static/css/paper-skin.css` |
| Rail chrome CSS | `app/web/static/css/rail.css` |
| Rail chrome markup | `app/web/templates/_app_rail.html` |
| Topnav chrome markup | `app/web/templates/_app_header.html` |
| Theme/layout resolvers | `app/instance_config.py` (`get_instance_theme`, `get_ui_layout`) |
| Config surface | `app/api/config_surface.py`; docs `docs/CONFIGURATION.md` |
| Guards | `tests/test_design_system_contract.py`, `tests/test_ui_layout_theme.py` |

## Adding a new theme

1. Add the value to the whitelist in `get_instance_theme()`.
2. Add `:root[data-theme="<name>"]` in `design-tokens.css` overriding
   BOTH families: the `--ds-*` set AND the legacy compat shims
   (`--primary`, `--background`, `--surface`, `--text-*` …) — follow
   the `paper`/`dark` blocks as the template.
3. If the theme needs shape/type changes, add a scoped skin sheet and
   load it from BOTH bases (`base_ds.html`, `base.html`).
4. Extend `tests/test_ui_layout_theme.py` with the new value.
5. Document in `docs/CONFIGURATION.md` + `config/instance.yaml.example`.
