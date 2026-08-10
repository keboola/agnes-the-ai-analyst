# Feature flags

Canonical convention for gating a feature on/off in Agnes (#1022). Before this,
gating was heterogeneous — some flags read `instance.yaml` directly, some
consulted an env var only, some duplicated the truthy-string parsing inline.
This doc is the one pattern every new flag follows.

## The convention

**Naming.** A feature that owns a config section uses `<section>.enabled`
(e.g. `chat.enabled`, `guardrails.enabled`, `studio.enabled`,
`data_apps.enabled`). A small or experimental toggle with no section of its
own lives under the reserved `features.<name>` namespace instead of inventing
a top-level key.

**Resolution order**, identical for every flag:

```
env var  >  server-config overlay (DATA_DIR/state/instance.yaml)  >  instance.yaml (static base)  >  default
```

The middle two collapse into one step in code: `config/loader.py` deep-merges
the writable admin overlay over the static `config/instance.yaml` at load
time (see `app/instance_config.py::load_instance_config`), so
`get_value(*keys)` already returns the fully-resolved value. A flag's
resolver is therefore just:

```
env var (if set)  >  get_value(*keys)  >  default
```

**Env var naming**: `AGNES_<SECTION>_ENABLED` (uppercased section name), e.g.
`AGNES_CHAT_ENABLED`, `AGNES_STUDIO_ENABLED`, `AGNES_GUARDRAILS_ENABLED`,
`AGNES_DATA_APPS_ENABLED`. Terraform/infra-friendly — operators can flip a
flag per-deployment without touching the YAML.

**Truthy parsing** is shared across every boolean config/env value in Agnes,
not just feature flags: a Python `bool` passes through unchanged; a string is
false only for `"0"`, `"false"`, `"no"`, `"off"`, or `""` (case-insensitive) —
everything else, including an unrecognized typo, is true. This avoids a
truthy operator intent silently degrading to disabled because of a casing
mismatch.

**Default posture**: **new user-visible features default OFF** (`default=False`).
Two flags are grandfathered on (`studio`, `guardrails`) because they shipped
enabled before this convention existed and flipping them off by default would
be a breaking change for existing instances — don't use them as a precedent
for a new flag's default.

## The helper

`app/instance_config.py::feature_enabled`:

```python
def feature_enabled(*keys: str, env_var: str | None = None, default: bool = False) -> bool:
    ...

feature_enabled("chat", "enabled", env_var="AGNES_CHAT_ENABLED", default=False)
```

Every flag resolver in the codebase (`get_studio_enabled`,
`get_guardrails_enabled`, the `chat.enabled` / `data_apps.enabled` read
sites) delegates to this function — nothing re-derives the truthy-string
rule or the env-over-yaml order by hand.

Two exceptions, by necessity rather than choice: `app.chat.config.load_chat_config`
parses a *caller-supplied* `instance.yaml` path, not the process-global merged
config `get_value()` reads from. Its `chat.enabled` resolution therefore reuses
only the shared truthy-parsing primitive (`app.instance_config.coerce_flag_value`)
rather than calling `feature_enabled` directly — the value *source* differs, the
resolution *order* and *parsing rule* do not.

This has a production consequence, not just a test one: `app/main.py` boots chat
from `load_chat_config(DATA_DIR/state/instance.yaml)` — the writable
server-config **overlay file alone**. A `chat.enabled` set only in the static
`config/instance.yaml` base is invisible to the chat runtime; enable chat via
the `/admin/server-config` editor (which writes the overlay) or the
`AGNES_CHAT_ENABLED` env var. The `/admin/server-config` flag inventory resolves
its `chat` row from the same overlay-only source the runtime uses, so the panel
always reflects what the app actually does.

`chat.approvals_enabled` (the `chat_approvals` flag) is the second exception,
for the same reason and with the same consequences: the chat gate reads it off
`load_chat_config`, so setting it in the static base config alone has no effect
— use the `/admin/server-config` editor or `AGNES_CHAT_APPROVALS_ENABLED`. Both
flags resolve their panel row through `_chat_flag_runtime_view`
(`app/api/admin.py`), fed by `_CHAT_RUNTIME_FLAGS` — a `{flag name: ChatConfig
attribute}` map derived from each switch's `runtime_view` field (see
`Switch.runtime_view` below), not hand-maintained. A third chat-resolved flag
needs only `runtime_view` set on its `Switch` entry — the map, and the panel
row, follow automatically. `switch_value()` refuses to resolve any switch that
declares `runtime_view` (it raises `ValueError` rather than silently reading
the wrong source) — see the caveat under "How to add a switch" below.

## The registry

`app/switches.py::SWITCHES` is a tuple of `Switch` entries, one per
operator-facing toggle. Each entry declares:

- `name`, `config_keys`, `env_var` — identity and where the value can come
  from, in the resolution order above.
- `kind`, `default`, `options` — every switch today is `kind="bool"`;
  `options` is for the `select` switches (`theme`, `ui_layout`, …) landing in
  a later PR.
- `on_invalid` — what a `select` switch does with a token not in `options`:
  `"default"` (the default) falls back to `default` silently, `"raise"`
  fails loudly at read time instead of guessing wrong (use for a switch
  where a bad value is worse than a crash, e.g. a backend selector). Ignored
  for non-`select` kinds.
- `effect` — what the running system can do with a new value: `live` (read
  per request — a save takes effect immediately), `restart` (read at boot —
  a save is stored and applies after a restart), or `deploy` (not a section
  of `instance.yaml` at all; it's what the container was started with, so
  there is nothing to write).
- `category` — the settings-panel display group (`product` / `operations` /
  `locked`), independent of `editable`: a `product` row can still be
  read-only.
- `editable` / `lock_reason` — whether the admin UI offers a write path for
  this switch, and, when it doesn't, the operator-facing reason: nothing to
  write (`effect="deploy"`), a deliberate security lock, or an unmet
  dependency. `editable` has no default — every entry states it explicitly,
  because `POST /api/admin/server-config` validates only the section name
  and then deep-merges the patch, so `editable=True` on one switch exposes
  every key in that switch's section, not just the switch's own key. Every
  switch in the table below is editable except `data_apps`: the flag itself
  is read per request, but the `apps_runner` sidecar it gates sits behind
  the `apps` Compose profile, so flipping it live would surface a feature
  with no backend running.
- `danger` — marks a switch whose flip is high-risk enough to warrant its
  own confirmation copy in the panel — the per-switch analog of the
  section-level `_DANGER_SECTIONS` gate in `app/api/admin.py` (`auth`,
  `server`). Not consumed anywhere yet; reserved for a future per-switch
  confirmation dialog. Every switch today leaves it at the default `False`.
- `runtime_view` — non-`None` for a switch whose *running* value is not read
  from the merged config, per the "Two exceptions" above: the `ChatConfig`
  attribute name holding the resolved flag (`chat` and `chat_approvals` are
  the current two). `switch_value()` raises `ValueError` for any switch that
  sets this rather than risk returning a value the runtime does not
  actually use — see "How to add a switch" below.
- `description` — the operator-facing summary shown in the panel and in the
  table below.

`app.instance_config.FEATURE_FLAGS` is the same tuple under its historical
name (`FeatureFlag` is an alias for `Switch`), so existing imports and the
resolution helper above keep working unchanged.

The registry backs the read-only **Feature flags** panel on
`/admin/server-config` (fed by the `feature_flags` block in
`GET /api/admin/server-config`), so an operator can see every switch's
effective value, where it came from (`env` / `config` / `default`), and
whether they can change it — and why not, when they can't — without
grepping the codebase. `app/api/admin.py::_EDITABLE_SECTIONS` is derived
from the same registry: any config section holding at least one
`editable=True` switch is automatically writable, so shipping a new
editable switch can no longer leave its section rejecting saves — the gap
that shipped `mcp.allow_query_param_token` without a write path.

## How to add a switch

1. Pick a name and config key: does the switch own a config section
   (`<section>.enabled`), or is it small enough for `features.<name>`? Also
   decide its `effect` (`live` / `restart` / `deploy`) and whether it's
   `editable` — most switches are; give the others a `lock_reason`.
2. At the read site, call `switch_value("<name>")` instead of hand-rolling
   `os.environ.get(...)` / `get_value(...)`. **Caveat:** this only works if
   the switch's runtime actually reads the merged config `switch_value()`
   resolves from. If your read site instead boots from its own
   caller-supplied config — as `chat` and `chat_approvals` do, from
   `load_chat_config(DATA_DIR/state/instance.yaml)`, the writable overlay
   file alone — declare `runtime_view` on the `Switch` entry instead (step 3)
   and give the panel its own resolver alongside `_chat_flag_runtime_view`
   in `app/api/admin.py`. `switch_value()` raises `ValueError` for any
   switch that sets `runtime_view`, so this cannot be discovered by a quiet
   wrong answer — only by the exception.
3. Append a `Switch` entry to `SWITCHES` in `app/switches.py` with a short
   operator-facing `description`.
4. Add a row to this doc's flag list below (or update the section it belongs
   to) so operators reading `docs/feature-flags.md` see it without reading
   the registry source.
5. See `CONTRIBUTING.md`'s sync-map — a new user-visible switch is a
   tracked row there too.

## Current flags

| Flag | Config key | Env var | Default | Editable | Notes |
|---|---|---|---|---|---|
| `studio` | `studio.enabled` | `AGNES_STUDIO_ENABLED` | `true` | yes | Grandfathered — shipped enabled before this convention. |
| `guardrails` | `guardrails.enabled` | `AGNES_GUARDRAILS_ENABLED` | `true` | yes | Grandfathered. Env override added in #1022 (new, additive). |
| `chat` | `chat.enabled` | `AGNES_CHAT_ENABLED` | `false` | yes | New feature — off by default. |
| `chat_approvals` | `chat.approvals_enabled` | `AGNES_CHAT_APPROVALS_ENABLED` | `true` | yes | Operator kill-switch for interactive approval prompts. Off denies ask-flagged tool calls instantly instead of waiting for a human. |
| `data_apps` | `data_apps.enabled` | `AGNES_DATA_APPS_ENABLED` | `false` | no — apps_runner sidecar needs the `apps` Compose profile | New feature — off by default. |
| `library_show_unverified_trust` | `library.show_unverified_trust` | `AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST` | `true` | yes | 'Community' trust marker for unverified Store items in the Library, so every row states its provenance (Organization / Verified / Community) and none is left silently unlabelled. On by default: the whole trust vocabulary is gated to the paper theme, so upgrade parity for a default blue instance comes from that gate, not from this flag. Set `false` for the older reading, where an unverified item is marked by the ABSENCE of a marker. |
| `experience` | `instance.experience` | `AGNES_INSTANCE_EXPERIENCE` | `classic` | yes | Select (`classic` \| `redesign`): the one-line redesign adoption preset — see the dedicated section below. Changes only the DEFAULTS of the coupled knobs; any per-knob setting wins; invalid values fall back to `classic`. |
| `stack_auto_membership` | `features.stack_auto_membership` | `AGNES_STACK_AUTO_MEMBERSHIP` | `false` | yes | Stack membership mode. Off (classic, the default): membership is the subscribe model — required plus subscribed grants — with the grant-downgrade subscription fan-out, exactly the pre-redesign behavior. On: auto-membership — every granted resource is in the caller's stack immediately; subscribe/unsubscribe only control the downloaded local copy, and `agnes pull` manifests list granted-but-unsubscribed tables as `server_only`. Default follows the `instance.experience` preset (below). Flipping OFF after running ON: users lose visibility of granted-but-unsubscribed resources until they subscribe (no data loss — subscriptions are interpreted, never rewritten); flipping ON later only widens visibility. |
| `mcp_query_param_token` | `mcp.allow_query_param_token` | `AGNES_MCP_ALLOW_QUERY_PARAM_TOKEN` | `true` | yes | Grandfathered on. Accepts the MCP bearer token as `?token=` on SSE GET for header-incapable clients; the token then appears in every request log (CWE-598). Turn off when all MCP clients send the `Authorization` header. **Unlike the other flags here its default is the permissive state, so an unrecognized value — `disabled`, `n` — silently leaves it on; use `false`/`off`/`no`/`0` and verify via `effective`.** |
| `mcp_source_url_strict` | `mcp.source_url_strict` | `AGNES_MCP_SOURCE_URL_STRICT` | `false` | yes | Holds a registered MCP source's own url to the same bar as its OAuth endpoints (https, public address). Off by default, which is **not** unguarded: the baseline always refuses link-local / metadata / multicast / reserved addresses and cleartext http to a public one. The default only permits a source on an *internal* address — an organization's own tool server, a developer's localhost — because those are ordinary deployments. Turn on for instances that talk only to third-party MCP services; it makes an intranet source unconfigurable, hence opt-in. |
| `agent_profiles` | `agent_profiles.enabled` | `AGNES_AGENT_PROFILES_ENABLED` | `true` | no — deliberately env-var-only kill switch (owner decision on #1186) | Grandfathered — shipped enabled before this flag existed. Gates the `/agents` builder and the `/api/v1/agents*` management + runtime API (and its CLI clients, `agnes agent`/`agnes chat`). Does not gate default-agent seeding, chat attribution, or the broker's agent policy — those are internal mechanisms, not HTTP surface. Set the env var, or hand-edit the static `instance.yaml`, and restart. |

## The `instance.experience` preset

One line flips the DEFAULTS of every experience-coupled knob (spec
`docs/superpowers/specs/2026-08-07-default-chrome-ux-parity.md`):

| | |
|---|---|
| Config key | `instance.experience` |
| Values | `classic` (default) \| `redesign` |
| Env | `AGNES_INSTANCE_EXPERIENCE` |

`redesign` defaults `instance.ui_layout` to `rail`, `instance.theme` to
`paper` and `features.stack_auto_membership` to `true`; `classic` (or an
absent/invalid key) is byte-for-byte the pre-redesign experience. The preset
changes only defaults — any per-knob env/yaml setting still wins, and
per-knob precedence is unchanged (`env(knob) > yaml(knob) > preset-implied
default > built-in default`). Deliberately NOT coupled:
`store.verification_enabled` (a governance opt-in — it needs a reviewer, not
a theme) and `library.show_unverified_trust` (the trust vocabulary is
already gated to the paper theme itself). The `/admin/server-config` flag
inventory leads with the preset's resolved value and labels preset-sourced
flag defaults with a `preset` badge.
