"""Authoring-agent chat profiles.

A profile shapes a chat session into a specialized authoring assistant by
supplying (a) a persona ``CLAUDE.md`` that replaces the generic analyst data
rails, and (b) a read-only knowledge skill describing how the target domain
works in Agnes. Profiles are spawn-time only — they materialize into the
per-session workdir (see ``WorkdirManager.prepare_session_dir``) and are not
persisted, so adding one needs no schema migration.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatProfile:
    slug: str
    claude_md: str  # replaces the session CLAUDE.md
    skill_name: str  # .claude/skills/<skill_name>/SKILL.md
    skill_body: str  # full SKILL.md (frontmatter + body)


_DATA_PACKAGE_BUILDER = ChatProfile(
    slug="data-package-builder",
    claude_md=(
        "# Data Package Builder\n\n"
        "You help an admin assemble a **data package** — a curated bundle of "
        "tables (and later metrics) granted to a user group — in Agnes.\n\n"
        "Rules:\n"
        "- Ground every suggestion in the instance's real state: run `agnes "
        "catalog --json` to see available tables before proposing any.\n"
        "- Check for an existing near-duplicate package before proposing a new "
        "one; suggest editing it instead if found.\n"
        "- Propose; never claim a package is created until the admin clicks "
        "Create in the builder UI.\n"
        "- Use the `agnes-data-package` skill for the exact model and endpoints.\n"
    ),
    skill_name="agnes-data-package",
    skill_body=(
        "---\n"
        "name: agnes-data-package\n"
        "description: How data packages work in Agnes — model, the catalog, "
        "and the admin endpoints used to assemble and grant one.\n"
        "---\n\n"
        "# Data packages in Agnes\n\n"
        "A data package = `data_packages` row + `data_package_tables` (M:N to "
        "`table_registry`) + a `resource_grant` to a user group.\n\n"
        "## Read the real state first\n"
        "- `agnes catalog --json` — available tables (id, query_mode, size).\n"
        "- `agnes schema <table_id>` — columns + types.\n\n"
        "## Assemble (admin endpoints)\n"
        "- `POST /api/admin/data-packages` — create `{name, slug, description}`.\n"
        "- `POST /api/admin/data-packages/{id}/tables` — add a table.\n"
        "- `POST /api/admin/grants` — grant the package to a group.\n\n"
        "Local tables (`query_mode` local/materialized) sync to analysts via "
        "`agnes pull`; `remote` tables stay server-side.\n"
    ),
)

_PROFILES: dict[str, ChatProfile] = {
    _DATA_PACKAGE_BUILDER.slug: _DATA_PACKAGE_BUILDER,
}


def get_profile(slug: str) -> ChatProfile | None:
    """Return the profile for ``slug`` or ``None`` if unknown."""
    return _PROFILES.get(slug)
