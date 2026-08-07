# Unified switch registry — design

Date: 2026-08-07
Status: design approved, implementation not started

## Problem

Agnes gates behavior through **six unrelated mechanisms**. Only one of them is
a convention; the rest were each invented at their callsite.

1. **`FEATURE_FLAGS` + `feature_enabled`** (`app/instance_config.py`) — the
   canonical convention from #1022. Seven flags. Backs a read-only inventory
   panel on `/admin/server-config`.
2. **Bespoke boolean resolvers** — `get_home_automode_visibility`,
   `get_home_status_frame_visibility`, `get_store_verification_enabled`. Same
   env > yaml > default shape as `feature_enabled`, hand-copied, outside the
   registry, invisible in the panel.
3. **Enum resolvers** — `get_instance_theme`, `get_ui_layout`,
   `get_slack_transport`, `distribution_signed_urls_mode`,
   `resolve_analytics_backend_name`, the coordination factory. Not boolean, so
   the bool-only registry could never hold them.
4. **Env-only switches with inline truthy parsing** — `DEBUG`,
   `AGNES_AUTH_RATELIMIT_ENABLED`, `AGNES_DB_SELF_HEAL`, `EGRESS_BLOCK_PRIVATE`
   and ~10 more. Never in yaml, never in the admin UI, each with its own
   hand-written parser.
5. **Deployment-time selection** — `AGNES_ROLE`, Compose profiles
   (`apps`, `mtier`, `standalone`), overlay compose files.
6. **Admin field metadata** — `_KNOWN_FIELDS` + `_EDITABLE_SECTIONS` in
   `app/api/admin.py`, hand-maintained lists that decide what the UI renders
   and what the API accepts.

### Concrete damage this has already caused

- **`data_apps` cannot be turned on from the admin UI.** It is a registered
  feature flag, but its section is missing from `_EDITABLE_SECTIONS`, so the
  panel displays it while `POST /api/admin/server-config` rejects the write
  with `400 unknown section`. This is the third instance of the same bug class
  — the code comments at `mcp` and `chat` document the two earlier ones, both
  found in review rather than by a guard.
- **The theme selector lies.** `_KNOWN_FIELDS` offers `blue` and `navy`;
  `get_instance_theme` accepts `blue`, `navy`, `dark`, `auto`, `paper`. The
  redesign (#896/#1104) shipped `paper` and `rail` with **no way to enable
  either from the admin UI** — `ui_layout` is not in `_KNOWN_FIELDS` at all.
- **Truthy parsing is inverted between mechanisms.** `coerce_flag_value` is
  permissive (`false` only for `0/false/no/off/""`; anything unrecognized is
  `true`). The inline parsers are the opposite (allow-list for `true`; anything
  unrecognized is `false`). `DEBUG=on` therefore enables the debug branch in
  `app/api/health.py` and not the one in `app/logging_config.py` — one value,
  two behaviors, in the same process.
- **Documentation drifted from runtime** — `chat.approvals_enabled` was
  documented off-by-default while the registry and runtime had it on, which is
  why `_flag_default()` was introduced to derive that value instead of
  re-typing it. That helper is the precedent this design generalizes.

### What the operator experiences

There is no single place that answers "what can I change on this instance, what
is it set to, and where did that value come from?" The read-only flag panel
answers it for seven switches out of roughly thirty-five.

## Goals

- One registry, one resolver, one parser for every switch in the product.
- Every switch visible to an admin, with its **effective** value and the
  **source** that produced it.
- Every switch that can be changed at runtime, changeable from the admin UI.
- The bug classes above become **structurally impossible**, not test-covered.
- Zero behavior change for existing instances until an operator changes
  something, with one documented exception (see Migration).

### Non-goals

- Per-user or percentage rollouts. A switch stays instance-global; per-user
  gating is RBAC and the user's stack, which is a different subsystem.
- Hot-reload of switches that require a process restart. The design *reports*
  restart-pending state; it does not implement reloading.
- Migrating the rest of `/admin/*` to the paper theme. See PR4.

## Decisions

Three decisions shaped everything below. Each was taken explicitly.

| Decision | Choice | Consequence |
|---|---|---|
| Scope | **Everything**, including operational switches | `DEBUG`, rate-limit, self-heal, egress and TLS-verify become yaml-backed and admin-editable, behind a danger gate |
| Env precedence | **Env wins; the UI control locks** | Resolution order is unchanged repo-wide. Infra-managed instances keep Terraform as the source of truth |
| Non-immediate effect | **Per-entry effect class** | Every entry declares `live` / `restart` / `deploy`; the UI states which, per switch |

## Architecture

### The registry

New module **`app/switches.py`**. It does not go into `app/instance_config.py`
— that file is already 1389 lines and this is a separate responsibility with
its own contract.

```python
@dataclass(frozen=True)
class Switch:
    name: str                    # stable id, e.g. "ui_layout"
    config_keys: tuple[str, ...] # ("instance", "ui_layout")
    env_var: str                 # "AGNES_UI_LAYOUT"
    kind: str                    # "bool" | "select" | "int" | "string"
    default: Any
    effect: str                  # "live" | "restart" | "deploy"
    category: str                # "product" | "operations" | "deploy"
    description: str             # operator-facing, one or two sentences
    options: tuple[str, ...] = ()      # select only; must contain `default`
    danger: bool = False               # confirm dialog + high-risk audit
    on_invalid: str = "default"        # "default" | "raise"
    runtime_view: str | None = None    # overlay-only readers (chat)
```

`SWITCHES: tuple[Switch, ...]` replaces `FEATURE_FLAGS`.

### The resolver

`switch_value(name) -> Any` is the single implementation of the resolution
order, unchanged from the documented convention:

```
env var  >  server-config overlay  >  instance.yaml (static base)  >  default
```

The middle two are already deep-merged by `config/loader.py`, so in code it is
`env var > get_value(*config_keys) > default`. Boolean parsing goes through
`coerce_flag_value` — one parser, no exceptions.

`feature_enabled(*keys, env_var=..., default=...)` **stays** as the bool-shaped
facade over `switch_value`, so the 14 existing callsites do not churn.

### Everything else is derived

Nothing below is hand-maintained after this change:

| Derived artifact | Derived from | Bug class it retires |
|---|---|---|
| `_EDITABLE_SECTIONS` | sections of all non-`deploy` switches | flag registered but section not editable (`data_apps`, historically `mcp` and `chat`) |
| `_KNOWN_FIELDS` for switch-backed keys | `kind`, `options`, `default`, `description` | admin offers fewer values than the resolver accepts (`theme`, `ui_layout`) |
| Admin panel rows | the registry + resolver | panel and runtime disagreeing |
| `docs/feature-flags.md` table | the registry | documentation contradicting runtime (`chat.approvals_enabled`) |

### Existing resolvers keep their names

`get_instance_theme()`, `get_ui_layout()`, `get_slack_transport()`,
`get_home_automode_visibility()`, `get_home_status_frame_visibility()`,
`get_store_verification_enabled()`, `distribution_signed_urls_mode()`,
`resolve_analytics_backend_name()` and the coordination factory keep their
public names and signatures — callsites and tests are untouched. Their bodies
collapse to `return switch_value("...")`.

This is where the inline parsing divergence dies, and where the inconsistent
typo behavior becomes declarative: today `ui_layout` falls back silently,
`distribution.signed_urls` falls back with a warning, and `analytics.backend`
raises. After this change that difference is the `on_invalid` field, visible in
one place.

### The chat exception is load-bearing

`app/main.py` boots chat from `load_chat_config(DATA_DIR/state/instance.yaml)`
— the **writable overlay file alone**, not the merged config. The panel must
therefore read `chat.enabled` and `chat.approvals_enabled` from the same
overlay-only source the runtime uses, or it reports a value the gate does not
use. `runtime_view` carries this. It is not leftover complexity to clean up.

## Admin UI

### Placement

The read-only "Feature flags" panel on `/admin/server-config` becomes the
**primary editable surface**, renamed **Switches**, moved to the top of the
section navigation.

**One value, one control.** A switch-backed key no longer renders in its
section form; those forms keep the rest of the configuration (hosts, URLs,
tokens, limits). Each affected section header carries a one-line note with an
anchor to Switches. Without this rule `chat.enabled` would exist twice on one
page and an operator could not tell which control wins.

### Row anatomy

Each row carries four pieces of information that are nowhere together today:

| Element | Meaning |
|---|---|
| Control | toggle (`bool`), select from `options` (`select`), number (`int`) |
| Effect chip | `Live` / `Restart` / `Deploy-time` |
| Source badge | `env` / `config` / `default` — already computed by `_feature_flags_inventory()` |
| Effective value | what the instance actually uses, not what is stored |

### Three states that must be distinguishable at a glance

1. **Env-pinned** — the control renders locked, badged `env`, labelled with the
   pinning variable. The lock is **not UI-only**: a `POST` touching an
   env-pinned key returns `409` naming the variable. A `disabled` attribute
   alone would let a CLI or script write a value that can never take effect.
2. **Saved, awaiting restart** — the row shows **both** values
   (`stored on · running off`) plus the `Restart` chip. The current panel
   cannot express this state at all, and it is exactly where an operator
   reloads the page in confusion.
3. **Deploy-time** — no control. Effective value plus one sentence naming where
   it is changed. These entries have no config key because there is nothing to
   write: they are what the container was started with, not a section of
   `instance.yaml`.

### Grouping and search

Roughly 35 switches is too many for one flat list, so `category` groups them:
**Product** (~18), **Operations** (~14), **Deploy-time** (3).

A filter box (name / config key / env var) and a **"changed from default"**
toggle. The latter answers the question an operator asks most often when
debugging an instance — "what is different here from the OSS defaults?" —
which today is answered by diffing yaml files.

### Danger gating

`danger=True` rows (TLS verify, rate-limit, egress, self-heal, fake agent) get
a confirmation dialog that names **the consequence**, not just the switch. The
existing `confirm_danger` machinery extends from sections to individual
switches, and the audit entry is flagged high-risk.

### Design-system bindings

Binding standard:
`.claude/skills/agnes-conventions/references/design-system.md`.

| Element | Token / rule |
|---|---|
| Effect chip, source badge | badge language → `--ds-radius-pill` |
| Toggle, select, save button | labelled control → `--ds-radius-btn` (9px), never pill |
| Env-pinned lock | `--ds-accent-info-*` |
| Awaiting restart | `--ds-accent-warn-*` |
| Danger rows | `--ds-accent-danger-*` |
| Category grouping | neutral text plus a hairline |

Two prohibitions, both from rule 4 of the standard: the categories must **not**
use `--ds-kind-*` (that palette denotes entity kinds — Data, Skill, Plugin — and
these are not entities), and no state may use `--ds-primary` (the single brand
action color).

Rule 2 governs everything else: the default `blue` + `topnav` rendering must
carry the new panel correctly, and no existing chrome changes appearance.

## Migration and upgrade parity

An existing instance must behave identically after the upgrade until an
operator changes something. Three places where that is not free.

### 1. Operational switches move to the permissive parser — BREAKING for junk values

The canonical parser treats unrecognized strings as `true`; the inline parsers
treat them as `false`. An instance running `DEBUG=on` today has debug logging
**off** and will have it **on** after migration.

The permissive parser stays: it is the documented convention and seven existing
flags depend on it. The risk is handled by a **boot audit** — at startup, any
switch whose env value is outside the canonical vocabulary
(`0/false/no/off/1/true/yes/on/""`) logs a `WARNING` naming the switch, the raw
value, and what it resolved to. The operator sees this in the first lines of
the log after upgrading rather than inferring it from behavior.

`CHANGELOG.md` gets a `**BREAKING**` bullet scoped to this value class.

### 2. The wire format does not change

`POST /api/admin/server-config` still accepts
`{"sections": {"chat": {"enabled": true}}}`. Existing CLI calls and scripts are
unaffected. The only new responses are `409` (env-pinned) and `400`
(deploy-class) — both for cases that today silently do nothing.

### 3. Switches leave the section forms

Mitigated by the section-header note and anchor. Unpleasant once.

### Documentation filename is kept

`docs/feature-flags.md` is **not** renamed. It is referenced from
`CONTRIBUTING.md`'s sync-map, `CLAUDE.md`, the admin template and several
docstrings; renaming is churn without benefit. The title and content change to
cover all switches.

### Nothing touches the database

Switches live in the yaml overlay. **No repository method, no `_pg.py` sibling,
no Alembic revision, no `src/db.py` ladder step.** Stated explicitly so the
parity reviewer is not woken for nothing.

### Sync-map

The `New user-visible feature flag` row in `CONTRIBUTING.md` is rewritten in
PR1 to point at the new registry.

## Delivery

Four PRs. Each leaves the tree consistent and is independently releasable.

| PR | Content | Visible result |
|---|---|---|
| **1** | `app/switches.py`, `Switch`, `switch_value()`, derived `_EDITABLE_SECTIONS` and `_KNOWN_FIELDS`, covering today's seven flags only | `data_apps` becomes switchable; no new UI |
| **2** | Absorb the bespoke and enum resolvers (theme, ui_layout, home.show_*, store verification, slack transport, distribution, analytics, coordination) | `paper` and `rail` appear in the admin UI — the main payoff |
| **3** | Absorb operational env-only switches, danger gating, categories and filter, boot audit | Full 35-switch inventory |
| **4** | Paper migration of `/admin/server-config` | The page speaks the redesign's visual language |

## PR4 — paper migration of the settings page

### The specificity trap

The page carries **242 lines of page-local CSS** in `{% block head_extra %}`,
which loads **after** the global sheets. This is precisely why
`detail-page.css` prefixes every selector with `html[data-theme="paper"]` and
documents the reason "twice over" — opt-in, and outranking page-local blocks.
`[data-theme="paper"] .cfg-section` would lose to `.cfg-section` from
`head_extra`.

### Where the CSS lives

New sheet **`app/web/static/css/admin-config.css`**, every selector prefixed
`html[data-theme="paper"]`, loaded globally and inert for default instances —
the `detail-page.css` pattern. Not `paper-skin.css`: that sheet holds shape and
typography rules, not a page migration.

**The existing 242-line block is not edited.** It remains the theme-neutral
base; the paper sheet only overrides. This is what makes default parity
structural rather than dependent on care.

### What changes under paper

Vocabulary is the redesign's own, not invented here:

- **Sections stop being boxes** — `.cfg-section` drops its border and fill for
  whitespace and a hairline. This is the main change; today the page is a stack
  of bordered cards.
- **The Switches panel takes the editorial rhythm** — roomy rows, hairline
  separators, no outer frame, a wash on hover.
- **The section navigation** becomes a quiet label column.
- **Buttons need no work** — the page-local `.cfg-btn` rule is already gone and
  all buttons route through the canonical `.btn-*` family, so they inherit
  `--ds-radius-btn`.
- Transitions under 200ms with a `prefers-reduced-motion` opt-out.

### Known consequence

After PR4, `/admin/server-config` is the only admin page in the new visual
language. This is an accepted first step; the remaining `/admin/*` pages should
follow in their own pass, migrated together rather than one at a time — the
lesson #1104 recorded was that per-page drift is the disease.

## Testing

The goal is not coverage but making three bug classes impossible.

### Registry integrity — `tests/test_switches.py`

No duplicate names, env vars or config keys; `select` entries have non-empty
`options` with `default` among them; `deploy` entries carry no config key; env
var names follow the `AGNES_*` convention.

### Derivation guards — the ones that pay for the refactor

- `_EDITABLE_SECTIONS` ⊇ sections of all non-`deploy` switches.
- Every switch-backed `_KNOWN_FIELDS` field's `options` equals the set the
  resolver accepts.
- The table in `docs/feature-flags.md` equals the registry.

### Resolver equivalence — the migration safety net

For every absorbed resolver, a parametrized table of
`(env value, yaml value) -> expected result`, written **before** the refactor
against today's behavior: `ui_layout` typo falls back silently to `topnav`,
`distribution.signed_urls` typo falls back to `auto` with a warning,
`analytics.backend` typo raises `ValueError`. The refactor then cannot smuggle
a difference through.

### API contract — `tests/test_admin_configure_api.py`

Env-pinned key returns `409` naming the variable; deploy-class switch returns
`400`; `danger=True` without `confirm_danger` returns `400`; the legacy wire
format still succeeds.

### UI — `tests/test_ui_layout_theme.py`

The lock renders when the env var is set; the `restart` class shows both stored
and running values; the default instance renders unchanged. PR4 adds a
sheet-scoping test modelled on
`test_trustmark_css_rules_are_scoped_to_theme`: every selector in
`admin-config.css` must start with `html[data-theme="paper"]`.

`tests/test_design_system_contract.py` applies unchanged — no raw hex, no
`var(--primary)`.

### Regression net

`tests/test_feature_flags.py`, `tests/test_instance_config.py` and
`tests/test_instance_config_overlay.py` stay green without modification. If the
refactor breaks them, it broke behavior.

## Appendix — switch inventory

Thirty-five entries, enumerated. PR3 confirms the operational list against the
environment-variable audit; the product and deploy lists are complete.

**Product (18):** `studio`, `guardrails`, `chat`, `chat_approvals`,
`data_apps`, `library_show_unverified_trust`, `mcp_query_param_token`,
`instance.theme`, `instance.ui_layout`, `instance.home_route`,
`instance.home.show_automode`, `instance.home.show_status_frame`,
`store.verification_enabled`, `chat.slack.transport`,
`distribution.signed_urls`, `analytics.backend`, `coordination.backend`,
`data_source.type`.

**Operations (14):** `DEBUG`, `AGNES_AUTH_RATELIMIT_ENABLED`,
`AGNES_DB_SELF_HEAL`, `AGNES_REBUILD_ON_BOOT`, `AGNES_SKIP_CACHE_WARMUP`,
`AGNES_SKIP_LEGACY_COLLECTOR`, `AGNES_BOOTSTRAP_MARKETPLACE`,
`AGNES_INSECURE_SKIP_TLS_VERIFY`, `AGNES_DUCKDB_TZ_STRICT`,
`AGNES_PUSH_NO_GZIP`, `AGNES_NO_UPDATE_CHECK`, `EGRESS_BLOCK_PRIVATE`,
`AGNES_DEBUG_AUTH`, `AGNES_RUNNER_FAKE_AGENT`.

**Deploy-time (3):** `AGNES_ROLE`, `LOCAL_DEV_MODE`, Compose profiles.

### Two entries that are deliberately not switches

- **`AGNES_APPROVALS`** is not an operator switch. It is the environment
  variable the chat manager builds into the sandbox, derived from the
  `chat_approvals` switch. Registering it separately would create a second
  control for one behavior.
- **`LOCAL_DEV_MODE`** is classified `deploy` rather than `operations` on
  purpose. It bypasses authentication (`app/auth/dependencies.py`) by enabling
  a fake development user. If it were writable from the browser, one admin
  click — or one CSRF hole in the admin surface — would leave the instance
  permanently open without a login, and the write would survive a restart. It
  stays visible in the inventory and boot-only in effect.
