# Design Brief: Design System Unification

Cross-cutting cleanup of the FastAPI web UI: collapse the zoo of one-off
classes (~10 button variants, 9 table class names, ~12 card/panel
variants, two coexisting palettes) into a single, opinionated component
vocabulary on the green/navy `--ds-*` palette — with **minimal markup
churn** and **no structural rework** of pages that already function.

Target pages: `/home` (onboarded + not-onboarded + setup + setup_advanced),
marketplace (browse, guide, item detail, plugin detail, format guide),
catalog (browse + package/recipe/table detail), corporate-memory
(browse + domain detail), and all admin pages. Auth/error pages get
palette-only updates (no structural changes).

## Problem

Today the UI is vibecoded — patterns were invented per page as they
were built, and consistency was patched in only where a regression
was noticed. Concretely:

- **Two palettes coexist.** Legacy `--primary` blue powers the chrome
  on most pages; the new `--ds-*` green/navy is scoped to `/home` and
  `/setup`. The rule "`.btn-primary` is always green, regardless of
  page palette" exists *because* the surrounding chrome is the wrong
  colour and the CTA has to compensate. The compensation rule is a
  tell that the underlying split needs to go.
- **~10 button variants.** `.btn-copy`, `.btn-copy-inline`,
  `.btn-copy-block`, `.btn-copy-code`, `.btn-sm-primary`,
  `.btn-sm-secondary`, `.btn-block`, `.btn-link`, `.btn-google`, plus
  `.btn` / `.btn-primary` / `.btn-secondary`. The names describe
  *where the button lives*, not *what it does* — a sign each was
  invented in isolation.
- **9 table classes.** `data-table`, `obs-table`, `sched-table`,
  `subs-table`, `members-table`, `ud-table`, `ea-table`, `ad-table`,
  `versions`. Only `.ud-table` even has rules in CSS — the rest
  inherit page-local styles or browser defaults, so the same admin
  list looks subtly different on different pages.
- **~12 card/panel variants.** `.card`, `.card-error`,
  `.card-highlight`, `.card-ai`, `.cc-setup-card`, `.ai-setup-card`,
  `.next-step-card`, `.data-link-card`, `.notifications-card`,
  `.memory-widget`, plus the marketplace/catalog `.stack-card` tile.
  Each carries its own padding, radius, and shadow stack.
- **Inconsistent page widths.** Three width tokens (`--width-narrow`
  800, `--width-app` 1280, `--width-wide` 1400) exist but pages still
  ship local `max-width` overrides. Reading `/admin/tables` after
  `/marketplace` feels physically jarring because the page jumps in
  width.

The human friction this produces, from the developer-as-user
perspective: building a new page or fixing a bug means **deciding
which precedent to follow** every time, and decisions on similar
problems land in different places. From the analyst-as-user
perspective: the UI feels stitched-together rather than crafted —
buttons jump colours, panel corners change, table densities shift
between pages.

## Solution

A unified `--ds-*` component vocabulary applied across all four page
families, plus an explicit "one way to do each thing" rule. The work
is mostly **class renames + CSS consolidation** — not a redesign.
Every page keeps its current information architecture, copy, and
flow; only the surface vocabulary changes.

Concretely the analyst experiences:

1. **One palette.** Green CTAs, navy hero/code surfaces, white card
   surfaces, blue info / amber rec callouts. The legacy `--primary`
   blue is remapped to `--ds-primary` green via a compatibility shim
   so unconverted markup stops looking foreign immediately; the shim
   gets removed once the migration is done.
2. **One button vocabulary.** Four semantic variants
   (primary / secondary / ghost / danger), one size modifier (`.btn-sm`),
   one icon-only modifier (`.btn--icon`), plus the special-case
   `.btn-google` for OAuth brand fidelity. Every existing variant
   collapses to one of these.
3. **Two table styles** — default `.ds-table` and `.ds-table--dense`.
4. **Three card variants** — `.ds-card`, `.ds-card--accent`,
   and the existing `.stack-card` for tile grids.
5. **One tab pattern** — extend `.tab-strip`, retire `.auth-tabs`.
6. **Three code surfaces** — `.code-inline`, `.code-block`, `.code-output`.
7. **Two page widths** — default 1280 and wide 1400. The narrow
   800-width is dropped; reading-width forms use a max-width on the
   form element itself instead.

## Experience Principles

1. **One way to do each thing** — Every common UI primitive has a
   single canonical class with at most a small, named variant set.
   When in doubt, do not invent — pick the closest existing variant
   and extend its BEM family rather than fork a parallel class.

2. **Tokens, not literals** — Every spacing, radius, font-size, and
   colour reads from a `--ds-*` (or `--space-*`, `--radius-*`,
   `--text-*`) token. A raw `12px` or `#2ea877` in the diff is a code
   smell unless it's *defining* a token. This is what makes future
   palette/density tuning a single-file edit instead of a grep job.

3. **Minimal disruption over ideal layout** — A working page is
   precious. The cleanup changes *class names* and *shared rules*; it
   does not rearrange information architecture, change copy, or
   reorder sections. If a page is broken-and-shipped, it stays
   broken-and-shipped after this pass — separately tracked.

## Aesthetic Direction

- **Philosophy**: Calm editorial dashboard — the green/navy palette
  already established by the `/home` redesign, applied consistently
  everywhere. Borders-dominant, shadows reserved. System font stack
  (San Francisco on macOS, Segoe UI on Windows, Inter fallback).
  Deep navy as the "depth-by-contrast" surface for hero/code panels;
  white cards on a soft `--ds-bg` page background everywhere else.

- **Tone**: Confident, calm, opinionated. The interface tells the
  reader what to do next without raising its voice — green CTAs are
  the loudest element on a page, and there is at most one per
  section.

- **Reference points**: Linear (typography rhythm, calm dashboard
  density), Vercel dashboard (navy hero + white surfaces),
  Plausible Analytics (one-button-per-decision discipline), GitHub
  primer (data-table style: 1px borders, no zebra by default).

- **Anti-references**: Generic SaaS rainbow (purple action buttons,
  multi-coloured tabs, varied accent hues per section); Material
  Design heavy elevation (deep drop-shadows replacing borders);
  dense enterprise tables with zebra-striping and bordered cells
  everywhere; Bootstrap-style "kitchen sink of button colours"
  (`btn-info`, `btn-warning`, etc.).

## Existing Patterns

These are the canonical pieces already correct in the codebase —
the work extends them rather than replaces them.

- **Tokens** (`app/web/static/css/design-tokens.css`):
  - Colour: `--ds-primary` (#2ea877), `--ds-primary-dark` (#1f8a5e),
    `--ds-hero-bg` (#0f1b3a), `--ds-code-bg` (#0c1224), `--ds-bg`,
    `--ds-surface`, `--ds-border`, semantic info/warn/success/error.
  - Spacing scale: `--space-1` (4) … `--space-12` (64); most-used
    are `--space-2/3/4/6`.
  - Radius scale: `--radius-sm` (4) … `--radius-2xl` (16) +
    `--radius-full` (9999).
  - Type scale: `--text-xs` (10) … `--text-2xl` (30); body is
    `--text-base` (14).
  - Widths: `--width-narrow` (800), `--width-app` (1280),
    `--width-wide` (1400). **This brief drops `--width-narrow`** —
    the rare narrow-form case uses a `max-width` on the form
    element, not the page shell.
  - Motion: `--transition-fast` (120), `--transition-base` (200),
    `--transition-slow` (320).
  - Shadows: `--ds-shadow-sm/md/lg`; reserved for hero/lightbox
    surfaces — the default depth is a 1px border.

- **Container shell** (`style-custom.css:143`): `.container`
  centres at `--width-app` with `--space-4` horizontal padding;
  `.container--wide` opts into `--width-wide`. **This brief drops
  `.container--narrow`.**

- **Page header** (`style-custom.css:4075`): `.page-header`,
  `.page-header--hero` (navy gradient), `.page-header--compact`.
  Already canonical; no change.

- **Stack card** (`stack_card.css`): `.stack-card` and its BEM
  family (`__btn`, `__tag`, `__status-pill`). The reference for "do
  the BEM family thing, don't fork." No change in this brief.

- **Callouts** (`components.css`): `.callout-rec` (amber) +
  `.callout-hint` (blue) + `.code-output` (terminal expected-output).
  Already canonical; no change.

- **Tab strip** (`style-custom.css:4217`): `.tab-strip` +
  `.tab-strip__item` + `.is-active` / `[aria-selected="true"]`.
  Extend to absorb `.auth-tabs`.

Full reference: `.interface-design/system.md` (the audit doc).

## Component Inventory

| Component                | Status   | Notes                                                                                                       |
|--------------------------|----------|-------------------------------------------------------------------------------------------------------------|
| `.btn`                   | Modify   | Base shape unchanged; ensure all variants are size-consistent (10×20 default, 6×12 `-sm`).                  |
| `.btn-primary`           | Modify   | Stays green-filled. Drop the "always-green-regardless-of-palette" carve-out — once palette is unified the rule becomes trivial. |
| `.btn-secondary`         | Modify   | Transparent + 1px border, hover fills `--ds-surface-dim`. Currently inconsistent across pages.              |
| `.btn-ghost`             | **New**  | No border, hover fills `--ds-surface-dim`. Replaces today's borderless `.btn-link`-ish usages.              |
| `.btn-danger`            | **New**  | Red ink/border, fills `--error` on hover. Replaces ad-hoc red buttons (delete actions in admin).            |
| `.btn-sm`                | Modify   | Size modifier only — no colour semantic. Composes with `-primary` / `-secondary` / `-ghost` / `-danger`.    |
| `.btn--icon`             | **New**  | Square icon-only modifier (e.g. copy-to-clipboard, close).                                                  |
| `.btn-google`            | Keep     | OAuth brand fidelity; stays as a single-purpose special-case.                                               |
| `.btn-copy` × 4 variants | **Remove** | All four collapse into `.btn` + `.btn-sm` + `.btn--icon` + `.btn-ghost`.                                   |
| `.btn-sm-primary/secondary` | **Remove** | Composed via `.btn-sm.btn-primary` / `.btn-sm.btn-secondary`.                                            |
| `.btn-block` / `.btn-link` | **Remove** | `btn-block` → utility class `.w-full` or inline style; `btn-link` → `.btn-ghost` or actual `<a>`.        |
| `.ds-table`              | **New**  | Single canonical table: border-collapse, 1px row borders, 12px / `--text-sm` cells, 11px UPPERCASE header. |
| `.ds-table--dense`       | **New**  | Reduces padding for reference tables (`setup_advanced`, `admin_user_detail` properties).                    |
| `.ds-table--zebra`       | **New**  | Optional row striping for very long admin lists.                                                            |
| 9 existing table classes | **Remove** | `data-table`, `obs-table`, `sched-table`, `subs-table`, `members-table`, `ud-table`, `ea-table`, `ad-table`, `versions` all become `.ds-table` (+ modifier where needed). |
| `.ds-card`               | **New**  | Canonical panel: white surface, 1px `--ds-border`, 8px radius, 24px padding, optional `--ds-shadow-sm`.    |
| `.ds-card--accent`       | **New**  | Adds a 4px solid left border in `--accent` (info/success/warn/error). Replaces `.card-error`, `.card-highlight`, `.card-ai`. |
| `.stack-card`            | Keep     | Marketplace/catalog tile grid. Untouched.                                                                   |
| `.cc-setup-card`, `.ai-setup-card`, `.next-step-card`, `.data-link-card`, `.notifications-card`, `.memory-widget` | **Remove** | All collapse to `.ds-card` + content layout.                                |
| `.tab-strip`             | Modify   | Absorb `.auth-tabs` styling as a `.tab-strip--pill` modifier (or keep base if pill is the canonical look).  |
| `.auth-tabs`             | **Remove** | Migrates to `.tab-strip`.                                                                                  |
| `.code-inline`           | **New**  | Inline `<code>` chip — `--ds-primary-light` bg, `--ds-primary-dark` ink, 4px radius. Replaces the dozen inline `<code>` style overrides scattered through templates. |
| `.code-block`            | Modify   | Navy `--ds-code-bg` surface, `--ds-code-ink` text, 8px radius, copy button slot in the top-right. Currently duplicated across pages with slight variation. |
| `.code-output`           | Keep     | Dashed "what you should see" block. Already canonical.                                                      |
| `.terminal-body`, `.code-block-wrapper`, `.command-row` | **Remove** | Absorbed into `.code-block` (and `.code-block--cmd` modifier where needed). |
| `.container`             | Modify   | Default to `--width-app` (1280); add `.container--wide` (1400). **Drop `.container--narrow`** and remove every page-local `max-width` override. |
| Page width: narrow (800) | **Remove** | Replaced by a `max-width: 720px` on the *form element* in the few pages that need it (login, password setup). |
| `.page-header` family    | Keep     | Canonical hero/compact/default already wired up across the app.                                             |
| `.callout-hint`, `.callout-rec` | Keep | Already canonical.                                                                                       |
| `.flash` family          | Modify   | Re-skin to `.ds-card--accent` semantics (info/success/warn/error), reuse the same accent token vocabulary. |
| `.badge` family          | Modify   | Three semantic badges (`.badge-analyst`, `.badge-privileged`, `.badge-admin`) consolidate to `.badge` + `--accent` modifier reading from the same accent vocabulary as `.ds-card--accent` and `.flash`. |

## Key Interactions

The unification is mostly visual, but a few interaction states must be
explicitly consistent across the new vocabulary:

- **Button hover**: `--transition-fast` (120ms) on background. Primary
  darkens to `--ds-primary-dark`; secondary fills `--ds-surface-dim`;
  ghost fills `--ds-surface-dim`; danger fills `--error`. No
  translate, no scale — calm.
- **Button active (pressed)**: 1px translateY down, no shadow change.
- **Button focus-visible**: 2px outline in `--ds-primary` at 2px
  offset on every variant — the same focus ring everywhere makes
  keyboard navigation predictable.
- **Table row hover**: `--ds-surface-dim` background, `--transition-fast`.
  Only on tables whose rows are clickable (admin lists, catalog browse);
  static reference tables (setup_advanced) do not hover.
- **Card hover**: only `.stack-card` (clickable tile) and `.ds-card` with
  an explicit `[data-clickable]` attribute lift on hover; plain
  panels do not move.
- **Tab activate**: 200ms colour + border-bottom transition; the
  underline moves with `--transition-base`. No content fade (content
  swap is instant; only the chrome animates).
- **Code copy click**: button briefly shows "Copied" label for 1.2s,
  then returns. State change uses no animation — the label swap is
  the entire feedback.

## Responsive Behavior

This brief preserves existing responsive behavior — it does **not**
introduce new breakpoints or rearrange grids. Specifically:

- **Page container** is centred with horizontal padding; on viewports
  <640px the padding drops from `--space-4` to `--space-3`.
- **Buttons** stay inline on desktop; on mobile, buttons inside
  `.page-header__actions` stack vertically and stretch to full width.
  This is already the existing behavior.
- **Tables**: `.ds-table` allows horizontal scroll on the parent
  container at <768px rather than reflowing — the same as today.
  `.ds-table--dense` is small enough to fit on mobile without scroll.
- **Cards**: stacked vertically on mobile; current grid behavior is
  preserved (e.g. `.dashboard-grid` 2-column → 1-column collapse).
- **Tabs** (`.tab-strip`): horizontal scroll on overflow rather than
  wrap — keeps the row clean on narrow viewports.

## Accessibility Requirements

Each is a non-negotiable checkpoint for the cleanup PRs:

- **Contrast**:
  - `.btn-primary` (white on `--ds-primary` #2ea877): verify ≥ 4.5:1
    against AAA-level large text or ≥ 3:1 against AA. The token may
    need a slight ink-on-green darkening if it fails on smaller
    sizes — prefer changing the token over reverting.
  - All body text on `--ds-bg` and `--ds-surface`: ≥ 7:1 (AAA) for
    `--ds-text-primary`; ≥ 4.5:1 (AA) for `--ds-text-secondary`.
  - Navy hero surfaces: ink is `--ds-hero-ink` #f3f6ff — verify ≥
    7:1 against `--ds-hero-bg` #0f1b3a.
- **Keyboard navigation**: every interactive element reachable via
  Tab; focus order matches visual order. `.tab-strip` items use
  arrow-key navigation between tabs (already in place — verify
  retained post-migration).
- **Focus-visible**: a single, consistent 2px outline in
  `--ds-primary` with 2px offset on every button, link, tab, table
  row, and form input. No `outline: none` carve-outs.
- **Screen-reader semantics**:
  - `.ds-table` rows clickable via `<a>` overlay, not a JS-only
    click handler — the row remains keyboard-navigable.
  - `.tab-strip` uses `role="tab"` + `aria-selected` + `aria-controls`.
  - Decorative icons in buttons use `aria-hidden="true"`; icon-only
    `.btn--icon` always carries `aria-label`.
- **Reduced motion**: respect `prefers-reduced-motion: reduce` on
  all transitions (hover lifts, tab underlines). Already partly in
  place; this pass enforces it across new components.

## Out of Scope

The following are deliberately not touched by this brief — if any
turns out to be necessary, file a separate brief rather than expanding
this one:

- **Information architecture changes**. No page reorders sections, no
  new tabs appear, no navigation reorganization. Class names change;
  markup hierarchy does not.
- **Copy / content changes**. Headings, helper text, button labels
  all stay verbatim. (Exception: if a button label is wrong *and* the
  fix is one word, do it in the same PR — but the brief is not the
  reason.)
- **New pages**. This is a cleanup, not a feature.
- **Logo, illustration, or photography**. The aesthetic direction is
  about the component system; brand assets are unchanged.
- **Marketplace tile thumbnails / cover images**. Stay as-is.
- **JS interaction patterns** beyond what's listed under Key
  Interactions. No new client-side behavior; existing handlers stay.
- **Dark mode**. The `--ds-*` palette is light-surface; a future
  brief can add a dark variant once the unification is done.
- **Email templates / PDF exports**. Out of scope; they have their
  own constrained styling layer.
- **Print stylesheets**. Out of scope.
- **Auth pages (structural)**. `login.html`, `login_email.html`,
  `login_magic_link*.html`, `password_*.html`, `error.html`,
  `desktop_link.html` receive palette-only updates (`.btn-primary`
  stays green, `--ds-*` tokens replace any legacy literals). No
  structural changes — layout, copy, and flow stay as today.
- **The catalog `metric_modal.css` and the marketplace
  `stack_card.css`** stay as their own files; they are already
  internally consistent and the unification pulls them into the
  token vocabulary without restructuring them.
