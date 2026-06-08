"""Canonical onboarding / product-tour steps — the single source of truth.

Both ends of the onboarding feature read this module, which is what keeps
the guided tour from silently going stale as the UI churns:

  • The **frontend** never hardcodes steps. The server filters this list by
    audience (admin vs non-admin) and injects the result as JSON into the
    page (`_tour.html` → ``onboarding_steps`` Jinja global); the engine
    (`static/js/tour.js`) just renders whatever it receives.

  • The **contract test** (`tests/test_onboarding_not_outdated.py`) imports
    ``ONBOARDING_STEPS`` and asserts every step still points at a registered
    route and a DOM anchor that exists in the templates. Delete a nav item
    or rename its ``data-tour`` anchor and the test goes red — the tour can't
    drift out of sync with the app without CI noticing.

Keep the content generic and vendor-agnostic: this is the OSS distribution,
and the tour must read sensibly on any instance regardless of branding,
data source, or which optional features are enabled. Steps gated on a
feature that may be absent for a given viewer (e.g. Chat) are dropped at
render time when their DOM anchor isn't present — see tour.js.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

Audience = Literal["all", "admin", "non_admin"]


@dataclass(frozen=True)
class OnboardingStep:
    """One step of the guided tour.

    ``anchor`` is the ``data-tour="<anchor>"`` element the spotlight lands
    on; ``None`` renders a centered card with no target (closing card).
    ``route`` is the registered path the anchor links to — the contract
    test asserts it's still routable. ``audience`` controls who sees the
    step; the server filters before the step ever reaches the browser.
    """

    key: str
    title: str
    body: str
    anchor: str | None = None
    route: str | None = None
    audience: Audience = "all"


# Order matters — this is the walk order. The intro consent modal (rendered
# in _tour.html) is the "welcome", so the first step here is the first
# spotlight; the last is a target-less closing card.
ONBOARDING_STEPS: tuple[OnboardingStep, ...] = (
    OnboardingStep(
        key="home",
        anchor="nav-home",
        route="/dashboard",
        title="Home",
        body="Your starting point — an overview of what's available to you and "
        "shortcuts to get going.",
    ),
    OnboardingStep(
        key="chat",
        anchor="nav-chat",
        route="/chat",
        title="Chat",
        body="Ask questions about your data in natural language, right in the "
        "browser. (Shown when chat is enabled for you.)",
    ),
    OnboardingStep(
        key="marketplace",
        anchor="nav-marketplace",
        route="/marketplace",
        title="Marketplace",
        body="Discover skills and plugins that extend what your AI agent can "
        "do, and install them into your workspace.",
    ),
    OnboardingStep(
        key="catalog",
        anchor="nav-catalog",
        route="/catalog",
        title="Data Packages",
        body="Browse the datasets you have access to. Each package shows its "
        "tables, schema, and how to query it locally.",
    ),
    OnboardingStep(
        key="memory",
        anchor="nav-memory",
        route="/corporate-memory",
        title="Memory",
        body="Shared organizational knowledge — canonical metric definitions "
        "and business rules your agent should follow.",
    ),
    OnboardingStep(
        key="admin",
        anchor="nav-admin",
        route="/admin/tables",
        title="Admin tools",
        body="As an admin, this menu is your control center — manage tables, "
        "users and access, data sync, MCP sources, and server settings.",
        audience="admin",
    ),
    OnboardingStep(
        key="profile",
        anchor="user-menu",
        route="/me/profile",
        title="Your menu",
        body="Your profile, AI Cowork setup, recent activity, and sign-out "
        "live here. You can reopen this tour from your profile anytime.",
    ),
    OnboardingStep(
        key="done",
        title="You're all set",
        body="That's the lay of the land. Reopen this tour anytime from the "
        "help icon in the header or from your profile page.",
    ),
)


def steps_for(is_admin: object) -> list[dict]:
    """Return the steps a given viewer should see, as plain dicts ready for
    JSON serialization. ``is_admin`` is coerced to bool so a Jinja
    ``_SilentUndefined`` (anonymous / missing) safely reads as non-admin.

    Audience rules: ``all`` always; ``admin`` only for admins; ``non_admin``
    only for non-admins. Feature-gated steps whose DOM anchor is absent for
    the viewer (e.g. Chat without a grant) are dropped client-side, so they
    can stay in this list unconditionally.
    """
    admin = bool(is_admin)
    out: list[dict] = []
    for step in ONBOARDING_STEPS:
        if step.audience == "admin" and not admin:
            continue
        if step.audience == "non_admin" and admin:
            continue
        out.append(asdict(step))
    return out
