# Default-chrome UX parity — legacy surfaces and the stack-membership mode

**Date:** 2026-08-07
**Status:** wave 1 in progress; wave 2 planned
**Follows:** the #896/#1104 paper-theme + rail-layout redesign, the topnav
content-parity work that shipped with it, and the detail-page parity follow-up
(#1195).

## Problem

The redesign shipped with an explicit contract: *existing instances see zero
change without opting in*. The chrome honored it (topnav/blue is untouched),
and the content-parity work extended it to the list pages, chat additions and
detail pages. Three groups of changes still reach a **default-chrome instance**
on a routine upgrade:

1. **Stack membership semantics** (`feat(stack)!`): membership became a pure
   function of RBAC grants (auto-membership). On a default instance this
   changes what the catalog's Browse tab lists, what "Add to stack" means
   (relabeled to local-download), and what `agnes pull` manifests contain —
   the most user-visible behavioral delta of the whole redesign, and the one
   operators of established deployments report as "filtering and
   add-to-stack changed".
2. **Page rewrites that were never layout-gated**: `/me/profile`,
   `/me/activity`, the `/agents` builder, the `/me/ai-connector` page (now a
   redirect), and the chat welcome cards (copy + icon glyphs).
3. **Removed affordances**: the guided tour (deleted; its replacement is
   rail-only) and the "AI Connector" menu label.

This spec defines how every remaining delta is either restored behind the
established parity machinery or explicitly accepted — **without changing the
redesign experience for opted-in instances**.

## Principles (established, reused)

- **Two worlds, one switch.** The redesign opt-in condition is
  `ui_layout == "rail" OR theme == "paper"` — the same condition the base
  templates key their chrome on and `_detail_template()` keys the detail
  pages on. Default-chrome surfaces render **frozen pre-redesign copies**;
  opted-in surfaces render the redesign, unchanged.
- **Frozen copies, closed sets.** A legacy surface is a byte-for-byte copy of
  the pre-redesign template (source: the last pre-redesign mainline commit,
  `e01073be2^`), named `*_legacy.html`, exempt from cosmetic sweeps (emoji
  ban) through a **closed, guarded set** (`LEGACY_FROZEN`) so the exemption
  cannot be dodged by naming. Legacy copies retire together with the topnav
  chrome.
- **Handlers stay shared.** A handler serves both worlds when its context is
  a superset of what the legacy template needs (verified against the
  pre-redesign handler, not against the current tree). Value-level
  regressions (e.g. a badge no longer computed) are restored **scoped to the
  legacy path** so retired UI cannot resurface on the redesign.
- **Both directions pinned.** Every parity surface has tests asserting the
  legacy markers on default AND the redesign markers under the opt-in
  (`TestDefaultContentParity`, `TestDetailPageParity`, and per-surface
  siblings). Redesign-anatomy tests opt in explicitly via env fixtures.
- **Semantics fork behind an instance flag, not a chrome check.** Server-side
  behavior (stack membership) must not read UI configuration: it gets its own
  feature flag, resolved through the standard `feature_enabled` convention.

## One-line adoption: the `experience` preset

The parity machinery necessarily spans several independent knobs (chrome
layout, theme, stack semantics, trust vocabulary) — they are genuinely
orthogonal, and forcing them into one boolean would take real capability
away (paper-without-rail is supported; the verification workflow is a
governance choice, not a visual one). But *adoption* must be one line:

| | |
|---|---|
| Config key | `instance.experience` |
| Values | `classic` (default) \| `redesign` |
| Env | `AGNES_INSTANCE_EXPERIENCE` |

The preset changes only the **defaults** the individual resolvers fall back
to; any per-knob setting (env or yaml) still wins. Precedence per knob stays
exactly as today — `env(knob) > yaml(knob) > preset-implied default >
built-in default`:

| Knob | `classic` default | `redesign` default |
|---|---|---|
| `instance.ui_layout` | `topnav` | `rail` |
| `instance.theme` | `blue` | `paper` |
| `features.stack_auto_membership` | `false` | `true` |
| `library.show_unverified_trust` | not preset-coupled — the trust vocabulary is gated to the paper theme at every `mark()` callsite, so default-chrome parity comes from the theme gate itself; the flag keeps its registry default either way | |
| `store.verification_enabled` | `false` | `false` — stays a deliberate governance opt-in (it needs a reviewer, not a theme) |

`experience: redesign` is therefore the one-line switch to the full new
world; `classic` (or an absent key) is byte-for-byte the pre-redesign
experience. The preset appears in the `/admin/server-config` flag inventory
with its resolved value and source, and the per-knob rows show when a value
came from the preset.

## Wave 1 — stack membership mode

### Flag

| | |
|---|---|
| Name | `stack_auto_membership` |
| Config key | `features.stack_auto_membership` |
| Env | `AGNES_STACK_AUTO_MEMBERSHIP` |
| Default | **`false`** — the classic subscribe model (upgrade parity) |
| Registry | `FEATURE_FLAGS` entry (shows in `/admin/server-config` inventory) |

Instances that adopted the redesign enable it together with the chrome
opt-ins; the three settings are documented as a trio in
`config/instance.yaml.example`.

### Semantics table

| Concern | Classic (default) | Auto-membership (`true`) |
|---|---|---|
| `StackResolver.stack()` | `required ∪ (subscribed ∩ available)`; admin god-mode additionally surfaces raw subscriptions | `required ∪ available ∪ (admin raw subs)`; `materialized` flags local copies |
| `StackResolver.browse()` | all granted entries; `in_stack = id ∈ required ∪ subscribed` | all granted entries; `in_stack = True`, `materialized` drives Download/Remove |
| `POST /api/stack/subscribe` | joins the stack (membership op) | requests a local copy (materialization op) |
| `agnes pull` manifest | stack members only (all local) | every granted resource; unmaterialized rows marked `server_only` |
| Grant downgrade `required → available` | **eager subscription fan-out** to group members (they must not lose access) | no fan-out needed (membership follows the grant) |
| Catalog Browse (default chrome) | lists granted resources with add-to-stack state | only genuinely addable entries; members live in My Stack |
| Admin view in user-facing catalog | god-mode listing restored (classic behavior) | grant-scoped like everyone; audit lives at `/admin/data-packages` |

The classic formulas are taken verbatim from the pre-redesign resolver, not
re-derived. MCP `stack_*` tool docstrings describe both modes, leading with
the default.

### Touched components

- `app/instance_config.py` — flag resolver + registry entry.
- `app/services/stack_resolver.py` — mode switch in `stack()` / `browse()`
  (single read point for the flag; both formula sets live side by side).
- `app/api/stack.py` — response copy/`next_step` hints per mode.
- `app/api/sync.py` — manifest content falls out of `stack()`; verify
  `server_only` rows appear only in auto mode.
- `app/api/access.py` — downgrade fan-out for `data_package` /
  `memory_domain` conditional on the flag (classic: restore the v49
  behavior; auto: keep the no-fan-out contract).
- `app/web/templates/catalog.html`, `corporate-memory.html` — restored to
  pre-redesign content (only the default chrome renders them; rail uses
  `catalog_unified.html`). Their handlers re-verified against the
  pre-redesign handlers for context parity; god-mode listing restored on the
  classic path.
- `docs/feature-flags.md`, `docs/RBAC.md` (stack section),
  `config/instance.yaml.example`, `CHANGELOG.md` (**BREAKING-revert** bullet:
  the 0.82.0 breaking change becomes opt-in).

### Compatibility notes

- The flag flips behavior instantly; no schema or data change.
  `user_stack_subscriptions` rows are interpreted, never rewritten.
- Flipping **off after running on**: users lose visibility of granted-but-
  unsubscribed resources until they subscribe (or the operator runs the
  fan-out). Documented in the flag description; not a data loss.
- Flipping **on later** widens visibility only (no access loss).

### Tests

- New dual-mode contract file (`tests/test_stack_membership_modes.py`):
  the semantics table above, asserted per mode via the env var, on both
  backends where repos are involved.
- Existing auto-membership suites (`test_web_stack_auto_membership`,
  catalog reshape, CLI no-fan-out contract, stack API tests) gain an
  explicit opt-in fixture — they pin the auto mode, which is no longer the
  default.
- Classic-mode additions: downgrade fan-out restores access; manifest
  excludes unsubscribed availables; catalog Browse lists granted rows with
  add-to-stack affordance; admin god-mode listing present.

## Wave 2 — remaining default-chrome surfaces

Each item follows the frozen-copy pattern unless noted. Legacy source is
`e01073be2^` throughout.

| # | Surface | Mechanism | Notes / risks |
|---|---|---|---|
| 1 | `/me/profile` | `profile_legacy.html` + layout switch | The pre-redesign page already contains the admin-elevation panel (#1146). Handler ctx verified superset. |
| 2 | `/me/activity` | `me_activity_legacy.html` + switch | Straightforward. |
| 3 | `/agents` builder | `agents_legacy.html` (+ pre-redesign handler branch if ctx diverged) | The pre-redesign builder existed briefly on mainline; classic instances keep it. Redesign builder untouched under the opt-in. |
| 4 | `/me/ai-connector` | Serve `me_cowork_legacy.html` on default chrome instead of the 302; keep the redirect under the opt-in | Restores the page's `/mcp-connect` token-fallback link on default (nav-contract guard updates: the link now has one home per world — `me_cowork_legacy` on default, `/how-it-works` under the opt-in). |
| 5 | Topnav user menu | "AI Connector" label restored on default chrome; "Learn how it works" remains the opt-in wording | `_app_header.html` conditional on the same opt-in expression; "My agents" and "News" rows predate the redesign and stay in both worlds. |
| 6 | Guided tour | `_tour_legacy.html` + `js/tour_legacy.js` (frozen) + the header "?" launcher, default chrome only | The redesign rewrote `tour.js` into the coach-mark engine, so the legacy tour ships its own frozen script. Rail keeps the new tour. Frozen JS joins a guarded closed set analogous to `LEGACY_FROZEN`. |
| 7 | Chat welcome cards | `_chat_welcome_cards_legacy.html` frozen partial included on default chrome; the rail hero untouched | Restores pre-redesign copy and icons byte-for-byte; being a frozen copy, it is exempt from the emoji ban without weakening it for living templates. |
| 8 | Admin "Moderation & Trust" menu entry | Gate on `store_verification_enabled()` | Semantic gate, not a chrome check: with verification disabled (the default) the hub has nothing to moderate. |

### Explicitly accepted deviations (documented, not gated)

- Toast restyle (shared component; rare surface, cosmetic).
- Metric + glossary sections in global search (additive, subtle).
- The four additive admin-hub links (admin-only navigation).
- The version badge and other diagnostic chrome.

Each is called out in the release notes; if any proves disruptive it can be
promoted into the frozen-copy pattern later without redesign changes.

## Verification

- Full parity guard battery (`TestDefaultContentParity`,
  `TestDetailPageParity`, per-surface classes, `LEGACY_FROZEN` closed-set
  guards, emoji ratchet, design-system contract) on every PR.
- Dual-mode stack contract suite (wave 1) on both DB backends.
- Live check on a clone of a real default-chrome instance (the method from
  the redesign screenshot audit): default renders the pre-redesign pages and
  classic stack behavior; an opted-in instance renders the redesign
  unchanged.

## Rollout

1. Wave 1 lands first (semantics are the reported pain); release-cut is a
   **BREAKING-revert** minor: the 0.82.0 auto-membership default reverts to
   classic, auto becomes opt-in.
2. Wave 2 lands as one PR (surfaces are independent; the pattern is
   mechanical).
3. Redesign instances set **one line** — `instance.experience: redesign` —
   which implies the chrome, the stack mode and the trust marker defaults
   (per-knob overrides remain available; `store.verification_enabled` stays
   a separate governance opt-in). Documented in `instance.yaml.example`.
4. Legacy copies and the legacy tour retire together with the topnav chrome,
   as one deliberate removal with its own migration note.
