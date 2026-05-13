"""Tests for the admin-configurable flea-market content-guardrail thresholds (#281).

Covers:
1. The four new `get_guardrails_min_*` getters in app/instance_config.py:
   defaults, overlay-driven overrides, type coercion, and the
   `max(1, int(val))` floor.
2. The round-trip: POST to /api/admin/server-config patches
   `guardrails.min_description_chars`, the next inline content check
   uses the new floor (closes the "primary testing gap" Vojta noted in
   the PR #281 safe-fix commit message).

These tests close the only real gap surfaced in the PR #281 takeover
review — every other reviewer finding was either already addressed in
Vojta's safe-fix commit or intentionally deferred (operator-direction
decisions on the `min_*=0` semantics + POST-time integer validation).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml as _yaml


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _reset_cache() -> None:
    import app.instance_config as ic
    ic._instance_config = None


# ---------------------------------------------------------------------------
# Unit tests for the four new getters
# ---------------------------------------------------------------------------


class TestGuardrailGetterDefaults:
    """Each getter returns the documented default when nothing is configured."""

    def test_min_description_chars_default_60(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        _reset_cache()
        from app.instance_config import get_guardrails_min_description_chars
        assert get_guardrails_min_description_chars() == 60

    def test_min_command_description_chars_default_25(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        _reset_cache()
        from app.instance_config import get_guardrails_min_command_description_chars
        assert get_guardrails_min_command_description_chars() == 25

    def test_min_distinct_words_default_5(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        _reset_cache()
        from app.instance_config import get_guardrails_min_distinct_words
        assert get_guardrails_min_distinct_words() == 5

    def test_min_body_chars_default_200(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        _reset_cache()
        from app.instance_config import get_guardrails_min_body_chars
        assert get_guardrails_min_body_chars() == 200


class TestGuardrailGetterOverlay:
    """Operator-supplied overlay values win over defaults."""

    def _seed_overlay(self, tmp_path, monkeypatch, payload: dict) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(_yaml.dump(payload))
        _reset_cache()

    def test_overlay_overrides_min_description_chars(self, tmp_path, monkeypatch):
        self._seed_overlay(tmp_path, monkeypatch, {
            "guardrails": {"min_description_chars": 90},
        })
        from app.instance_config import get_guardrails_min_description_chars
        assert get_guardrails_min_description_chars() == 90

    def test_overlay_overrides_min_body_chars(self, tmp_path, monkeypatch):
        self._seed_overlay(tmp_path, monkeypatch, {
            "guardrails": {"min_body_chars": 500},
        })
        from app.instance_config import get_guardrails_min_body_chars
        assert get_guardrails_min_body_chars() == 500

    def test_string_value_coerced_to_int(self, tmp_path, monkeypatch):
        # An operator hand-editing the YAML can leave a string that's still
        # numeric — int() accepts it. Documented defensively in the getter.
        self._seed_overlay(tmp_path, monkeypatch, {
            "guardrails": {"min_distinct_words": "8"},
        })
        from app.instance_config import get_guardrails_min_distinct_words
        assert get_guardrails_min_distinct_words() == 8

    def test_garbage_value_falls_back_to_default(self, tmp_path, monkeypatch):
        # Bool / non-numeric string / other garbage hits the
        # `(TypeError, ValueError)` branch and returns the documented default.
        self._seed_overlay(tmp_path, monkeypatch, {
            "guardrails": {"min_command_description_chars": "not-a-number"},
        })
        from app.instance_config import get_guardrails_min_command_description_chars
        assert get_guardrails_min_command_description_chars() == 25

    def test_zero_or_negative_floored_to_one(self, tmp_path, monkeypatch):
        # `max(1, int(val))` — operator setting 0 to "disable" doesn't
        # actually disable; it's silently coerced to 1. Documented behavior;
        # this test pins the contract so a future change to use 0-as-sentinel
        # has to update this test (and reviewers see the policy decision).
        self._seed_overlay(tmp_path, monkeypatch, {
            "guardrails": {"min_description_chars": 0},
        })
        from app.instance_config import get_guardrails_min_description_chars
        assert get_guardrails_min_description_chars() == 1

        # Negative integer hits the same floor.
        self._seed_overlay(tmp_path, monkeypatch, {
            "guardrails": {"min_body_chars": -50},
        })
        from app.instance_config import get_guardrails_min_body_chars
        assert get_guardrails_min_body_chars() == 1


# ---------------------------------------------------------------------------
# Round-trip: PATCH /api/admin/server-config → next inline check uses new floor
# ---------------------------------------------------------------------------


class TestPatchRoundTrip:
    """The "primary testing gap" Vojta flagged: an admin PATCH to
    `guardrails.min_description_chars` must take effect on the very next
    `content_check` call, with no app restart. The cache is invalidated
    by /api/admin/server-config's reset_cache() bracket.
    """

    def _write_skill(self, plugin_dir: Path, *, description: str) -> None:
        target = plugin_dir / "skills" / "test-skill"
        target.mkdir(parents=True, exist_ok=True)
        body = "Body content explaining the skill in enough words to clear the body floor. " * 4
        (target / "SKILL.md").write_text(
            f"---\nname: test-skill\ndescription: {description}\n---\n\n{body}\n",
            encoding="utf-8",
        )

    def test_patch_min_description_chars_takes_effect_next_check(
        self, seeded_app, monkeypatch, tmp_path,
    ):
        # 75-char description: passes default floor (60) but fails after
        # we PATCH the floor to 90.
        mid_length = "Use when validating the round-trip live config thresholds end to end now."
        assert 60 <= len(mid_length) < 90, len(mid_length)

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        _reset_cache()

        plugin_dir = Path(tempfile.mkdtemp(prefix="agnes_admin_config_test_"))
        try:
            self._write_skill(plugin_dir, description=mid_length)

            # Step 1: at default floor (60), the description passes.
            from src.store_guardrails.content_check import check as content_check
            result = content_check(plugin_dir)
            assert result["status"] == "pass", (
                f"description {len(mid_length)} chars should pass default floor 60, "
                f"got: {result}"
            )

            # Step 2: PATCH the floor to 90 via the admin API.
            c = seeded_app["client"]
            token = seeded_app["admin_token"]
            r = c.post(
                "/api/admin/server-config",
                headers=_auth(token),
                json={"sections": {"guardrails": {"min_description_chars": 90}}},
            )
            assert r.status_code in (200, 204), r.text

            # Step 3: same description, same content_check — must now fail
            # with too_short. Cache invalidation done inside the admin POST
            # handler; no test-side reset_cache() call is needed (or
            # acceptable — that would be testing the test, not the system).
            result_after = content_check(plugin_dir)
            assert result_after["status"] == "fail", (
                f"after PATCH to floor 90, {len(mid_length)}-char description "
                f"must fail; got: {result_after}"
            )
            codes = {issue["code"] for issue in result_after["issues"]}
            assert "too_short" in codes, (
                f"expected too_short in issue codes, got: {codes}"
            )

            # Step 4: PATCH the floor back to 60 (fixture hygiene + extra
            # confirmation that subsequent PATCHes also propagate).
            r = c.post(
                "/api/admin/server-config",
                headers=_auth(token),
                json={"sections": {"guardrails": {"min_description_chars": 60}}},
            )
            assert r.status_code in (200, 204), r.text
            assert content_check(plugin_dir)["status"] == "pass", (
                "PATCH-back-to-default did not propagate"
            )
        finally:
            shutil.rmtree(plugin_dir, ignore_errors=True)
            _reset_cache()
