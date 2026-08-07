# Switch Registry — PR1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `FEATURE_FLAGS` registry with a richer `Switch` registry in a new `app/switches.py`, and make `_EDITABLE_SECTIONS` plus the flag panel derive from it — so a switch's editability and the reason for it live on the switch instead of in three other places.

**Architecture:** One frozen dataclass `Switch` and one tuple `SWITCHES` in a new module. A `switch_value(name)` resolver implements the single resolution order (env > overlay > yaml > default). `app/instance_config.py` re-exports `FEATURE_FLAGS = SWITCHES` so existing importers do not churn. `app/api/admin.py` derives `_EDITABLE_SECTIONS` from the registry instead of hand-listing it, and the exemption reasons currently held in a test-file dict move onto the entries as `lock_reason`.

**Tech Stack:** Python 3.11+, FastAPI, pytest. No new dependencies.

## Global Constraints

- **No database work.** Switches live in the yaml overlay. No repository method, no `_pg.py` sibling, no Alembic revision, no `src/db.py` ladder step. If you find yourself editing `src/repositories/`, stop — you have misread the task.
- **Resolution order is unchanged:** `env var > server-config overlay > instance.yaml static base > default`. PR1 must not alter what any switch resolves to on any instance.
- **Boolean parsing goes through `app.instance_config.coerce_flag_value`.** Never re-implement the truthy rule. It is permissive: `false` only for `"0"`, `"false"`, `"no"`, `"off"`, `""` (case-insensitive); everything else is `true`.
- **Import direction is one-way.** `app/switches.py` must NOT import `app.instance_config` at module level — `instance_config` imports `switches`. Inside functions, a local import is correct and has precedent (`src/analytics_backend.py` does exactly this).
- **Vendor-agnostic.** No customer names, deployment hostnames, cloud project ids, or internal URLs in code, comments, docs, or commit messages.
- **No AI attribution** in commits or PR bodies.
- **CHANGELOG discipline.** This PR changes operator-visible behavior, so it needs a `## [Unreleased]` bullet in `CHANGELOG.md` in this same PR (Task 5).
- **Run only what the diff touches.** `.venv/bin/pytest tests/test_switches.py tests/test_feature_flags.py tests/test_admin_configure_api.py tests/test_instance_config.py -q`. Do not run the full suite locally; CI runs it on the PR.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/switches.py` *(create)* | The `Switch` dataclass, the `SWITCHES` tuple, `switch_value()`, and lookup helpers. The single source of truth. |
| `tests/test_switches.py` *(create)* | Registry integrity + resolver behavior. |
| `app/instance_config.py` *(modify)* | Drops `FeatureFlag` and the inline `FEATURE_FLAGS` tuple; re-exports from `app.switches` for compatibility. Keeps `feature_enabled` and `coerce_flag_value` where they are. |
| `app/api/admin.py` *(modify)* | `_EDITABLE_SECTIONS` derived; `_flag_default` reads `SWITCHES`; the inventory block gains `effect`, `editable`, `lock_reason`. |
| `tests/test_admin_configure_api.py` *(modify)* | The existing ratchet reads `SWITCHES`; `_NOT_LIVE_WRITABLE` is deleted in favour of `lock_reason`. |
| `docs/feature-flags.md`, `CONTRIBUTING.md`, `CHANGELOG.md` *(modify)* | Documentation and the sync-map row. |

`app/switches.py` is a new module rather than more lines in `app/instance_config.py` because that file is already 1389 lines and this is a distinct responsibility with its own contract.

---

### Task 1: The `Switch` dataclass and the `SWITCHES` registry

**Files:**
- Create: `app/switches.py`
- Test: `tests/test_switches.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `app.switches.Switch` (frozen dataclass), `app.switches.SWITCHES` (`tuple[Switch, ...]`), `app.switches.get_switch(name) -> Switch` (raises `KeyError` on unknown name).

**Context you need:** the seven current flags live in `app/instance_config.py` in a tuple named `FEATURE_FLAGS`, as `FeatureFlag(name=..., config_keys=..., env_var=..., default=..., description=...)`. Port all seven verbatim — same names, same config keys, same env vars, same defaults, same description strings. Do not reword a description in this task; a changed description is indistinguishable from a changed default when reviewing.

Two entries also need `runtime_view`, because `app/main.py` boots chat from `load_chat_config(DATA_DIR/state/instance.yaml)` — the writable overlay file alone, not the merged config. `chat` maps to the `ChatConfig` attribute `enabled`, `chat_approvals` to `approvals_enabled`. `app/api/admin.py` already has `_CHAT_RUNTIME_FLAGS` holding exactly this mapping; you are moving that knowledge onto the entries.

One entry needs `editable=False`: `data_apps`. Its reason comes from `tests/test_admin_configure_api.py::_NOT_LIVE_WRITABLE` — read the current wording there and carry its meaning across.

- [ ] **Step 1: Write the failing test**

Create `tests/test_switches.py`:

```python
"""Unified switch registry — integrity and resolution.

The registry in `app/switches.py` is the single source of truth for every
operator-facing toggle: its resolution order, its type, whether it can be
edited, and why not when it cannot. These tests guard the registry's own
shape; the derivation guards (that `_EDITABLE_SECTIONS` and the admin field
metadata follow from it) live in `tests/test_admin_configure_api.py`.
"""

from __future__ import annotations

import pytest

from app.switches import SWITCHES, Switch, get_switch


class TestRegistryIntegrity:
    def test_registry_is_not_empty(self):
        assert len(SWITCHES) >= 7

    def test_names_are_unique(self):
        names = [s.name for s in SWITCHES]
        assert len(names) == len(set(names)), "duplicate switch name"

    def test_env_vars_are_unique(self):
        env_vars = [s.env_var for s in SWITCHES if s.env_var]
        assert len(env_vars) == len(set(env_vars)), "duplicate env var"

    def test_config_keys_are_unique(self):
        keys = [s.config_keys for s in SWITCHES if s.config_keys]
        assert len(keys) == len(set(keys)), "duplicate config key path"

    def test_every_entry_carries_a_description(self):
        for s in SWITCHES:
            assert s.description.strip(), f"{s.name} has no description"

    def test_effect_is_a_known_value(self):
        for s in SWITCHES:
            assert s.effect in ("live", "restart", "deploy"), f"{s.name}: {s.effect}"

    def test_category_is_a_known_value(self):
        for s in SWITCHES:
            assert s.category in ("product", "operations", "locked"), f"{s.name}: {s.category}"

    def test_select_entries_declare_options_containing_their_default(self):
        for s in SWITCHES:
            if s.kind != "select":
                continue
            assert s.options, f"{s.name} is a select with no options"
            assert s.default in s.options, f"{s.name} default {s.default!r} not in options"

    def test_non_select_entries_declare_no_options(self):
        for s in SWITCHES:
            if s.kind != "select":
                assert s.options == (), f"{s.name} is {s.kind} but declares options"

    def test_locked_entries_state_a_reason(self):
        """A switch the UI refuses to edit must say why, in the product.

        The reason previously lived in a dict inside a test file, where an
        operator hitting the refusal could never see it.
        """
        for s in SWITCHES:
            if not s.editable:
                assert s.lock_reason.strip(), f"{s.name} is locked with no lock_reason"

    def test_editable_entries_state_no_reason(self):
        """Shrinks-only: a switch that became editable must clear its reason,
        so a stale explanation cannot outlive the restriction it described."""
        for s in SWITCHES:
            if s.editable:
                assert not s.lock_reason, f"{s.name} is editable but still carries a lock_reason"

    def test_deploy_entries_have_no_config_key(self):
        """`deploy` means there is nothing to write: the value is what the
        container was started with, not a section of instance.yaml."""
        for s in SWITCHES:
            if s.effect == "deploy":
                assert s.config_keys == (), f"{s.name} is deploy-class but declares a config key"

    def test_env_var_names_follow_the_convention(self):
        for s in SWITCHES:
            if s.env_var:
                assert s.env_var.isupper(), f"{s.env_var} is not upper-case"

    def test_switches_are_frozen(self):
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            SWITCHES[0].default = "mutated"  # type: ignore[misc]


class TestPortedFlagsAreUnchanged:
    """PR1 must not change what any instance resolves. These pin the seven
    flags' identity fields against the values they shipped with."""

    EXPECTED = {
        "studio": (("studio", "enabled"), "AGNES_STUDIO_ENABLED", True),
        "guardrails": (("guardrails", "enabled"), "AGNES_GUARDRAILS_ENABLED", True),
        "chat_approvals": (("chat", "approvals_enabled"), "AGNES_CHAT_APPROVALS_ENABLED", True),
        "chat": (("chat", "enabled"), "AGNES_CHAT_ENABLED", False),
        "data_apps": (("data_apps", "enabled"), "AGNES_DATA_APPS_ENABLED", False),
        "library_show_unverified_trust": (
            ("library", "show_unverified_trust"),
            "AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST",
            True,
        ),
        "mcp_query_param_token": (
            ("mcp", "allow_query_param_token"),
            "AGNES_MCP_ALLOW_QUERY_PARAM_TOKEN",
            True,
        ),
    }

    def test_all_seven_are_present(self):
        assert {s.name for s in SWITCHES} >= set(self.EXPECTED)

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_identity_fields_are_unchanged(self, name):
        s = get_switch(name)
        keys, env_var, default = self.EXPECTED[name]
        assert s.config_keys == keys
        assert s.env_var == env_var
        assert s.default is default
        assert s.kind == "bool"

    def test_chat_flags_declare_their_overlay_only_runtime_view(self):
        """`app/main.py` boots chat from the writable overlay file alone, so
        the panel must read these two from the same place the runtime does."""
        assert get_switch("chat").runtime_view == "enabled"
        assert get_switch("chat_approvals").runtime_view == "approvals_enabled"

    def test_data_apps_is_locked_with_its_sidecar_reason(self):
        s = get_switch("data_apps")
        assert s.editable is False
        assert "apps" in s.lock_reason


class TestGetSwitch:
    def test_returns_the_entry(self):
        assert get_switch("chat").name == "chat"

    def test_unknown_name_raises(self):
        with pytest.raises(KeyError):
            get_switch("no_such_switch")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_switches.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named 'app.switches'`.

- [ ] **Step 3: Create the module**

Create `app/switches.py`:

```python
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
```

Note on the long `library_show_unverified_trust` description: the original in `app/instance_config.py` is longer still. Shorten it to the text above — the full rationale belongs in `docs/feature-flags.md`, not in a registry field the admin panel renders into a hint. This is the one description you may change, and the test above does not pin descriptions.

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/pytest tests/test_switches.py -q
```

Expected: all pass.

- [ ] **Step 5: Verify the `effect` values against the real read sites**

`effect` is metadata in PR1 — nothing consumes it until the panel is built — so a wrong value here would ship silently and be wrong in the UI later. Check each one now:

```bash
grep -rn "feature_enabled(\"studio\"\|get_studio_enabled\|feature_enabled(\"guardrails\"\|library_show_unverified_trust\|allow_query_param_token" --include="*.py" app/ src/
```

Expected: each appears inside a request handler or a helper called from one (so `live` is right), while `chat` and `chat_approvals` appear in `app/main.py` startup and in `app/chat/config.py::load_chat_config` (so `restart` is right). If any grep contradicts the table, fix the `effect` value in the registry — the code is the authority, not this plan.

- [ ] **Step 6: Commit**

```bash
git add app/switches.py tests/test_switches.py
git commit -m "feat(switches): add the Switch registry with the seven current flags"
```

---

### Task 2: `switch_value()` and the `instance_config` compatibility layer

**Files:**
- Modify: `app/switches.py`
- Modify: `app/instance_config.py:219-294` (the `FeatureFlag` dataclass and the `FEATURE_FLAGS` tuple)
- Test: `tests/test_switches.py`

**Interfaces:**
- Consumes: `Switch`, `SWITCHES`, `get_switch` from Task 1.
- Produces: `app.switches.switch_value(name: str) -> Any`. `app.instance_config.FEATURE_FLAGS` remains importable and is now `SWITCHES`. `app.instance_config.feature_enabled` keeps its exact signature `(*keys: str, env_var: str | None = None, default: bool = False) -> bool`.

**Why the compatibility layer matters:** `app/web/router.py:25` and `app/api/admin.py` both do `from app.instance_config import FEATURE_FLAGS`. Breaking that import turns a mechanical refactor into a wide diff, and wide diffs hide behavior changes.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_switches.py`:

```python
class TestSwitchValueResolution:
    """The single resolution order: env > overlay/yaml > default."""

    def test_default_when_nothing_set(self, monkeypatch):
        import app.switches as sw

        monkeypatch.delenv("AGNES_CHAT_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: default)
        assert sw.switch_value("chat") is False

    def test_yaml_wins_over_default(self, monkeypatch):
        import app.switches as sw

        monkeypatch.delenv("AGNES_CHAT_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: True)
        assert sw.switch_value("chat") is True

    def test_env_wins_over_yaml(self, monkeypatch):
        import app.switches as sw

        monkeypatch.setenv("AGNES_CHAT_ENABLED", "0")
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: True)
        assert sw.switch_value("chat") is False

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", ""])
    def test_falsy_env_spellings(self, monkeypatch, raw):
        import app.switches as sw

        monkeypatch.setenv("AGNES_STUDIO_ENABLED", raw)
        assert sw.switch_value("studio") is False

    @pytest.mark.parametrize("raw", ["1", "true", "YES", "on", "enabled", "banana"])
    def test_permissive_truthy_env_spellings(self, monkeypatch, raw):
        """Unrecognized values are TRUE — the documented convention. An
        operator's intent to enable must not degrade to disabled over a
        casing mismatch."""
        import app.switches as sw

        monkeypatch.setenv("AGNES_CHAT_ENABLED", raw)
        assert sw.switch_value("chat") is True

    def test_unknown_switch_raises(self):
        import app.switches as sw

        with pytest.raises(KeyError):
            sw.switch_value("no_such_switch")


class TestBackwardCompatibility:
    def test_feature_flags_still_importable_from_instance_config(self):
        from app.instance_config import FEATURE_FLAGS
        from app.switches import SWITCHES

        assert FEATURE_FLAGS is SWITCHES

    def test_feature_enabled_signature_is_unchanged(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.delenv("AGNES_TEST_FLAG", raising=False)
        monkeypatch.setattr(ic, "get_value", lambda *k, default=None: default)
        assert ic.feature_enabled("a", "b", env_var="AGNES_TEST_FLAG", default=True) is True
        assert ic.feature_enabled("a", "b", env_var="AGNES_TEST_FLAG", default=False) is False

    def test_registry_entries_still_expose_the_old_attribute_names(self):
        """`app/api/admin.py` and `app/web/router.py` read `.name`,
        `.config_keys`, `.env_var`, `.default` and `.description` off registry
        entries. `Switch` is a superset of `FeatureFlag`, so they keep working."""
        from app.instance_config import FEATURE_FLAGS

        for flag in FEATURE_FLAGS:
            assert isinstance(flag.name, str)
            assert isinstance(flag.config_keys, tuple)
            assert isinstance(flag.env_var, str)
            assert isinstance(flag.description, str)
            assert isinstance(flag.default, bool), (
                f"{flag.name}: PR1 ports only boolean flags; a non-bool default here means "
                "a PR2 switch landed without the panel being taught its type"
            )
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_switches.py -q -k "SwitchValue or Backward"
```

Expected: FAIL — `AttributeError: module 'app.switches' has no attribute 'switch_value'`.

- [ ] **Step 3: Implement `switch_value`**

Append to `app/switches.py`:

```python
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
    """
    # Local import: `app.instance_config` imports this module, so a
    # module-level import here would be circular. Precedent:
    # `src/analytics_backend.py::resolve_analytics_backend_name`.
    import os

    from app.instance_config import coerce_flag_value, get_value

    switch = get_switch(name)

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
                f"invalid value {value!r} for switch {switch.name!r}; "
                f"expected one of {', '.join(switch.options)}"
            )
        return switch.default
    return value
```

- [ ] **Step 4: Point `instance_config` at the new registry**

In `app/instance_config.py`, delete the `FeatureFlag` dataclass and the entire `FEATURE_FLAGS` tuple (currently lines 219-294), and put this in their place:

```python
# The switch registry moved to `app/switches.py` so that a switch's type,
# effect class, editability and the reason for it live in one declaration.
# Re-exported under the old name because `app/web/router.py` and
# `app/api/admin.py` import it from here. `Switch` is a superset of the old
# `FeatureFlag`, so every attribute those callers read still resolves.
from app.switches import SWITCHES as FEATURE_FLAGS  # noqa: E402
from app.switches import Switch as FeatureFlag  # noqa: E402
```

Leave `coerce_flag_value` and `feature_enabled` exactly where they are and exactly as they are. `feature_enabled` takes config keys rather than a switch name and is used by callsites that predate the registry; it stays the bool-shaped facade.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_switches.py tests/test_feature_flags.py -q
```

Expected: all pass. `tests/test_feature_flags.py` must pass **unmodified** — it is the regression net for this refactor. If it fails, you changed behavior; fix the code, not the test.

- [ ] **Step 6: Verify the import cycle is genuinely absent**

```bash
.venv/bin/python -c "import app.switches; import app.instance_config; print('ok')"
.venv/bin/python -c "import app.instance_config; import app.switches; print('ok')"
```

Expected: `ok` twice. Both orders must work — one of them failing means a module-level import crept back in.

- [ ] **Step 7: Commit**

```bash
git add app/switches.py app/instance_config.py tests/test_switches.py
git commit -m "feat(switches): add switch_value() and re-export the registry from instance_config"
```

---

### Task 3: Derive `_EDITABLE_SECTIONS` and fold in the exemption reasons

**Files:**
- Modify: `app/api/admin.py:293-330` (the `_EDITABLE_SECTIONS` tuple)
- Modify: `tests/test_admin_configure_api.py:604-694` (the ratchet and `_NOT_LIVE_WRITABLE`)
- Test: `tests/test_admin_configure_api.py`

**Interfaces:**
- Consumes: `SWITCHES` and `Switch.editable` / `Switch.lock_reason` from Tasks 1-2.
- Produces: `app.api.admin._EDITABLE_SECTIONS` — same type as today (`tuple[str, ...]`), now computed.

**Context you need:** `_EDITABLE_SECTIONS` today is a hand-written tuple of 20 section names. Most are not switch-backed at all (`email`, `telegram`, `jira`, `auth`, `server`, `ai`, …) and must stay listed explicitly. Only the switch-backed ones should be derived, so that adding a switch cannot leave its section unwritable.

There is an existing ratchet in `tests/test_admin_configure_api.py` — `test_every_feature_flag_section_is_editable_or_explicitly_exempt` — which already derives from the registry and holds exemption reasons in a module-level dict `_NOT_LIVE_WRITABLE`. That dict is what moves onto the entries. `test_no_stale_exemption` keeps its shrinks-only job in a new form.

- [ ] **Step 1: Write the failing test**

Replace the `_NOT_LIVE_WRITABLE` dict and `test_no_stale_exemption` in `tests/test_admin_configure_api.py` with:

```python
    def _registry_sections(self):
        """Sections owned by the switch registry."""
        from app.switches import SWITCHES

        return {s.config_keys[0] for s in SWITCHES if s.config_keys}

    def _locked_sections(self):
        """Sections whose only switches are locked, with a stated reason.

        Replaces the old `_NOT_LIVE_WRITABLE` dict: the reason now lives on
        the entry, where the product can show it, instead of in this file
        where only a test reader ever saw it.
        """
        from app.switches import SWITCHES

        editable = {s.config_keys[0] for s in SWITCHES if s.editable and s.config_keys}
        locked = {s.config_keys[0] for s in SWITCHES if not s.editable and s.config_keys}
        return locked - editable

    def test_every_registry_section_is_editable_or_locked_with_a_reason(self):
        from app.api.admin import _EDITABLE_SECTIONS

        unaccounted = self._registry_sections() - set(_EDITABLE_SECTIONS) - self._locked_sections()
        assert not unaccounted, (
            "switch section neither writable via /admin/server-config nor backed by a locked "
            f"switch carrying a lock_reason: {sorted(unaccounted)}. The panel displays it, so "
            "a save that 400s is the operator's only signal."
        )

    def test_no_locked_section_is_also_editable(self):
        """Shrinks-only, in its new form: a switch that became editable must
        clear `lock_reason`, and its section must not appear in both sets."""
        from app.api.admin import _EDITABLE_SECTIONS

        stale = self._locked_sections() & set(_EDITABLE_SECTIONS)
        assert not stale, f"now editable — clear lock_reason on: {sorted(stale)}"

    def test_every_editable_switch_section_is_writable(self):
        """The derivation guard. Adding an editable switch whose section is
        not writable is the bug shape that shipped `mcp.allow_query_param_token`
        env-var-only, then `agent_profiles.enabled` after it."""
        from app.api.admin import _EDITABLE_SECTIONS
        from app.switches import SWITCHES

        for s in SWITCHES:
            if s.editable and s.config_keys:
                assert s.config_keys[0] in _EDITABLE_SECTIONS, (
                    f"{s.name} is editable but section {s.config_keys[0]!r} is not writable"
                )
```

Keep `test_the_documented_key_scrape_finds_something`, `_documented_keys` and `test_every_documented_section_is_editable_or_explicitly_exempt`.

**`_NOT_LIVE_WRITABLE` serves two ratchets, not one** — this cost a blocked task round during execution, so do not repeat the mistake. The registry-derived test asks "why is this registered flag not writable?", which `lock_reason` now answers. The documentation-derived test asks a different question about sections scraped from `docs/DEPLOYMENT.md`, and three of the old dict's four keys — `analytics`, `coordination`, `distribution` — own no switch in PR1's registry, so deleting the dict outright leaves that test with nothing to subtract and it fails.

Add a second, strictly smaller dict for exactly those three, scoped to the documentation ratchet only, carrying the three original reason strings **verbatim** from the dict you deleted:

```python
    #: Sections the deployment guide documents that own no registry switch YET.
    #:
    #: NOT a revival of `_NOT_LIVE_WRITABLE`. That dict answered "why is this
    #: registered flag not writable?", and the registry now answers it itself via
    #: `Switch.lock_reason`. What is left is a different and strictly smaller
    #: question: DEPLOYMENT.md documents these three, but no switch owns them, so
    #: neither derived set can speak for them.
    #:
    #: Temporary by construction. PR2 registers switches for `analytics.backend`,
    #: `coordination.backend` and `distribution.signed_urls`, and the shrinks-only
    #: guard below fails the moment one lands, forcing its removal from here.
    _DOCUMENTED_BUT_NOT_SWITCH_BACKED = {
        "analytics": "backend choice is governed by the state machine + a data migration, not a live patch",
        "coordination": "process topology; takes effect on restart, and the guide pairs it with a compose change",
        "distribution": "documented as an `instance.yaml` + `AGNES_DISTRIBUTION_*` pair, object-store credentials included",
    }

    def test_no_switch_backed_section_is_still_listed_as_undeclared(self):
        """Shrinks-only. A section that gains a switch must leave the dict —
        otherwise a stale entry would keep excusing a section the registry can
        now speak for, which is how the exemption this replaced grew stale."""
        from app.switches import SWITCHES

        owned = {s.config_keys[0] for s in SWITCHES if s.config_keys}
        stale = owned & set(self._DOCUMENTED_BUT_NOT_SWITCH_BACKED)
        assert not stale, f"now switch-backed — drop from _DOCUMENTED_BUT_NOT_SWITCH_BACKED: {sorted(stale)}"
```

`test_every_documented_section_is_editable_or_explicitly_exempt` then subtracts **both** `self._locked_sections()` and `set(self._DOCUMENTED_BUT_NOT_SWITCH_BACKED)`. `test_every_registry_section_is_editable_or_locked_with_a_reason` subtracts **only** `self._locked_sections()` — the new dict must never weaken the registry-derived ratchet, and that separation is the point.

Do not add these three to `_STATIC_EDITABLE_SECTIONS` instead. Their own recorded reasons say they must not be live-writable, and it would widen the write allowlist — Step 5 would report `added: ['analytics', 'coordination', 'distribution']`, the one outcome this task exists to prevent.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_admin_configure_api.py -q -k "registry_section or locked_section or editable_switch_section"
```

Expected: FAIL — `_locked_sections()` returns `{"data_apps"}`, and the derivation in Step 3 does not exist yet. `test_every_documented_section_is_editable_or_explicitly_exempt` passes only once `_DOCUMENTED_BUT_NOT_SWITCH_BACKED` from Step 1 is in place; if you skipped that dict, this is where you find out.

- [ ] **Step 3: Derive `_EDITABLE_SECTIONS`**

In `app/api/admin.py`, replace the hand-written tuple with:

```python
# Sections an admin can mutate.
#
# Two halves. `_STATIC_EDITABLE_SECTIONS` are the sections that carry ordinary
# configuration — hosts, credentials, limits — and own no switch. The rest is
# DERIVED from the switch registry, so adding an editable switch cannot leave
# its section unwritable; that omission shipped twice before this was
# mechanical (`mcp`, then `chat`).
#
# A typo'd section in the request body is still rejected loudly rather than
# being merged into the YAML root.
_STATIC_EDITABLE_SECTIONS: tuple[str, ...] = (
    "instance",
    "data_source",
    "email",
    "telegram",
    "jira",
    "theme",
    "server",
    "auth",
    "ai",
    "openmetadata",
    "desktop",
    "corporate_memory",
    "materialize",
    "marketplace",
    "connectors",
)

_EDITABLE_SECTIONS: tuple[str, ...] = tuple(
    sorted(
        set(_STATIC_EDITABLE_SECTIONS)
        | {s.config_keys[0] for s in SWITCHES if s.editable and s.config_keys}
    )
)
```

Add the import at the top of the file: `from app.switches import SWITCHES`.

Note what left the static list: `guardrails`, `library`, `mcp`, `chat` and `studio` are now derived (they own editable switches), and `data_apps` was never in it and stays out (its switch is locked). The resulting tuple must contain the same 20 names as before — sorted differently, which is fine because every consumer treats it as a set or renders it in its own order.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_admin_configure_api.py -q
```

Expected: all pass.

- [ ] **Step 5: Verify the editable set did not change**

A derivation that quietly widens the write allowlist is the one dangerous outcome of this task. Check the before/after explicitly:

```bash
.venv/bin/python -c "
from app.api.admin import _EDITABLE_SECTIONS
before = {'instance','data_source','email','telegram','jira','theme','server','auth','ai','openmetadata','desktop','corporate_memory','materialize','guardrails','library','marketplace','connectors','mcp','chat','studio'}
now = set(_EDITABLE_SECTIONS)
print('added:', sorted(now - before))
print('removed:', sorted(before - now))
assert now == before, 'the editable set changed'
print('unchanged, 20 sections')
"
```

Expected: `added: []`, `removed: []`, `unchanged, 20 sections`.

- [ ] **Step 6: Commit**

```bash
git add app/api/admin.py tests/test_admin_configure_api.py
git commit -m "refactor(admin): derive _EDITABLE_SECTIONS from the switch registry"
```

---

### Task 4: `_flag_default` reads the registry; the inventory exposes the new fields

**Files:**
- Modify: `app/api/admin.py:362-380` (`_flag_default`)
- Modify: `app/api/admin.py:1347-1395` (`_feature_flags_inventory`)
- Modify: `app/api/admin.py` (`_CHAT_RUNTIME_FLAGS`, now derivable)
- Test: `tests/test_feature_flags.py`

**Interfaces:**
- Consumes: `SWITCHES`, `Switch.effect`, `Switch.editable`, `Switch.lock_reason`, `Switch.runtime_view` from Tasks 1-2. The `from app.switches import SWITCHES` line in `app/api/admin.py` was added in Task 3 — if you are running this task standalone, add it.
- Produces: each item in the `feature_flags` array of `GET /api/admin/server-config` gains `effect: str`, `editable: bool`, `lock_reason: str`. Existing keys (`name`, `effective`, `source`, `default`, `env_var`, `description`) are unchanged. PR3's panel consumes these.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_feature_flags.py`:

```python
class TestInventoryExposesSwitchMetadata:
    """The panel cannot explain a refusal it is not told about."""

    def test_every_row_carries_the_new_fields(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 200
        rows = resp.json()["feature_flags"]
        assert rows, "inventory is empty"
        for row in rows:
            assert row["effect"] in ("live", "restart", "deploy")
            assert isinstance(row["editable"], bool)
            assert isinstance(row["lock_reason"], str)

    def test_locked_row_carries_its_reason(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        row = next(r for r in resp.json()["feature_flags"] if r["name"] == "data_apps")
        assert row["editable"] is False
        assert row["lock_reason"], "a locked switch must explain itself to the operator"

    def test_editable_row_carries_no_reason(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        row = next(r for r in resp.json()["feature_flags"] if r["name"] == "studio")
        assert row["editable"] is True
        assert row["lock_reason"] == ""

    def test_existing_fields_are_untouched(self, seeded_app):
        """PR3 rewrites the panel; until then the current renderer must keep
        working against the same keys."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        row = resp.json()["feature_flags"][0]
        for key in ("name", "effective", "source", "default", "env_var", "description"):
            assert key in row
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_feature_flags.py -q -k InventoryExposes
```

Expected: FAIL with `KeyError: 'effect'`.

- [ ] **Step 3: Derive `_CHAT_RUNTIME_FLAGS` and extend the inventory**

In `app/api/admin.py`, replace the hand-written `_CHAT_RUNTIME_FLAGS` dict with a derivation:

```python
#: Registry switches whose runtime value comes from `load_chat_config` rather
#: than the merged config, mapped to the ChatConfig attribute holding it.
#: Derived from `runtime_view` so a third chat-resolved switch cannot be added
#: without the panel following — the previous hand-written dict was the reason
#: this needed a Devin Review note on #1146/#1157.
_CHAT_RUNTIME_FLAGS = {s.name: s.runtime_view for s in SWITCHES if s.runtime_view}
```

In `_feature_flags_inventory`, extend the appended dict:

```python
        out.append(
            {
                "name": flag.name,
                "effective": effective,
                "source": source,
                "default": flag.default,
                "env_var": flag.env_var,
                "description": flag.description,
                "effect": flag.effect,
                "editable": flag.editable,
                "lock_reason": flag.lock_reason,
            }
        )
```

Then simplify `_flag_default` to read the registry by config key:

```python
def _flag_default(section: str, key: str, fallback: bool) -> bool:
    """The default the switch registry declares for a flag-backed field.

    Hand-copying it here is how `chat.approvals_enabled` ended up documented
    as off-by-default while the registry and the runtime had it on. `fallback`
    covers a declared field with no registry entry — a plain config boolean
    rather than a switch.
    """
    for s in SWITCHES:
        if s.config_keys == (section, key):
            return bool(s.default)
    return fallback
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_feature_flags.py tests/test_admin_configure_api.py tests/test_switches.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/api/admin.py tests/test_feature_flags.py
git commit -m "feat(admin): expose effect, editable and lock_reason in the flag inventory"
```

---

### Task 5: Documentation, sync-map and changelog

**Files:**
- Modify: `docs/feature-flags.md`
- Modify: `CONTRIBUTING.md:57` (the sync-map row)
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: nothing consumed by later tasks.

**Context you need:** `docs/feature-flags.md` keeps its filename deliberately — it is referenced from `CONTRIBUTING.md`, `CLAUDE.md`, the admin template and several docstrings, and renaming is churn without benefit.

- [ ] **Step 1: Update the sync-map row**

In `CONTRIBUTING.md`, replace the `New user-visible feature flag` row with:

```markdown
| New user-visible switch (feature flag, theme, layout, mode) | an entry in `app.switches.SWITCHES` + a row in `docs/feature-flags.md` (see that doc's "How to add a switch") — never a hand-rolled `os.environ.get(...)` / `get_value(...)` pair | BLOCKING | `tests/test_switches.py` (registry integrity) + `tests/test_admin_configure_api.py` (editable-section derivation) |
```

- [ ] **Step 2: Update the flag documentation**

In `docs/feature-flags.md`:

1. Change the "The registry" section to point at `app/switches.py::SWITCHES` and describe the `Switch` fields, including `effect`, `editable` and `lock_reason`.
2. Change "How to add a flag" to "How to add a switch" and renumber its steps: pick a name and config key, call `switch_value` at the read site, append the entry, add the doc row. Delete the step that told the author to touch `_EDITABLE_SECTIONS` — it is derived now.
3. In the "Current flags" table, add an **Editable** column. Every row reads `yes` except `data_apps`, which reads `no — apps_runner sidecar needs the \`apps\` Compose profile`.

- [ ] **Step 3: Add the changelog bullet**

Under `## [Unreleased]` in `CHANGELOG.md`, in the `### Changed` group:

```markdown
- **Every operator switch is declared in one registry, and says why it is or is not editable.** Gating was spread across the `FEATURE_FLAGS` registry, the `_EDITABLE_SECTIONS` write allowlist in `app/api/admin.py`, the `_KNOWN_FIELDS` form metadata beside it, and — for the reason a registered flag was *not* writable — a dict inside `tests/test_admin_configure_api.py`. Four homes for one switch's metadata, and the one an operator most needed (why can I see this and not change it?) was in a test file they will never read. `app/switches.py` now holds a `Switch` per toggle: its config key, env var, type, default, effect class (`live` / `restart` / `deploy`), whether it is editable, and `lock_reason` when it is not. `_EDITABLE_SECTIONS` is derived from it, so adding an editable switch can no longer leave its section rejecting saves — the omission that shipped `mcp.allow_query_param_token` env-var-only and `agent_profiles.enabled` after it. `data_apps` keeps its existing, deliberate restriction (the flag is read per request, but the apps_runner sidecar sits behind the `apps` Compose profile, so a live flip would surface a backend-less feature) — now stated on the switch and returned by `GET /api/admin/server-config`, which gains `effect`, `editable` and `lock_reason` per row. `FEATURE_FLAGS` remains importable from `app.instance_config` and resolves to the same registry; `feature_enabled` is unchanged. No behavior changes on any instance: the editable section set is identical, and `tests/test_feature_flags.py` passes unmodified as the regression net.
```

- [ ] **Step 4: Verify the sync-map gate**

```bash
python3 scripts/verify_syncmap.py
```

Expected: `verify-syncmap: clean`.

- [ ] **Step 5: Run the touched tests once more**

```bash
.venv/bin/pytest tests/test_switches.py tests/test_feature_flags.py tests/test_admin_configure_api.py tests/test_instance_config.py tests/test_instance_config_overlay.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add docs/feature-flags.md CONTRIBUTING.md CHANGELOG.md
git commit -m "docs(switches): document the registry and update the sync-map row"
```

---

## Done when

- `tests/test_switches.py` passes and covers registry integrity plus resolution order.
- `tests/test_feature_flags.py` passes **unmodified except for the added inventory class** — it is the proof that no instance's behavior changed.
- `_EDITABLE_SECTIONS` contains exactly the same 20 sections as before, now derived.
- `GET /api/admin/server-config` returns `effect`, `editable` and `lock_reason` per flag row.
- `python3 scripts/verify_syncmap.py` is clean.
- A draft PR is open and CI is green. Do not merge — merging waits for an explicit instruction.

## Not in this PR

**Full `_KNOWN_FIELDS` generation.** The spec lists `_KNOWN_FIELDS` among PR1's
derived artifacts, and PR1 derives the half that can bite today — the
`default` each flag-backed field declares, via `_flag_default` (Task 4).
Generating `kind` and `options` from the registry is deliberately deferred to
PR2, because all seven PR1 switches are booleans with no options: the code
would have no case to exercise and no test that could fail. It is the arrival
of `theme` and `ui_layout` in PR2 — a select whose admin options are a strict
subset of what the resolver accepts — that gives the generator something to
be right about.

PR2 absorbs the bespoke and enum resolvers (`theme`, `ui_layout`, `home.show_*`, store verification, slack transport, distribution, analytics, coordination) and is where `paper` and `rail` first become reachable from the admin UI. PR3 absorbs the operational switches and classifies the six security-locked ones. PR4 is the paper migration of the settings page. Each gets its own plan, written against the shape this PR lands.
