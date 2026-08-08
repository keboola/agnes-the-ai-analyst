"""The `instance.experience` preset (spec 2026-08-07-default-chrome-ux-parity).

One line flips the DEFAULTS of every experience-coupled knob; any per-knob
env/yaml setting still wins, and per-knob resolution order is unchanged
(`env(knob) > yaml(knob) > preset-implied default > built-in default`).
`classic` — or an absent/invalid key — must be byte-for-byte the
pre-redesign experience.
"""

from __future__ import annotations

import pytest

import app.instance_config as ic


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "AGNES_INSTANCE_EXPERIENCE",
        "AGNES_UI_LAYOUT",
        "AGNES_INSTANCE_THEME",
        "AGNES_STACK_AUTO_MEMBERSHIP",
        "AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)


class TestExperienceResolution:
    def test_default_is_classic(self):
        assert ic.get_experience() == "classic"

    def test_yaml_sets_redesign(self, monkeypatch):
        monkeypatch.setattr(
            ic, "get_value", lambda *keys, default=None: "redesign" if keys == ("instance", "experience") else default
        )
        assert ic.get_experience() == "redesign"

    def test_env_wins_over_yaml(self, monkeypatch):
        monkeypatch.setattr(
            ic, "get_value", lambda *keys, default=None: "redesign" if keys == ("instance", "experience") else default
        )
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "classic")
        assert ic.get_experience() == "classic"

    def test_invalid_value_falls_back_to_classic(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "fancy")
        assert ic.get_experience() == "classic"


class TestPresetImpliedDefaults:
    def test_classic_defaults_are_the_pre_redesign_world(self):
        assert ic.get_ui_layout() == "topnav"
        assert ic.get_instance_theme() == "blue"
        assert ic.get_stack_auto_membership() is False

    def test_redesign_preset_flips_the_defaults(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "redesign")
        assert ic.get_ui_layout() == "rail"
        assert ic.get_instance_theme() == "paper"
        assert ic.get_stack_auto_membership() is True

    def test_library_trust_flag_is_not_preset_coupled(self, monkeypatch):
        """The trust vocabulary is gated to the paper theme at every
        ``mark()`` callsite, so default-chrome parity comes from the theme
        gate itself — the preset must NOT rewrite this flag's default (the
        registry keeps it, and the inventory would otherwise disagree with
        the runtime)."""
        assert "library_show_unverified_trust" not in ic.PRESET_COUPLED_FLAGS
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "redesign")
        assert ic.preset_flag_default("library_show_unverified_trust") is False

    def test_per_knob_setting_beats_the_preset(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "redesign")
        monkeypatch.setenv("AGNES_UI_LAYOUT", "topnav")
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "blue")
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "0")
        assert ic.get_ui_layout() == "topnav"
        assert ic.get_instance_theme() == "blue"
        assert ic.get_stack_auto_membership() is False

    def test_per_knob_setting_beats_classic_too(self, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")
        assert ic.get_ui_layout() == "rail"
        assert ic.get_stack_auto_membership() is True

    def test_verification_stays_a_governance_opt_in(self, monkeypatch):
        """`store.verification_enabled` needs a reviewer, not a theme — the
        redesign preset must NOT flip it."""
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "redesign")
        assert ic.get_store_verification_enabled() is False

    def test_invalid_layout_value_falls_back_to_the_preset_default(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "redesign")
        monkeypatch.setenv("AGNES_UI_LAYOUT", "sidebarish")
        assert ic.get_ui_layout() == "rail"


class TestStackAutoMembershipFlag:
    def test_registered_in_the_flag_registry(self):
        by_name = {f.name: f for f in ic.FEATURE_FLAGS}
        flag = by_name["stack_auto_membership"]
        assert flag.config_keys == ("features", "stack_auto_membership")
        assert flag.env_var == "AGNES_STACK_AUTO_MEMBERSHIP"
        assert flag.default is False
        assert flag.description

    def test_yaml_key_resolves(self, monkeypatch):
        monkeypatch.setattr(
            ic,
            "get_value",
            lambda *keys, default=None: True if keys == ("features", "stack_auto_membership") else default,
        )
        assert ic.get_stack_auto_membership() is True


# ---------------------------------------------------------------------------
# The editable-field registry must never render a default the runtime ignores
# ---------------------------------------------------------------------------


def test_the_preset_itself_resolves_its_panel_default(monkeypatch):
    """An env-set preset must not be reported as `classic` by the panel.

    `collectSection` posts every rendered leaf, so whatever the panel shows
    for an UNSET key is what a routine "Save section" persists. Rendering the
    static `classic` on an `AGNES_INSTANCE_EXPERIENCE=redesign` instance wrote
    `instance.experience: classic` into the overlay — harmless while the env
    var is present (env wins) and a silent revert of the entire preset the day
    it is dropped (Devin on #1199).
    """
    import app.instance_config as ic
    from app.api.admin import _known_fields_resolved

    monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "redesign")
    ic.reset_cache()
    assert _known_fields_resolved()["instance"]["experience"]["default"] == "redesign"

    monkeypatch.delenv("AGNES_INSTANCE_EXPERIENCE", raising=False)
    ic.reset_cache()
    assert _known_fields_resolved()["instance"]["experience"]["default"] == "classic"


def test_every_preset_coupled_knob_in_the_registry_is_resolved(monkeypatch):
    """The durable half: assert the COUPLING TABLE, not three literals.

    The resolver has now been extended three times, each time because one
    coupled knob had been missed. Rather than trusting the next enumeration,
    walk `preset_knob_default` / `preset_flag_default` — the single source of
    the mapping — and require that any coupled key which the panel actually
    renders differs between the two presets.
    """
    import app.instance_config as ic
    from app.api.admin import _KNOWN_FIELDS, _known_fields_resolved

    coupled = [("instance", "theme"), ("instance", "ui_layout"), ("features", "stack_auto_membership")]
    coupled = [(s, k) for s, k in coupled if "default" in _KNOWN_FIELDS.get(s, {}).get(k, {})]
    assert coupled, "the coupling table drifted — no coupled knob is rendered at all"

    seen = {}
    for preset in ("classic", "redesign"):
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", preset)
        ic.reset_cache()
        fields = _known_fields_resolved()
        seen[preset] = {(s, k): fields[s][k]["default"] for s, k in coupled}
        seen[preset][("instance", "experience")] = fields["instance"]["experience"]["default"]
    monkeypatch.delenv("AGNES_INSTANCE_EXPERIENCE", raising=False)
    ic.reset_cache()

    for key in seen["classic"]:
        assert seen["classic"][key] != seen["redesign"][key], (
            f"{key[0]}.{key[1]} renders the same panel default under both presets — "
            "it is preset-coupled at runtime but static in the registry"
        )
