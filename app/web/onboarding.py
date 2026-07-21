"""Canonical onboarding / product-tour steps — the single source of truth.

Both ends of the onboarding feature read this module, which is what keeps
the guided tour from silently going stale as the UI churns:

  • The **frontend** never hardcodes steps. The server filters this list by
    audience (admin vs non-admin) and injects the result as JSON into the
    page (`_tour.html` → ``onboarding_steps`` Jinja global); the engine
    (`static/js/tour.js`) just renders whatever it receives.

  • The **contract test** (`tests/test_onboarding_not_outdated.py`) imports
    ``ONBOARDING_STEPS`` and asserts every step still points at a DOM anchor
    that exists in the templates. Delete a nav item or rename its anchor and
    the test goes red — the tour can't drift out of sync with the app without
    CI noticing.

All tour steps spotlight nav items. The nav header is present on every
authenticated page, so every step renders in place on whatever page the user
is on when they start the tour — no cross-page navigation. ``route`` is left
as an optional field for future use but is intentionally ``None`` on all
current steps.

Keep the content generic and vendor-agnostic: this is the OSS distribution,
and the tour must read sensibly on any instance regardless of branding,
data source, or which optional features are enabled. Steps gated on a
feature that may be absent for a given viewer (e.g. Chat) are dropped at
render time when their DOM anchor isn't present — see tour.js.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Audience = Literal["all", "admin", "non_admin"]


@dataclass(frozen=True)
class OnboardingStep:
    """One step of the guided tour.

    ``anchor`` is the ``data-tour="<anchor>"`` element the spotlight lands
    on; ``None`` renders a centered card with no target (closing card).
    ``route`` is reserved for future use — all current steps set it to
    ``None`` because every anchor lives in the nav header, which is present
    on every authenticated page. ``icon`` is a short, vendor-agnostic glyph
    shown in the card header for visual wayfinding. ``tips`` is an optional
    list of concrete "what you can do here" bullets shown under the body.
    ``audience`` controls who sees the step; the server filters before the
    step ever reaches the browser.
    """

    key: str
    title: str
    body: str
    icon: str = ""
    tips: tuple[str, ...] = field(default_factory=tuple)
    anchor: str | None = None
    route: str | None = None
    audience: Audience = "all"
    # Which chrome layout(s) this step applies to. The rail (ui_layout=rail)
    # and the topnav header expose different nav anchors — e.g. rail folds
    # Marketplace/Memory under Catalog and adds a top-level "My Stack" — so a
    # step whose anchor only exists in one chrome must be scoped to it, or its
    # spotlight lands on nothing in the other layout. Default: both.
    layouts: tuple[str, ...] = ("topnav", "rail")


# Order matters — this is the walk order. The intro consent modal (rendered
# in _tour.html) is the "welcome", so the first step here is the first
# spotlight; the last is a target-less closing card.
ONBOARDING_STEPS: tuple[OnboardingStep, ...] = (
    OnboardingStep(
        key="home",
        icon="🏠",
        anchor="nav-home",
        title="Home base",
        body="Your starting point — a live overview of everything you can "
        "access and shortcuts to get going. You'll come back here often.",
        tips=(
            "See the data, skills, and tools your account is granted, at a glance.",
            "Jump straight into a task from the shortcut cards.",
            "Status here always reflects your access — nothing you can't use.",
        ),
    ),
    OnboardingStep(
        key="chat",
        icon="💬",
        anchor="nav-chat",
        title="Chat with your data",
        body="Ask questions in plain language and get answers grounded in your own datasets — no SQL required.",
        tips=(
            "Try “How many active accounts did we have last month?”",
            "Answers cite the tables they used, so you can trust and verify them.",
            "Follow up in the same thread — it remembers the context.",
            "Shown when an admin has enabled chat for your group.",
        ),
    ),
    OnboardingStep(
        key="stack",
        icon="🗂️",
        anchor="nav-stack",
        title="Your stack & uploads",
        body="My Stack is your personal workspace — the data packages and "
        "plugins you've added, plus files you upload yourself.",
        tips=(
            "Add “+ New upload” to bring your own files in — they stay private to you.",
            "Uploaded files are indexed so your agents can search across them.",
            "Everything you add from the Catalog lands here for quick access.",
        ),
        # Rail-only: "My Stack" is a top-level rail destination; the topnav
        # header has no equivalent nav item (its anchor is nav-library).
        layouts=("rail",),
    ),
    OnboardingStep(
        key="marketplace",
        icon="🧩",
        anchor="nav-marketplace",
        title="Extend your agent",
        body="The Marketplace is where you discover skills and plugins that "
        "teach your AI agent new tricks, then install them into your workspace.",
        tips=(
            "Browse skills & plugins curated and approved for your organization.",
            "Install into your Claude Code workspace in one click.",
            "New items appear here as admins publish them — check back now and then.",
        ),
        # Topnav-only: the rail folds Marketplace under the Catalog destination,
        # so there is no standalone nav-marketplace anchor to spotlight there.
        layouts=("topnav",),
    ),
    OnboardingStep(
        key="catalog",
        icon="📦",
        anchor="nav-catalog",
        title="Browse your data",
        body="Every dataset you're granted shows up here as a package — with its "
        "tables, schema, and copy-paste instructions for querying it locally.",
        tips=(
            "Open a package to see its tables, columns, and data types.",
            "Copy ready-to-run query snippets for local analysis.",
            "Big remote tables stay in the warehouse — pull just a filtered slice.",
            "Only datasets your groups can access are listed.",
        ),
    ),
    OnboardingStep(
        key="memory",
        icon="🧠",
        anchor="nav-memory",
        title="Shared knowledge",
        body="Canonical metric definitions and business rules your agent should "
        "follow live here — so everyone (and every agent) computes the same way.",
        tips=(
            "Look up the agreed definition of metrics like MRR, churn, or NPS.",
            "Read the business rules your AI agent is expected to honor.",
            "One source of truth means consistent numbers across the org.",
        ),
        # Topnav-only: the rail folds Memory under the Catalog destination,
        # so there is no standalone nav-memory anchor to spotlight there.
        layouts=("topnav",),
    ),
    OnboardingStep(
        key="search",
        icon="🔍",
        anchor="nav-search",
        title="Search everything",
        body="One box that searches across all of it — datasets in the catalog, "
        "shared knowledge, and uploaded documents. Every result says where it lives.",
        tips=(
            "Type a couple of characters — results group into Tables, Knowledge, and Documents.",
            "Table hits link straight to the catalog detail so you can start querying.",
            "You only ever see results your account is granted.",
        ),
    ),
    OnboardingStep(
        key="admin",
        icon="⚙️",
        anchor="nav-admin",
        title="Admin control center",
        body="As an admin, this menu is mission control for the whole instance.",
        tips=(
            "Register tables and trigger or schedule data sync.",
            "Manage users, groups, and access grants (RBAC).",
            "Configure MCP sources, the marketplace, and server settings.",
        ),
        audience="admin",
    ),
    OnboardingStep(
        key="profile",
        icon="👤",
        anchor="user-menu",
        title="Your menu",
        body="Your profile, AI Connector setup, recent activity, and sign-out live here.",
        tips=(
            "Set up AI Connector to analyze your data locally with Claude Code.",
            "Review your recent activity and manage access tokens.",
            "Reopen this tour anytime from here or the help icon in the header.",
        ),
    ),
    OnboardingStep(
        key="done",
        icon="✅",
        title="You're all set",
        body="That's the lay of the land. Reopen this tour whenever you like from "
        "the help icon in the header or your profile page.",
        tips=(
            "Tip: most pages show only what your account can access.",
            "Stuck? The help icon (top-right) restarts this tour from the top.",
        ),
    ),
)


def steps_for(is_admin: object, layout: object = "topnav") -> list[dict]:
    """Return the steps a given viewer should see, as plain dicts ready for
    JSON serialization. ``is_admin`` is coerced to bool so a Jinja
    ``_SilentUndefined`` (anonymous / missing) safely reads as non-admin.

    ``layout`` is the active chrome (``topnav`` / ``rail``); a Jinja
    ``_SilentUndefined`` or any unknown value falls back to ``topnav``. Steps
    whose anchor only exists in one chrome are scoped via ``OnboardingStep.layouts``
    so the spotlight never lands on a nav item the current layout doesn't render.

    Audience rules: ``all`` always; ``admin`` only for admins; ``non_admin``
    only for non-admins. Feature-gated steps whose DOM anchor is absent for
    the viewer (e.g. Chat without a grant) are still dropped client-side.
    """
    admin = bool(is_admin)
    lyt = str(layout) if str(layout) in ("topnav", "rail") else "topnav"
    out: list[dict] = []
    for step in ONBOARDING_STEPS:
        if step.audience == "admin" and not admin:
            continue
        if step.audience == "non_admin" and admin:
            continue
        if lyt not in step.layouts:
            continue
        out.append(asdict(step))
    return out
