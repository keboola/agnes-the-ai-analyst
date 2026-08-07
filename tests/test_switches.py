"""Unified switch registry — integrity and resolution.

The registry in `app/switches.py` is the single source of truth for every
operator-facing toggle: its resolution order, its type, whether it can be
edited, and why not when it cannot. These tests guard the registry's own
shape; the derivation guards (that `_EDITABLE_SECTIONS` and the admin field
metadata follow from it) live in `tests/test_admin_configure_api.py`.
"""

from __future__ import annotations

import pytest

from app.switches import SWITCHES, get_switch


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
