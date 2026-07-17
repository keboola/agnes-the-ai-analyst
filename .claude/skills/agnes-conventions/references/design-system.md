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
   - `--ds-agnes` / `--ds-agnes-soft` / `--ds-agnes-line` — the
     assistant's own voice (suggestion cards, "Agnes recommends"
     surfaces). Never reused for structural UI.
   - Status: `--ds-accent-{info,warn,success,danger}-{bg,ink,line}`.
5. **Shape contrast is meaningful.** Pill radius
   (`--ds-radius-pill`) is reserved for the one prominent CTA per
   context and for badge/category tags; dense per-row actions keep
   tight ~8px corners; inputs/selects are NEVER pill-shaped.
6. **Heroes:** content pages use the canonical `.page-header--hero` /
   `.stack-hero` (light card under paper, dark gradient elsewhere).
   The `--ds-hero-*` family stays DARK under paper (the one "night"
   moment: /home install hero, terminal mockups). Don't hand-build
   heroes; don't assume ink color — use tokens.
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
   · Library · Memory); the rail uses the prototype IA — Chat,
   My Stack (→ `/catalog?tab=my`), and Catalog with the content
   surfaces as subcategories (Data Packages, Plugins, Library,
   Memory). New content surfaces join the rail as another
   `.rail-sub-i` under Catalog, not as a new top-level item.
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
