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

- **Switch metadata lives in a fourth place: a dict inside a test file.**
  `tests/test_admin_configure_api.py::_NOT_LIVE_WRITABLE` holds the reason each
  registered flag is *not* writable — for `data_apps`, that the flag is read
  per request but the `apps_runner` sidecar sits behind the `apps` Compose
  profile, so a live flip can surface a feature whose backend is absent. That
  rationale is correct and the ratchet around it is sound (it derives from the
  registry, not from prose, and `test_no_stale_exemption` keeps it
  shrinks-only). The problem is location: an operator who tries to enable data
  apps and cannot sees no reason anywhere in the product, because the
  explanation is in a test. Metadata about a switch belongs on the switch.
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
- Every switch that can be changed at runtime **and should be**, changeable
  from the admin UI. Visibility is universal; editability is not — the
  security posture stays out of the browser.
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
| Scope | **Everything except the security posture** | Operational switches become yaml-backed and admin-editable behind a danger gate; switches that define the instance's security posture stay env-only and read-only (see *Security-locked switches*) |
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
    category: str                # "product" | "operations" | "locked"
    description: str             # operator-facing, one or two sentences
    options: tuple[str, ...] = ()      # select only; must contain `default`
    danger: bool = False               # confirm dialog + high-risk audit
    editable: bool = True              # False → inventory-only, never writable
    lock_reason: str = ""              # required when editable=False
    on_invalid: str = "default"        # "default" | "raise"
    runtime_view: str | None = None    # overlay-only readers (chat)
```

`editable` and `effect` are deliberately orthogonal. `effect` states what the
system *can* do with a new value; `editable` states whether we *let* anyone set
one. Two different reasons produce the same read-only row:

- **Nothing to write** — `effect="deploy"` entries (`AGNES_ROLE`, Compose
  profiles) are not sections of `instance.yaml`; they are what the container
  was started with. `editable=False` follows by construction.
- **Deliberately locked** — the security-posture switches below. They *could*
  be written; we choose not to offer it.
- **Unmet dependency** — `data_apps`, whose flag is genuinely read per request
  but whose `apps_runner` sidecar sits behind the `apps` Compose profile, so a
  live flip can surface a feature with no backend. This reason exists today in
  `tests/test_admin_configure_api.py::_NOT_LIVE_WRITABLE`; PR1 moves it, and
  every other entry in that dict, onto `lock_reason` so the product can state
  it where the operator is standing.

`editable` is independent of `category`: a **Product** row can be read-only.
`data_apps` stays under Product, where an operator looks for it, and renders
with its reason instead of a control.

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
| `_EDITABLE_SECTIONS` | sections of all `editable=True` switches | flag registered but section not editable (historically `mcp`, then `chat`) — today caught by a ratchet whose exemption reasons live in a test dict; this moves both the rule and the reason onto the entry |
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

### Security-locked switches

A switch that defines the instance's **security posture** is `editable=False`.
It appears in the inventory with its effective value and a sentence naming
where it is set — an operator can always *see* it — but there is no control,
and the API refuses to write it.

The reasoning is not that an admin cannot be trusted. It is that these switches
have no legitimate runtime-change use case, while a browser-reachable control
for them turns any single admin mistake, session compromise, or hole in the
admin surface into a durable downgrade of the instance's defenses that survives
a restart.

| Switch | What it controls | Why locked |
|---|---|---|
| `LOCAL_DEV_MODE` | enables a fake development user, bypassing authentication | one click leaves the instance permanently open without a login |
| `AGNES_DEBUG_AUTH` | unlocks `/api/me/debug`, an auth-internals diagnostic | the route answers `404` rather than `403` **on purpose**, so its existence is undetectable in production; a UI control defeats that design |
| `DEBUG` | unlocks `/api/debug/throw` (on-demand exception injection) and the Postgres query panel that logs SQL | reachable fault injection plus query disclosure |
| `AGNES_AUTH_RATELIMIT_ENABLED` | brute-force protection on login | read per request, so switching it off takes effect instantly — which makes it more dangerous to expose, not less |
| `AGNES_INSECURE_SKIP_TLS_VERIFY` | certificate verification on outbound calls | disabling verification is never a runtime decision |
| `EGRESS_BLOCK_PRIVATE` | the egress proxy's private-range block (SSRF defense) | also boot-only in fact: read once in the proxy's `main()`, in a separate process the app cannot restart |

**These six keep their current resolution untouched.** No config key is added,
no parser is migrated, no yaml path is introduced — they stay env-only exactly
as they are today. The registry only *describes* them, and the panel reads
their value through the resolver each already has. This removes them from the
breaking-change class in Migration as a side effect, including the sharpest
example (`DEBUG=on`).

`AGNES_RUNNER_FAKE_AGENT` is deliberately **not** in this table. It makes the
product lie about what it is doing, which is a correctness problem rather than
a security-posture one, so it stays editable behind the danger gate. Moving it
here is a one-line change if that judgement is wrong.

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
3. **Read-only** (`editable=False`) — no control. Effective value, plus one
   sentence naming where it is set and `lock_reason` explaining why it is not
   offered here. The row looks the same whether the reason is "nothing to
   write" (`AGNES_ROLE`) or "deliberately locked" (`DEBUG`); the sentence is
   what distinguishes them.

### Grouping and search

Thirty-five switches is too many for one flat list, so `category` groups them:
**Product** (18), **Operations** (9), **Locked** (8 — five security-posture
switches plus `AGNES_ROLE`, `LOCAL_DEV_MODE` and Compose profiles).

A filter box (name / config key / env var) and a **"changed from default"**
toggle. The latter answers the question an operator asks most often when
debugging an instance — "what is different here from the OSS defaults?" —
which today is answered by diffing yaml files.

### Danger gating

With the security posture locked, the danger gate covers what is left that can
still hurt: `AGNES_DB_SELF_HEAL`, `AGNES_RUNNER_FAKE_AGENT` and
`AGNES_BOOTSTRAP_MARKETPLACE`. Those rows get a confirmation dialog that names
**the consequence**, not just the switch. The existing `confirm_danger`
machinery extends from sections to individual switches, and the audit entry is
flagged high-risk.

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
treat them as `false`. An instance running `AGNES_REBUILD_ON_BOOT=yes` today
resolves it **off** (the inline check accepts only `1` and `true`) and will
resolve it **on** after migration.

The blast radius is limited to the nine editable operational switches. The six
security-locked ones keep their existing resolvers untouched, which removes the
sharpest case (`DEBUG=on`) from this class entirely.

The permissive parser stays: it is the documented convention and seven existing
flags depend on it. The residual risk is handled by a **boot audit** — at
startup, any switch whose env value is outside the canonical vocabulary
(`0/false/no/off/1/true/yes/on/""`) logs a `WARNING` naming the switch, the raw
value, and what it resolved to. The operator sees this in the first lines of
the log after upgrading rather than inferring it from behavior.

`CHANGELOG.md` gets a `**BREAKING**` bullet scoped to this value class.

### 2. The wire format does not change

`POST /api/admin/server-config` still accepts
`{"sections": {"chat": {"enabled": true}}}`. Existing CLI calls and scripts are
unaffected. The only new responses are `409` (env-pinned) and `400
switch_not_editable` with a reason code distinguishing `not_in_config` from
`security_locked` — all for cases that today either silently do nothing or,
for the locked ones, were never reachable through this endpoint at all.

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
| **1** | `app/switches.py`, `Switch`, `switch_value()`, derived `_EDITABLE_SECTIONS` and `_KNOWN_FIELDS`, covering today's seven flags only; `_NOT_LIVE_WRITABLE` folds into `lock_reason` | every flag states in the product why it is or is not editable; no new UI |
| **2** | Absorb the bespoke and enum resolvers (theme, ui_layout, home.show_*, store verification, slack transport, distribution, analytics, coordination) | `paper` and `rail` appear in the admin UI — the main payoff |
| **3** | Absorb operational env-only switches, classify the security-locked six as inventory-only, danger gating, categories and filter, boot audit | Full 35-switch inventory |
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
`options` with `default` among them; `deploy` entries carry no config key;
`editable=False` entries carry a non-empty `lock_reason`; env var names follow
the `AGNES_*` convention.

### Derivation guards — the ones that pay for the refactor

- `_EDITABLE_SECTIONS` ⊇ sections of all `editable=True` switches.
- No `editable=False` switch contributes a section to `_EDITABLE_SECTIONS`, and
  the six security-locked entries declare no config key at all — so there is no
  yaml path by which they could be set. This is the guard that keeps the lock
  from eroding one convenience commit at a time.
- The existing ratchet in `tests/test_admin_configure_api.py` keeps working
  against the new source: `_registry_sections()` reads `SWITCHES`, and
  `_NOT_LIVE_WRITABLE` is replaced by `editable=False` plus `lock_reason`.
  `test_no_stale_exemption` survives as "a switch that became editable must
  clear its `lock_reason`", so the shrinks-only property is preserved rather
  than reinvented.
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

Env-pinned key returns `409` naming the variable; an `editable=False` switch
returns `400 switch_not_editable` with the right reason code; `danger=True`
without `confirm_danger` returns `400`; the legacy wire format still succeeds.
One case per security-locked switch, so a future "just make this one
editable" change has to delete a named test rather than slip through.

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
environment-variable audit; the product and locked lists are complete.

**Product (18):** `studio`, `guardrails`, `chat`, `chat_approvals`,
`data_apps` (`editable=False` — unmet sidecar dependency, see above),
`library_show_unverified_trust`, `mcp_query_param_token`,
`instance.theme`, `instance.ui_layout`, `instance.home_route`,
`instance.home.show_automode`, `instance.home.show_status_frame`,
`store.verification_enabled`, `chat.slack.transport`,
`distribution.signed_urls`, `analytics.backend`, `coordination.backend`,
`data_source.type`.

**Operations (9), editable behind the danger gate where marked:**
`AGNES_DB_SELF_HEAL` (danger), `AGNES_RUNNER_FAKE_AGENT` (danger),
`AGNES_BOOTSTRAP_MARKETPLACE` (danger), `AGNES_REBUILD_ON_BOOT`,
`AGNES_SKIP_CACHE_WARMUP`, `AGNES_SKIP_LEGACY_COLLECTOR`,
`AGNES_DUCKDB_TZ_STRICT`, `AGNES_PUSH_NO_GZIP`, `AGNES_NO_UPDATE_CHECK`.

**Locked (8), inventory-only:**

- *Security posture* — `LOCAL_DEV_MODE`, `AGNES_DEBUG_AUTH`, `DEBUG`,
  `AGNES_AUTH_RATELIMIT_ENABLED`, `AGNES_INSECURE_SKIP_TLS_VERIFY`,
  `EGRESS_BLOCK_PRIVATE`. Resolution untouched, env-only, no config key.
  See *Security-locked switches*.
- *Nothing to write* — `AGNES_ROLE`, Compose profiles.

### One entry that is deliberately not a switch

**`AGNES_APPROVALS`** is not an operator switch. It is the environment variable
the chat manager builds into the sandbox, derived from the `chat_approvals`
switch. Registering it separately would create a second control for one
behavior — the exact disease this design treats.
