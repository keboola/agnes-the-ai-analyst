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
(`app/api/admin.py`), which is keyed off a `{flag name: ChatConfig attribute}`
map — a third chat-resolved flag belongs in that map, or the panel will report a
value the runtime does not use.

## The registry

`app/instance_config.py::FEATURE_FLAGS` is a tuple of `FeatureFlag` entries
— `name`, `config_keys`, `env_var`, `default`, `description` — one per flag
resolved through `feature_enabled`. It backs the read-only **Feature flags**
panel on `/admin/server-config` (fed by the `feature_flags` block in
`GET /api/admin/server-config`), so an operator can see every flag's
effective value and where it came from (`env` / `config` / `default`)
without grepping the codebase.

## How to add a flag

1. Pick a name and decide: does the feature own a config section
   (`<section>.enabled`), or is it small enough for `features.<name>`?
2. At the read site, call `feature_enabled(*keys, env_var="AGNES_<SECTION>_ENABLED", default=...)`
   instead of hand-rolling `os.environ.get(...)` / `get_value(...)`.
3. Append an entry to `FEATURE_FLAGS` in `app/instance_config.py` with a short
   operator-facing `description`.
4. Add a row to this doc's flag list below (or update the section it belongs
   to) so operators reading `docs/feature-flags.md` see it without reading
   the registry source.
5. See `CONTRIBUTING.md`'s sync-map — a new user-visible feature flag is a
   tracked row there too.

## Current flags

| Flag | Config key | Env var | Default | Notes |
|---|---|---|---|---|
| `studio` | `studio.enabled` | `AGNES_STUDIO_ENABLED` | `true` | Grandfathered — shipped enabled before this convention. |
| `guardrails` | `guardrails.enabled` | `AGNES_GUARDRAILS_ENABLED` | `true` | Grandfathered. Env override added in #1022 (new, additive). |
| `chat` | `chat.enabled` | `AGNES_CHAT_ENABLED` | `false` | New feature — off by default. |
| `chat_approvals` | `chat.approvals_enabled` | `AGNES_CHAT_APPROVALS_ENABLED` | `true` | Operator kill-switch for interactive approval prompts. Off denies ask-flagged tool calls instantly instead of waiting for a human. |
| `data_apps` | `data_apps.enabled` | `AGNES_DATA_APPS_ENABLED` | `false` | New feature — off by default. |
| `library_show_unverified_trust` | `library.show_unverified_trust` | `AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST` | `false` | 'Community' trust marker for unverified Store items in the Library. Off by default so an upgrade never changes how existing rows read; set `true` to state every row's provenance positively (Organization / Verified / Community). |
| `mcp_query_param_token` | `mcp.allow_query_param_token` | `AGNES_MCP_ALLOW_QUERY_PARAM_TOKEN` | `true` | Grandfathered on. Accepts the MCP bearer token as `?token=` on SSE GET for header-incapable clients; the token then appears in every request log (CWE-598). Turn off when all MCP clients send the `Authorization` header. **Unlike the other flags here its default is the permissive state, so an unrecognized value — `disabled`, `n` — silently leaves it on; use `false`/`off`/`no`/`0` and verify via `effective`.** |
