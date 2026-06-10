"""The onboarding tour must not silently go stale as the UI churns.

`app/web/onboarding.py` is the single source of truth for the guided-tour
steps. These tests assert that every step still points at:
  • a registered route, and
  • a DOM anchor (`data-tour="<anchor>"`) that actually exists in a template.

So if someone removes a nav item, renames its anchor, or deletes a route,
the tour step that referenced it turns this suite red instead of shipping a
dead spotlight. They also pin the audience split (admin vs non-admin) and the
server-side wiring that feeds the steps to the engine — guarding the "single
source of truth" property itself (no hardcoded steps creep back into JS).
"""

from __future__ import annotations

from pathlib import Path

from app.web.onboarding import ONBOARDING_STEPS, steps_for

TEMPLATES = Path("app/web/templates")
STATIC = Path("app/web/static")

VALID_AUDIENCES = {"all", "admin", "non_admin"}


def _all_template_text() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(TEMPLATES.rglob("*.html"))
    )



def test_every_step_audience_is_valid() -> None:
    bad = [(s.key, s.audience) for s in ONBOARDING_STEPS if s.audience not in VALID_AUDIENCES]
    assert not bad, f"invalid audience values: {bad}"


def test_every_step_anchor_exists_in_templates() -> None:
    """A step's spotlight anchor must be a `data-tour="<anchor>"` that some
    template actually renders — otherwise the spotlight lands on nothing."""
    blob = _all_template_text()
    missing = [
        s.key
        for s in ONBOARDING_STEPS
        if s.anchor and f'data-tour="{s.anchor}"' not in blob
    ]
    assert not missing, (
        "onboarding steps reference data-tour anchors absent from all templates "
        f"(nav changed without updating app/web/onboarding.py): {missing}"
    )



def test_every_step_has_an_icon() -> None:
    """Each step renders a wayfinding glyph in the card header — keep the set
    complete so the UI never shows a blank icon slot."""
    missing = [s.key for s in ONBOARDING_STEPS if not s.icon]
    assert not missing, f"onboarding steps missing an icon: {missing}"


def test_every_step_has_substantive_tips() -> None:
    """The guide's substance lives in the per-step bullets. Guard that every
    step carries at least two concrete tips so the tour stays informative and
    nobody trims it back down to one-line spotlights."""
    thin = [s.key for s in ONBOARDING_STEPS if len(s.tips) < 2]
    assert not thin, f"onboarding steps with too few tips (need >=2): {thin}"
    # Tips must be real strings, not blanks.
    blank = [s.key for s in ONBOARDING_STEPS if any(not t.strip() for t in s.tips)]
    assert not blank, f"onboarding steps with empty tip bullets: {blank}"


def test_audience_filtering_splits_admin_and_non_admin() -> None:
    admin_keys = {s.key for s in ONBOARDING_STEPS if s.audience == "admin"}
    assert admin_keys, "expected at least one admin-only step to exercise the split"

    non_admin = {s["key"] for s in steps_for(is_admin=False)}
    admin = {s["key"] for s in steps_for(is_admin=True)}

    # Admin-only steps are hidden from non-admins and shown to admins.
    assert not (admin_keys & non_admin), "admin-only steps leaked to non-admins"
    assert admin_keys <= admin, "admin-only steps missing for admins"
    # `all` steps appear for everyone.
    all_keys = {s.key for s in ONBOARDING_STEPS if s.audience == "all"}
    assert all_keys <= non_admin and all_keys <= admin


def test_is_admin_is_coerced_to_bool() -> None:
    """Jinja hands `session.user.is_admin`, which for an anonymous/missing
    user is a falsy _SilentUndefined — steps_for must treat it as non-admin
    rather than crash."""
    class _Falsy:
        def __bool__(self) -> bool:
            return False

    keys = {s["key"] for s in steps_for(is_admin=_Falsy())}
    admin_keys = {s.key for s in ONBOARDING_STEPS if s.audience == "admin"}
    assert not (admin_keys & keys)


def test_steps_are_server_injected_not_hardcoded_in_js() -> None:
    """Single-source-of-truth guard: the partial feeds steps via the
    `onboarding_steps` Jinja global, and the engine reads them from the
    injected JSON — not a hardcoded array."""
    partial = (TEMPLATES / "_tour.html").read_text(encoding="utf-8")
    assert "onboarding_steps(" in partial, "_tour.html must inject server-filtered steps"
    assert 'id="agnesOnboardingSteps"' in partial

    engine = (STATIC / "js" / "tour.js").read_text(encoding="utf-8")
    assert "agnesOnboardingSteps" in engine, "tour.js must read the injected steps"


def test_engine_resumes_across_page_navigation() -> None:
    """The cross-page walk relies on the engine stashing its position in
    sessionStorage before navigating and resuming on the next page — guard
    that wiring so a refactor can't silently turn it back into an in-place
    tour that never leaves the current page."""
    engine = (STATIC / "js" / "tour.js").read_text(encoding="utf-8")
    assert "sessionStorage" in engine, "tour.js must persist its resume position"
    assert "window.location.assign" in engine, "tour.js must navigate between step pages"


def test_reopen_launcher_present_in_header() -> None:
    """The tour must be re-openable via the (?) help icon in the nav header."""
    header = (TEMPLATES / "_app_header.html").read_text(encoding="utf-8")
    assert "data-tour-start" in header, "header is missing the tour launcher"
