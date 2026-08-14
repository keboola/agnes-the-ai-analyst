"""The `instance.experience` preset (spec 2026-08-07-default-chrome-ux-parity;
`classic` retired in the Wave 0 legacy-retirement — see `app/switches.py`).

`redesign` is now the only option and the default; any per-knob env/yaml
setting still wins over the preset-implied default, and per-knob resolution
order is unchanged (`env(knob) > yaml(knob) > preset-implied default >
built-in default`). An absent value, or any invalid one (including the
retired `classic`), resolves to `redesign` via `on_invalid="default"`.
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
    def test_default_is_redesign(self):
        assert ic.get_experience() == "redesign"

    def test_yaml_sets_redesign(self, monkeypatch):
        monkeypatch.setattr(
            ic, "get_value", lambda *keys, default=None: "redesign" if keys == ("instance", "experience") else default
        )
        assert ic.get_experience() == "redesign"

    # `test_env_wins_over_yaml` removed: it relied on `classic` being a
    # second *valid* value so env and yaml could disagree; with
    # options=("redesign",), setting the env var to "classic" is now just an
    # invalid value that falls back to the default (which happens to be
    # "redesign" too), so the assertion could no longer distinguish
    # "env won" from "env was ignored and the default applied". The
    # underlying env > yaml > default precedence is still covered generically
    # by tests/test_switches.py::TestSwitchValueResolution::test_env_wins_over_yaml
    # (exercised against a switch with two real options).

    def test_invalid_value_falls_back_to_redesign(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "fancy")
        assert ic.get_experience() == "redesign"


class TestPresetImpliedDefaults:
    def test_default_experience_is_the_redesign_world(self):
        """With no `instance.experience` override at all, the coupled knobs
        resolve to the redesign values — `redesign` is the default now, not
        an opt-in (Wave 0 legacy retirement)."""
        assert ic.get_ui_layout() == "rail"
        assert ic.get_instance_theme() == "paper"
        assert ic.get_stack_auto_membership() is True

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
    """The panel-rendered default for `instance.experience` must track the
    CURRENT resolved preset, not a hardcoded literal — whether that value is
    reached via an explicit env override or (now that `redesign` is the only
    option) via the plain default.

    `collectSection` posts every rendered leaf, so whatever the panel shows
    for an UNSET key is what a routine "Save section" persists. A static
    literal here would silently rewrite `instance.experience` to a value the
    runtime doesn't actually use (Devin Review on #1199).
    """
    import app.instance_config as ic
    from app.api.admin import _known_fields_resolved

    monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "redesign")
    ic.reset_cache()
    assert _known_fields_resolved()["instance"]["experience"]["default"] == "redesign"

    monkeypatch.delenv("AGNES_INSTANCE_EXPERIENCE", raising=False)
    ic.reset_cache()
    assert _known_fields_resolved()["instance"]["experience"]["default"] == "redesign"


# `test_every_preset_coupled_knob_in_the_registry_is_resolved` removed: it
# looped `for preset in ("classic", "redesign")`, setting
# `AGNES_INSTANCE_EXPERIENCE` to each and asserting the resolved panel
# defaults *differ* between them. With `classic` retired, setting the env var
# to "classic" is no longer a distinct valid state — `on_invalid="default"`
# resolves it to "redesign" too, so both loop iterations now produce the same
# values and the differ-assertion (`seen["classic"][key] != seen["redesign"][key]`)
# fails by construction, not because of a real regression. The coupling table
# itself (theme -> paper, ui_layout -> rail, stack_auto_membership -> True)
# is still exercised by `test_default_experience_is_the_redesign_world` above
# and by `test_redesign_preset_flips_the_defaults` below.
