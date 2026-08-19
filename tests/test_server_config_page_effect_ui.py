"""Tests for the /admin/server-config page's honest restart messaging and
the feature-flag `effect` / `editable` / `lock_reason` surfacing.

Confirmed defects being closed here:
1. Hero copy claimed every save triggers an app restart — POST
   /api/admin/server-config only calls reset_cache(); nothing restarts.
2. GET /api/admin/server-config ships `effect` (live/restart/deploy),
   `editable`, and `lock_reason` for every feature flag; the page rendered
   none of it (name/value/source/env_var/description only).
3. The danger-zone section set was a hardcoded JS literal
   (`new Set(["auth", "server"])`) duplicating the server-shipped
   `danger_sections` field from the same GET response.
4. The save toast and danger-zone confirmation dialog both asserted an app
   restart unconditionally, contradicting a fixed hero and each other.

The page renders its dynamic content (feature-flag table, danger-zone
gating, save-result banner) entirely client-side via the inline <script>
that ships with the page, so these tests assert on the *rendered HTML
document* — the same page-shell-marker style already used by
tests/test_admin_server_config.py — rather than driving a browser.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _load_page(seeded_app) -> str:
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    c.cookies.set("access_token", token)
    try:
        resp = c.get("/admin/server-config", headers={"Accept": "text/html"})
    finally:
        c.cookies.clear()
    assert resp.status_code == 200, resp.text
    return resp.text


class TestHeroCopyIsHonest:
    def test_hero_no_longer_claims_save_restarts_the_app(self, seeded_app):
        """The API never restarts anything (POST only calls reset_cache()) —
        the old hero string asserted otherwise."""
        body = _load_page(seeded_app)
        assert "Save triggers an app restart" not in body
        # Still allowed (expected, even) to mention that a restart is
        # sometimes needed — just not as a blanket guarantee.
        assert "restart" in body.lower()


class TestDangerDialogIsHonest:
    def test_danger_dialog_no_longer_asserts_restart_is_required(self, seeded_app):
        """The danger-zone confirmation modal claimed a restart was always
        required for auth/server saves — not true for every field in those
        sections."""
        body = _load_page(seeded_app)
        assert "An app restart is required for the change to take effect." not in body


class TestFeatureFlagEffectSurfaced:
    def test_effect_rendered_in_flag_summary(self, seeded_app):
        """`effect` (live/restart/deploy) must reach the rendered flag table,
        not just the GET payload."""
        body = _load_page(seeded_app)
        assert "f.effect" in body
        assert ">Effect<" in body

    def test_lock_reason_surfaced_for_locked_flag(self, seeded_app):
        """A flag with `editable: false` must expose why via `lock_reason`."""
        body = _load_page(seeded_app)
        assert "f.editable" in body
        assert "f.lock_reason" in body


class TestDangerSectionsSourcedFromPayload:
    def test_danger_set_reads_server_payload(self, seeded_app):
        """DANGER_SECTIONS must be built from the GET response's
        `danger_sections` field, not only the hardcoded JS literal."""
        body = _load_page(seeded_app)
        assert "original.danger_sections" in body

    def test_hardcoded_pair_kept_only_as_fallback(self, seeded_app):
        """The literal pair may remain as a fallback for an older API that
        doesn't ship `danger_sections` yet, but must not be the sole source
        (covered by test_danger_set_reads_server_payload above)."""
        body = _load_page(seeded_app)
        assert 'new Set(["auth", "server"])' in body


class TestSaveToastConsumesHonestFields:
    def test_toast_consumes_restart_required_and_sections_effect(self, seeded_app):
        """The save-result banner must read the POST response's
        `restart_required` + `sections_effect` fields instead of asserting a
        restart unconditionally."""
        body = _load_page(seeded_app)
        assert "restart_required" in body
        assert "sections_effect" in body
        assert "already in effect" in body.lower()
