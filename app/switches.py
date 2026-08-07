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


@dataclass(frozen=True)
class Switch:
    """One operator-facing toggle.

    `effect` and `editable` are deliberately orthogonal: `effect` states what
    the system *can* do with a new value, `editable` whether we *offer* one.
    A switch is locked for one of three reasons — nothing to write
    (`effect="deploy"`), a deliberate security lock, or an unmet dependency —
    and `lock_reason` is what the product shows the operator in each case.
    """

    name: str
    config_keys: tuple[str, ...]
    env_var: str
    kind: str
    default: Any
    effect: str
    category: str
    description: str
    options: tuple[str, ...] = ()
    danger: bool = False
    editable: bool = True
    lock_reason: str = ""
    on_invalid: str = "default"
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
        description=(
            "Accept the MCP bearer token as a ?token= query param on SSE GET, for clients "
            "that cannot set headers. On by default (grandfathered). The token lands in every "
            "request log when used (CWE-598) — turn this off if all your MCP clients send the "
            "Authorization header."
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
