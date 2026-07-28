# Agnes Interface Design System

Captured after the design-system unification (see
`.design/design-system-unification/DESIGN_BRIEF.md` for the rationale).
The **CSS files are the source of truth** — `app/web/static/css/design-tokens.css`
for the canonical tokens, `app/web/static/style-custom.css` for the
component rules. This doc captures the conventions that aren't
written in CSS and the rules `/interface-design:audit` and
`/interface-design:critique` should check against.

---

## Philosophy

**Calm editorial dashboard.** Borders-dominant, shadows reserved.
Green primary + navy hero + white card surfaces. One palette, one
button vocabulary, one accent vocabulary across cards / flashes /
badges / callouts.

The product's world is *terminal output and data freshness* — green
is the colour of `agnes pull` succeeding, of `[OK]` lines, of a
sync-state dot. Not the corporate-trust blue of generic data SaaS.

---

## Palette — one system

The legacy `--primary` (blue) family is **aliased** to `--ds-primary`
(green) via the compat shim at the bottom of
`design-tokens.css`. Every `var(--primary)` consumer renders green
without a selector change. Roll back by re-commenting the shim.

| Token family | Use |
|---|---|
| `--ds-primary` (`#2ea877`) — green | CTAs, active states, link colour, focus outline, eyebrow text on light surfaces |
| `--ds-primary-dark` (`#1f8a5e`) | Button hover, code-chip ink, link hover |
| `--ds-primary-light` (`#e6f9f0`) | Inline `<code>` chip background, info-light fills |
| `--ds-hero-bg` (`#0f1b3a`) — navy | `.page-header--hero`, dashboard cover, terminal mock background |
| `--ds-code-bg` (`#0c1224`) — deep navy | `.code-block` (install command, snippet) |
| `--ds-bg` (`#f6f7fa`) — page bg | App background everywhere |
| `--ds-surface` (`#ffffff`) — cards | All panels, tables, modals |
| `--ds-surface-dim` (`#f0f2f6`) | Row hover, button-secondary hover, button-ghost hover, table thead bg, zebra stripe — **one fill, four patterns** |
| `--ds-border` (`#e4e7ee`) | Default 1px borders |

### Cross-pattern token reuse

This is the unification's killer feature. One token feeds many patterns:

- **`--ds-surface-dim`** → row hover, secondary-button hover, ghost-button hover, zebra stripe. "Interactive" reads identically across patterns.
- **`--ds-focus-outline`** (2px solid `--ds-primary`, 2px offset) → buttons, tabs, table rows, form inputs, clickable cards. One focus ring, every interactive element.
- **`--ds-accent-{info,success,warn,danger}-{bg,ink,line}`** → flashes, badges, cards, callouts, legacy `.card-error/.card-highlight/.card-ai`, modal accents. **One status vocabulary, five surfaces.** Re-tinting "danger" is a one-token edit that updates every status-coloured surface together.

---

## Components — variant catalog

### Buttons — `.btn` family

Four semantic variants + two modifiers + one special case.

| Class | Visual | Use |
|---|---|---|
| `.btn-primary` | Green fill, white ink | One per section — the "do it" affordance |
| `.btn-secondary` | Transparent + 1px border, hover fills `--ds-surface-dim` | Alternate action |
| `.btn-ghost` | No border, hover fills `--ds-surface-dim` | Dense / inline action |
| `.btn-danger` | Red ink + red border, fills red on hover | Destructive — "committed to by hover," not announced by default |
| `.btn-sm` | Modifier — 6×12 pad, 12px text, 6px radius | Composes with any variant |
| `.btn--icon` | Modifier — square, no label | Icon-only (copy buttons, close, dismiss) |
| `.btn-google` | OAuth brand fidelity | Single-purpose special case |
| `.btn.copied` | Success-flash state (accent-success colours) | Stacks with any variant; brief 1.2s flash after copy |

**Compat shims** (auth pages, legacy consumers):
- `.btn-block` — full-width utility; composes with any variant
- `.btn-link` — hyperlink-style button; `--ds-primary` ink, underline on hover

**Rejected variants** (never re-introduce): `.btn-warning`, `.btn-lg`, `.btn-copy*` (sub-component of code-block surfaces, not a button), `.btn-sm-primary`, `.btn-sm-secondary`, `.btn-primary-v2`, `.btn-secondary-v2`, `.btn-ghost-v2`.

### Tables — `.ds-table` family

One canonical name + a `:is()`-aliased list of 15 legacy class names that inherit the same visuals.

| Class / modifier | Effect |
|---|---|
| `.ds-table` | Canonical: 1px border, 8px radius, sticky header, `--ds-surface-dim` row hover (matches button hover), tabular-nums |
| `.ds-table--dense` (aliases `.data-table--compact`) | Reduced cell pad — reference tables, audit logs |
| `.ds-table--zebra` | Optional row striping for very long lists |
| `td.num` / `th.num` | Right-aligned monospace column — IDs, hashes, timestamps, counts |

Sticky header is on by default; harmless when the parent doesn't scroll.

Legacy aliases (all render identically): `data-table`, `ad-table`, `ea-table`, `md-table`, `members-table`, `obs-table`, `overview-stats-table`, `registry-table`, `sample-table`, `sched-table`, `sess-table`, `sub-table`, `subs-table`, `ud-table`, `versions`.

**Note:** `admin_sessions`, `admin_usage`, `activity_center` carry page-local `.obs-table` overrides for sort arrows + selection + error rows. Those are additive enhancements and stay; the canonical applies to all other consumers.

### Cards — `.ds-card` family

| Class / modifier | Effect |
|---|---|
| `.ds-card` | White surface, 1px border, 8px radius, 24px pad, whisper-shadow |
| `.ds-card--accent` | 4px left border in `--ds-card-accent` (defaults to `--ds-border`) |
| `.ds-card--info` / `--success` / `--warn` / `--danger` | Sets `--ds-card-accent` to the matching accent line |
| `.ds-card[data-clickable]` | Opt-in hover lift; plain cards don't move |
| `.stack-card` (in `stack_card.css`) | Marketplace/catalog tile, 12px radius — kept as-is |

**Re-skinned, not retired:** `.card-error` (danger), `.card-highlight` (info), `.card-ai` (success) — each now reads from `--ds-accent-*-line` and **no longer carries a background tint**. The 4px left bar is the only colour cue. Background tints were brief-rejected — they add noise.

**One-off cards left intact** for follow-up template work (each carries intricate sub-element layout): `.cc-setup-card`, `.ai-setup-card`, `.next-step-card`, `.data-link-card`, `.notifications-card`, `.memory-widget`.

### Tabs — `.tab-strip` family

- `.tab-strip` + `.tab-strip__item` is the canonical pattern (marketplace, catalog, /me/activity).
- Item active state: `--ds-primary` ink + `--ds-primary` border-bottom (works for both `.is-active` and `[aria-selected="true"]`).
- Focus ring: same `--ds-focus-outline` as every interactive element.
- `.auth-tabs` / `.auth-tab` is the **heavier login-page variant** (2px border, flex:1 fill, 15px text) — palette-only update; structural unification waits for a future cleanup that adds `.tab-strip--heavy`.

### Code surfaces

| Class / tag | Treatment |
|---|---|
| `code`, `.code-inline` | Mint chip: `--ds-primary-light` bg + `--ds-primary-dark` ink, 4px radius, mono font. Replaces the legacy grey-blue chip. |
| `.code-block` | Navy `--ds-code-bg` surface + `--ds-code-ink`, 8px radius, mono font. The product signature — visually rhymes with the terminal where `agnes pull` runs. |
| `.code-output` (in `components.css`) | Dashed "What you should see" expected-output block. Already canonical. |
| `.page-header--hero code` (~line 4192) | Dark-surface override: yellow on translucent white. Wins on hero panels via specificity. |

Still alive on dark surfaces with page-local Catppuccin treatment (intentional, do not migrate without rewriting the surface): `.btn-copy`, `.btn-copy-term`, `.cmd-chip .btn-copy`, install.html's `.code-block primary`, dashboard.html's `.terminal-lines`.

### Callouts (unchanged, already canonical)

- `.callout-hint` — blue info, 3px left border
- `.callout-rec` — amber recommendation, lightbulb prefix
- `.code-output` — dashed expected-output

### Flashes — accent vocabulary

`.flash-success` / `.flash-error` / `.flash-info` / `.flash-warning` now read directly from `--ds-accent-{success,danger,info,warn}-{bg,ink,line}`. Same vocabulary as badges, cards, callouts.

### Badges — accent vocabulary

- `.badge` — neutral chip on `--ds-surface-dim`
- `.badge--info` / `--success` / `--warn` / `--danger` — canonical role-agnostic modifiers
- `.badge-admin` / `-analyst` / `-privileged` — legacy role-named aliases; map onto info / success / warn respectively

---

## Scales

| Scale | Tokens | Notes |
|---|---|---|
| **Spacing** (base 4) | `--space-1` (4) … `--space-12` (64) | Most-used: 8, 12, 16, 24 |
| **Radius** | `--radius-sm` (4) … `--radius-2xl` (16), `--radius-full` | Default = `--radius-lg` (8px) — buttons, cards, callouts |
| **Type sizes** | `--text-xs` (10) … `--text-2xl` (30) | Body = `--text-base` (14) |
| **Weights** | `--font-normal` (400) … `--font-extrabold` (800) | 500 = button text, 600 = card titles, 700 = headlines |
| **Line heights** | `--leading-tight` (1.2), `--leading-snug` (1.4), `--leading-normal` (1.55), `--leading-relaxed` (1.7) | Body = `--leading-normal` |
| **Letter spacing** | `--tracking-tight` (-0.5px), `--tracking-normal` (0), `--tracking-wide` (0.4px), `--tracking-eyebrow` (1.2px) | Wide = tags + status pills; eyebrow = `.setup-section-header` |
| **Shadows** | `--shadow-sm`, `--shadow-md`, `--shadow-card`, `--shadow-lg` | Cards use sm; heavy = lightbox + modals only |
| **Motion** | `--transition-instant` (50ms), `--transition-fast` (120ms), `--transition-base` (200ms), `--transition-slow` (320ms) | Fast = hover, base = tab/panel transitions |
| **Widths** | `--width-app` (1280), `--width-wide` (1400), `--width-form` (720) | **`--width-narrow` was dropped** — single-form pages use `max-width: var(--width-form)` on the form element |

---

## Depth strategy

**Borders-dominant, shadows reserved.**

- Default depth = 1px `--ds-border` outline. Adequate for all panels.
- Optional whisper shadow (`--shadow-sm`) on `.ds-card` and `.stack-card` — barely visible alone, but lifts a card off `--ds-bg` when stacked.
- Heavy shadows (`--shadow-lg`) reserved for `.lightbox` overlay and modal surfaces.
- Hero / terminal surfaces use deep-navy (`--ds-hero-bg`, `--ds-code-bg`) instead of shadows — depth-by-contrast.

---

## Typography conventions

- Body: 14px (`--text-base`) / `--leading-normal` (1.55) / `--ds-text-primary`
- Page heading: 28px / 700 (`.setup-section-header .setup-heading`)
- Eyebrow above headings: 11px / 700 / UPPERCASE / `--tracking-eyebrow` (1.2px), coloured `--ds-primary`
- Table headers: 10px / 600 / UPPERCASE / `--tracking-wide` (0.4px) / `--ds-text-muted`
- Meta / caption: 11–12px, `--ds-text-secondary` or `--ds-text-muted`
- Tags / status pills: 10–11px UPPERCASE / `--tracking-wide`
- Code: `--ds-font-mono`, 12.5px in callouts; inline inherits font-size, mint chip background

---

## When in doubt

1. **Read the tokens first** — `design-tokens.css` carries detailed comments explaining every choice.
2. **Use the token, not the literal** — `var(--space-3)` not `12px`, `var(--ds-primary)` not `#2ea877`.
3. **Extend the canonical family** (`.btn-*`, `.ds-table-*`, `.ds-card--*`) rather than forking a parallel class.
4. **One CTA per section.** If you're adding a second `.btn-primary` next to an existing one, ask whether one of them is actually `.btn-secondary` or `.btn-ghost`.
5. **One accent vocabulary.** A "this needs attention" panel uses `.ds-card--warn` (which sets `--ds-card-accent: var(--ds-accent-warn-line)`) — never `border-left: 4px solid orange`.
6. **Sticky-by-default on tables.** Wrap a table in a `max-height: 60vh; overflow: auto` div and the sticky header just works.
7. **Focus ring is non-negotiable.** Every interactive element gets `:focus-visible { outline: var(--ds-focus-outline); outline-offset: var(--ds-focus-outline-offset); }`. No `outline: none` carve-outs.

---

## Known follow-ups (not yet shipped)

- `.cc-setup-card`, `.ai-setup-card`, `.next-step-card`, `.data-link-card`, `.notifications-card`, `.memory-widget` — collapse onto `.ds-card` + content layout when those pages are touched.
- `.auth-tabs` — absorb into `.tab-strip --heavy` modifier when login flow is refactored.
- `.code-block-wrapper` / inner `.code-block` (the translucent dark variant at line ~2278) — re-skin onto `.code-block` + `--surface` modifier.
- `.btn-copy` / `.btn-copy-term` / `.cmd-chip .btn-copy` dark-surface treatments — unify when the `.code-block` family gains a copy-button slot.
- Page-local `.obs-table` overrides in `admin_sessions`, `admin_usage`, `activity_center` — extract sort-arrow + selection-state into shared `.ds-table--sortable` / `.ds-table tbody tr.is-selected` rules.
- Page-local `.container` overrides (profile.html 1100px, news.html / admin/news_editor 1280px redundant) — audit each, drop redundant ones.
- Dark mode — `[data-theme="dark"]` scaffold exists in `design-tokens.css` but no overrides defined. Future brief.
