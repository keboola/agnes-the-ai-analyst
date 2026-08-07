"""Unified switch registry — every operator-facing toggle in one place.

Before this module, gating lived in six unrelated mechanisms: the
`FEATURE_FLAGS` registry, hand-copied boolean resolvers, enum resolvers with
their own typo behavior, env-only switches with inline truthy parsing,
deployment-time selection, and the admin field metadata in
`app/api/admin.py`. A switch's editability was decided in one file and
justified in another — a test.

Everything a switch needs is declared here. `_EDITABLE_SECTIONS`, the admin
field metadata, the settings panel and the operator documentation all derive
from this tuple; none of them restates it.

Import direction is one-way: this module must not import
`app.instance_config` at module level — that module imports this one. The
local imports inside `switch_value` are deliberate and have precedent in
`src/analytics_backend.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Effect classes — what the system can do with a new value.
#:   live    — read per request; a save takes effect immediately.
#:   restart — read at boot; a save is stored and applies after a restart.
#:   deploy  — not a section of instance.yaml at all; it is what the
#:             container was started with. Nothing to write.
EFFECTS = ("live", "restart", "deploy")

#: Display groups in the settings panel. Independent of `editable`: a
#: `product` row can still be read-only.
CATEGORIES = ("product", "operations", "locked")

#: Kinds `switch_value` knows how to resolve: `bool` coerces through the
#: shared truthy-parsing rule, `int` parses (falling back to `default` on a
#: bad value), `select` validates against `options` (per `on_invalid`).
#: Anything else falls through the bottom of that dispatch and comes back as
#: a lowercased string with no coercion at all — the trap that lets a
#: typo'd `kind="boolean"` return the truthy string `"false"` instead of a
#: coerced `False`. `test_switches.py` asserts every entry's `kind` is one of
#: these.
KINDS = ("bool", "int", "select")

#: What `switch_value` does with a `select` switch's unrecognized value:
#: `default` falls back silently (the common case), `raise` fails loudly at
#: read time (use for a switch where guessing wrong is worse than crashing,
#: e.g. a backend selector). `test_switches.py` asserts every entry's
#: `on_invalid` is one of these.
ON_INVALID = ("default", "raise")


@dataclass(frozen=True)
class Switch:
    """One operator-facing toggle.

    `effect` and `editable` are deliberately orthogonal: `effect` states what
    the system *can* do with a new value, `editable` whether we *offer* one.
    A switch is locked for one of three reasons — nothing to write
    (`effect="deploy"`), a deliberate security lock, or an unmet dependency —
    and `lock_reason` is what the product shows the operator in each case.

    `editable` has no default and every entry must state it explicitly.
    `POST /api/admin/server-config` validates only the SECTION name and then
    deep-merges the patch, so `editable=True` on one switch makes the whole
    section — every key in it, not just this switch's — admin-writable. A
    class default would make that a silent side effect of adding a `Switch`
    rather than a decision stated at each call site.
    """

    name: str
    config_keys: tuple[str, ...]
    env_var: str
    kind: str
    default: Any
    effect: str
    category: str
    description: str
    editable: bool
    options: tuple[str, ...] = ()
    danger: bool = False
    lock_reason: str = ""
    on_invalid: str = "default"
    #: Non-None for a switch whose *running* value is NOT read from the
    #: merged config `switch_value()` resolves from. `chat` and
    #: `chat_approvals` are the current examples: `app/main.py` boots them
    #: via `load_chat_config(DATA_DIR/state/instance.yaml)` — the writable
    #: server-config overlay file alone, never the static `config/
    #: instance.yaml` base `switch_value()` would also consult. The value
    #: here is the attribute name on `ChatConfig` holding the resolved flag
    #: (e.g. `"enabled"`, `"approvals_enabled"`).
    #:
    #: A switch that sets this must never be read through `switch_value()` —
    #: doing so would silently answer from the wrong source (it could return
    #: True for an instance that only set the flag in the static base, while
    #: the runtime it actually gates has it off). `switch_value()` raises
    #: rather than risk that; read the switch through its own runtime path
    #: instead (`app/api/admin.py::_chat_flag_runtime_view` for the two
    #: chat flags today).
    runtime_view: str | None = None


SWITCHES: tuple[Switch, ...] = (
    Switch(
        name="studio",
        config_keys=("studio", "enabled"),
        env_var="AGNES_STUDIO_ENABLED",
        kind="bool",
        default=True,
        effect="live",
        category="product",
        editable=True,
        description="Authoring Studio surface (/admin/studio*). Grandfathered on by default.",
    ),
    Switch(
        name="guardrails",
        config_keys=("guardrails", "enabled"),
        env_var="AGNES_GUARDRAILS_ENABLED",
        kind="bool",
        default=True,
        effect="live",
        category="product",
        editable=True,
        description="Flea-market upload LLM security-review pipeline. Grandfathered on by default.",
    ),
    Switch(
        name="chat_approvals",
        config_keys=("chat", "approvals_enabled"),
        env_var="AGNES_CHAT_APPROVALS_ENABLED",
        kind="bool",
        default=True,
        effect="restart",
        category="product",
        editable=True,
        runtime_view="approvals_enabled",
        description=(
            "Interactive approval prompts for ask-flagged chat tool calls. Off makes the "
            "sandbox gate deny instantly instead of waiting for a human."
        ),
    ),
    Switch(
        name="chat",
        config_keys=("chat", "enabled"),
        env_var="AGNES_CHAT_ENABLED",
        kind="bool",
        default=False,
        effect="restart",
        category="product",
        editable=True,
        runtime_view="enabled",
        description="Cloud-hosted chat (E2B sandbox agent sessions). New feature — off by default.",
    ),
    Switch(
        name="data_apps",
        config_keys=("data_apps", "enabled"),
        env_var="AGNES_DATA_APPS_ENABLED",
        kind="bool",
        default=False,
        effect="live",
        category="product",
        editable=False,
        lock_reason=(
            "The flag itself is read per request, but the apps_runner sidecar sits behind "
            "the `apps` Compose profile — enabling it here would surface a feature whose "
            "backend is absent. Enable the profile and set AGNES_DATA_APPS_ENABLED together."
        ),
        description="Hosted user web apps (data apps). New feature — off by default.",
    ),
    Switch(
        name="library_show_unverified_trust",
        config_keys=("library", "show_unverified_trust"),
        env_var="AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST",
        kind="bool",
        default=True,
        effect="live",
        category="product",
        editable=True,
        description=(
            "Show the 'Community' trust marker for unverified Store items in the Library, so "
            "all three provenance levels (Organization / Verified / Community) are stated "
            "positively and no row is left silently unlabelled. Set false for the older silent "
            "reading, where an unverified item is marked by the ABSENCE of a marker."
        ),
    ),
    Switch(
        name="mcp_query_param_token",
        config_keys=("mcp", "allow_query_param_token"),
        env_var="AGNES_MCP_ALLOW_QUERY_PARAM_TOKEN",
        kind="bool",
        default=True,
        effect="live",
        category="product",
        editable=True,
        description=(
            "Accept the MCP bearer token as a ?token= query param on SSE GET, for clients "
            "that cannot set headers. On by default (grandfathered). The token lands in every "
            "request log when used (CWE-598) — turn this off if all your MCP clients send the "
            "Authorization header."
        ),
    ),
    Switch(
        name="agent_profiles",
        config_keys=("agent_profiles", "enabled"),
        env_var="AGNES_AGENT_PROFILES_ENABLED",
        kind="bool",
        default=True,
        effect="restart",
        category="product",
        editable=False,
        lock_reason=(
            "Deliberately env-var-only kill switch — no runtime-toggle use case identified. "
            "Flip via AGNES_AGENT_PROFILES_ENABLED (or the static instance.yaml) and restart."
        ),
        description=(
            "Agent profiles surface — /agents builder, /api/v1/agents* management + "
            "runtime API, `agnes agent`/`agnes chat` CLI. Grandfathered on by default; "
            "an instance opts out via AGNES_AGENT_PROFILES_ENABLED=0."
        ),
    ),
)

_BY_NAME: dict[str, Switch] = {s.name: s for s in SWITCHES}


def get_switch(name: str) -> Switch:
    """The registry entry, or `KeyError` if there is none.

    Deliberately strict: a typo'd switch name is a programming error, and a
    silent `None` would resolve as "off" at the callsite.
    """
    return _BY_NAME[name]


def switch_value(name: str) -> Any:
    """Resolve a switch to its effective value.

    Order, identical for every switch and unchanged from the convention
    `feature_enabled` established:

        env var  >  server-config overlay  >  instance.yaml base  >  default

    The middle two collapse into one step: `config/loader.py` deep-merges the
    writable admin overlay over the static base at load time, so `get_value`
    already returns the fully-resolved value.

    `on_invalid` decides what an unrecognized `select` token does — fall back
    to the default (the common case) or raise (`analytics.backend`, where a
    typo must fail loudly at boot rather than silently pick a backend).

    Raises `ValueError` for a switch that declares `runtime_view`: its
    running value does not come from the merged config this function reads,
    so answering from here would be silently wrong rather than merely
    unavailable. See `Switch.runtime_view`.
    """
    switch = get_switch(name)

    if switch.runtime_view:
        raise ValueError(
            f"switch_value({name!r}): this switch's runtime does not read the merged "
            "config switch_value() resolves from — it reads the writable server-config "
            "overlay file ALONE, via app.chat.config.load_chat_config. Calling "
            "switch_value() here would silently return the wrong value for an instance "
            "that only set it in the static instance.yaml base. Read it through "
            "app/api/admin.py::_chat_flag_runtime_view (or the switch's own runtime "
            "read site) instead."
        )

    # Local import: `app.instance_config` imports this module, so a
    # module-level import here would be circular. Precedent:
    # `src/analytics_backend.py::resolve_analytics_backend_name`.
    import os

    from app.instance_config import coerce_flag_value, get_value

    raw: Any = None
    if switch.env_var:
        raw = os.environ.get(switch.env_var)
    if raw is None and switch.config_keys:
        raw = get_value(*switch.config_keys, default=None)
    if raw is None:
        return switch.default

    if switch.kind == "bool":
        return coerce_flag_value(raw, switch.default)

    if switch.kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return switch.default

    value = str(raw).strip().lower()
    if switch.kind == "select" and value not in switch.options:
        if switch.on_invalid == "raise":
            raise ValueError(
                f"invalid value {value!r} for switch {switch.name!r}; expected one of {', '.join(switch.options)}"
            )
        return switch.default
    return value
