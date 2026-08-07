"""Unified switch registry — integrity and resolution.

The registry in `app/switches.py` is the single source of truth for every
operator-facing toggle: its resolution order, its type, whether it can be
edited, and why not when it cannot. These tests guard the registry's own
shape; the derivation guards (that `_EDITABLE_SECTIONS` and the admin field
metadata follow from it) live in `tests/test_admin_configure_api.py`.
"""

from __future__ import annotations

import pytest

from app.switches import CATEGORIES, EFFECTS, KINDS, ON_INVALID, SWITCHES, get_switch


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
            assert s.effect in EFFECTS, f"{s.name}: {s.effect}"

    def test_category_is_a_known_value(self):
        for s in SWITCHES:
            assert s.category in CATEGORIES, f"{s.name}: {s.category}"

    def test_kind_is_a_known_value(self):
        """Unlike `effect` and `category`, `kind` had no validity test: a
        typo'd `kind="boolean"` falls past every branch in `switch_value` and
        returns a lowercased STRING — and `"false"` is truthy at a callsite
        doing `if switch_value(...)`."""
        for s in SWITCHES:
            assert s.kind in KINDS, f"{s.name}: {s.kind}"

    def test_on_invalid_is_a_known_value(self):
        for s in SWITCHES:
            assert s.on_invalid in ON_INVALID, f"{s.name}: {s.on_invalid}"

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


class TestSwitchValueResolution:
    """The single resolution order: env > overlay/yaml > default.

    Exercised against `data_apps` / `studio` / `guardrails` rather than
    `chat`: `chat` declares `runtime_view` (its runtime reads the writable
    overlay alone via `load_chat_config`, not the merged config `get_value`
    reads), so `switch_value("chat")` raises — see
    `TestSwitchValueRefusesRuntimeViewSwitches` below. None of the three used
    here declare `runtime_view`, so they exercise the generic resolution
    order untouched by that carve-out.
    """

    def test_default_when_nothing_set(self, monkeypatch):
        import app.switches as sw

        monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: default)
        assert sw.switch_value("data_apps") is False

    def test_yaml_wins_over_default(self, monkeypatch):
        import app.switches as sw

        monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: True)
        assert sw.switch_value("data_apps") is True

    def test_env_wins_over_yaml(self, monkeypatch):
        import app.switches as sw

        monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "0")
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: True)
        assert sw.switch_value("data_apps") is False

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

        monkeypatch.setenv("AGNES_GUARDRAILS_ENABLED", raw)
        assert sw.switch_value("guardrails") is True

    def test_unknown_switch_raises(self):
        import app.switches as sw

        with pytest.raises(KeyError):
            sw.switch_value("no_such_switch")


class TestSwitchValueRefusesRuntimeViewSwitches:
    """`switch_value()` reads the merged config; `chat` and `chat_approvals`
    do not run from there — `app/main.py` boots them via
    `load_chat_config(DATA_DIR/state/instance.yaml)`, the writable overlay
    file alone. Answering from `switch_value()` would be silently wrong
    (True for an instance that only set the flag in the static base, while
    the runtime it gates has it off), so it must raise instead."""

    def test_switch_value_chat_raises(self):
        import app.switches as sw

        with pytest.raises(ValueError, match="chat"):
            sw.switch_value("chat")

    def test_switch_value_chat_approvals_raises(self):
        import app.switches as sw

        with pytest.raises(ValueError, match="chat_approvals"):
            sw.switch_value("chat_approvals")

    def test_non_runtime_view_switch_is_unaffected(self):
        """Sanity check that the guard is scoped to `runtime_view` switches,
        not a blanket regression on `switch_value`."""
        import app.switches as sw

        assert sw.switch_value("studio") is True


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
