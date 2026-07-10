"""Authoring-agent studio domains.

Each domain maps a builder page (`/admin/studio/<slug>`) to: a chat profile
(see ``app/chat/profiles.py``), the form fields the builder renders, and the
existing admin endpoint its Create action POSTs to. The page is generic — the
domain config drives the fields, the assistant profile, and the create call —
so all four authoring agents share one tested surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.store_categories import STORE_CATEGORIES


@dataclass(frozen=True)
class StudioField:
    key: str
    label: str
    type: str = "text"  # text | textarea | select
    placeholder: str = ""
    required: bool = False
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class StudioDomain:
    slug: str
    profile: str  # chat profile slug
    title: str
    subtitle: str
    endpoint: str  # admin endpoint the Create action POSTs to
    # True → the domain has its own moderation pipeline (e.g. the store's
    # guardrail + LLM review); EVERYONE posts directly to `endpoint` and the
    # authoring_suggestions queue rejects it (no _SAFE_REPLAY exists).
    submit_directly: bool = False
    fields: tuple[StudioField, ...] = field(default_factory=tuple)


_NAME = StudioField("name", "Name", required=True, placeholder="Finance — Controlling Q3")
_SLUG = StudioField("slug", "Slug", required=True, placeholder="finance-controlling-q3")
_DESC = StudioField("description", "Description", type="textarea", placeholder="What this is for.")

STUDIO_DOMAINS: dict[str, StudioDomain] = {
    "data-package": StudioDomain(
        slug="data-package",
        profile="data-package-builder",
        title="Data Package Builder",
        subtitle="Assemble a curated bundle of tables and grant it to a group.",
        endpoint="/api/admin/data-packages",
        fields=(_NAME, _SLUG, _DESC),
    ),
    "mcp": StudioDomain(
        slug="mcp",
        profile="mcp-connect",
        title="MCP Connection Builder",
        subtitle="Connect an external MCP server and grant its tools to a group.",
        endpoint="/api/admin/mcp-sources",
        fields=(
            StudioField(
                "name",
                "Name",
                required=True,
                placeholder="acme_tools",
            ),
            StudioField(
                "transport",
                "Transport",
                type="select",
                required=True,
                options=("http", "sse", "stdio"),
            ),
            StudioField("url", "URL", placeholder="https://mcp.example.com/sse"),
        ),
    ),
    "marketplace": StudioDomain(
        slug="marketplace",
        profile="marketplace-author",
        title="Marketplace Builder",
        subtitle="Register a curated marketplace (a git repo of skills/agents/plugins).",
        endpoint="/api/marketplaces",
        fields=(
            StudioField("name", "Name", required=True, placeholder="Engineering Skills"),
            StudioField("slug", "Slug", required=True, placeholder="engineering-skills"),
            StudioField("url", "Git URL", required=True, placeholder="https://github.com/org/repo"),
            StudioField("curator_name", "Curator", required=True, placeholder="Platform Team"),
            StudioField("curator_email", "Curator email", required=True, placeholder="team@example.com"),
        ),
    ),
    "corporate-memory": StudioDomain(
        slug="corporate-memory",
        profile="corporate-memory",
        title="Corporate Memory Builder",
        subtitle="Distill reusable knowledge into a memory domain granted to a group.",
        endpoint="/api/admin/memory-domains",
        fields=(_NAME, _SLUG, _DESC),
    ),
    "skill": StudioDomain(
        slug="skill",
        profile="skill-author",
        title="Skill Builder",
        subtitle="Author a reusable skill and publish it to the store.",
        endpoint="/api/store/entities/from-markdown",
        submit_directly=True,
        fields=(
            StudioField(
                "name",
                "Name",
                required=True,
                placeholder="quarterly-report-recipe",
            ),
            StudioField(
                "description",
                "Description",
                type="textarea",
                required=True,
                placeholder="Use when … (the trigger that tells an agent to load this skill).",
            ),
            StudioField(
                "category",
                "Category",
                type="select",
                options=tuple(["", *STORE_CATEGORIES]),
            ),
            StudioField(
                "skill_md",
                "Skill content (Markdown)",
                type="textarea",
                required=True,
                placeholder="Step-by-step instructions an AI agent should follow…",
            ),
        ),
    ),
}


def get_domain(slug: str) -> StudioDomain | None:
    return STUDIO_DOMAINS.get(slug)
