"""One-time mechanical swap: DuckDB ``XxxRepository(conn)`` → factory call.

Run from the repo root:

    python scripts/_swap_repos_to_factory.py

Touches every .py under app/, services/, cli/ that still instantiates a
repository class directly. After running, the constructor-style calls
are gone — every callsite gets the right backend via
``src.repositories.<name>_repo()``.

Imports of the form ``from src.repositories.<module> import XxxRepository``
are rewritten to ``from src.repositories import <name>_repo``.

NOT idempotent if you mid-edit a file — re-running on a file that has
already been swapped leaves it alone.
"""
from __future__ import annotations

import re
from pathlib import Path


# (DuckDB class name, factory func name, src module name)
_MAPPING = [
    ("UserRepository", "users_repo", "users"),
    ("UserGroupsRepository", "user_groups_repo", "user_groups"),
    ("UserGroupMembersRepository", "user_group_members_repo", "user_group_members"),
    ("ResourceGrantsRepository", "resource_grants_repo", "resource_grants"),
    ("AuditRepository", "audit_repo", "audit"),
    ("TableRegistryRepository", "table_registry_repo", "table_registry"),
    ("SyncStateRepository", "sync_state_repo", "sync_state"),
    ("MetricRepository", "metric_repo", "metrics"),
    ("ClaudeMdTemplateRepository", "claude_md_template_repo", "claude_md_template"),
    ("WelcomeTemplateRepository", "welcome_template_repo", "welcome_template"),
    ("NewsTemplateRepository", "news_template_repo", "news_template"),
    ("AccessTokenRepository", "access_token_repo", "access_tokens"),
    ("ProfileRepository", "profile_repo", "profiles"),
    ("ViewOwnershipRepository", "view_ownership_repo", "view_ownership"),
    ("ColumnMetadataRepository", "column_metadata_repo", "column_metadata"),
    ("BqMetadataCacheRepository", "bq_metadata_cache_repo", "bq_metadata_cache"),
    ("SyncSettingsRepository", "sync_settings_repo", "sync_settings"),
    ("TelegramRepository", "notifications_telegram_repo", "notifications"),
    ("PendingCodeRepository", "notifications_pending_code_repo", "notifications"),
    ("ScriptRepository", "notifications_script_repo", "notifications"),
    ("SessionProcessorStateRepository", "session_processor_state_repo", "session_processor_state"),
    ("ObservabilityViewsRepository", "observability_views_repo", "observability_views"),
    ("UsageRepository", "usage_repo", "usage"),
    ("MarketplaceRegistryRepository", "marketplace_registry_repo", "marketplace_registry"),
    ("MarketplacePluginsRepository", "marketplace_plugins_repo", "marketplace_plugins"),
    ("StoreEntitiesRepository", "store_entities_repo", "store_entities"),
    ("UserStoreInstallsRepository", "user_store_installs_repo", "user_store_installs"),
    ("UserCuratedSubscriptionsRepository", "user_curated_subscriptions_repo", "user_curated_subscriptions"),
    ("StoreSubmissionsRepository", "store_submissions_repo", "store_submissions"),
    ("KnowledgeRepository", "knowledge_repo", "knowledge"),
]


def _swap_file(path: Path) -> tuple[int, list[str]]:
    """Return (changes_count, list_of_factory_names_needed_for_import)."""
    text = path.read_text()
    original = text
    needed: set[str] = set()
    for cls, factory, _ in _MAPPING:
        # Constructor-style call ``XxxRepository(conn)`` → ``factory_name()``.
        pattern = rf"{cls}\(conn\)"
        new_text = re.sub(pattern, f"{factory}()", text)
        if new_text != text:
            needed.add(factory)
            text = new_text

        # Also handle direct ``XxxRepository(something_else_named_conn)``
        # by leaving them alone — only ``(conn)`` is the canonical sweep.

    if text == original:
        return 0, []

    # Rewrite imports.
    new_text = _rewrite_imports(text, needed)
    path.write_text(new_text)
    return len(original.splitlines()) - len(new_text.splitlines()), sorted(needed)


def _rewrite_imports(text: str, needed: set[str]) -> str:
    """Rewrite ``from src.repositories.<mod> import XxxRepository`` lines.

    Replace each with a `from src.repositories import <factory>` line.
    Deduplicates if multiple factories from the same package are needed.
    """
    if not needed:
        return text

    factory_names = sorted(needed)
    new_lines: list[str] = []
    inserted_factory_import = False
    for line in text.splitlines(keepends=True):
        m = re.match(
            r"^from src\.repositories\.[A-Za-z_]+ import ([A-Za-z_, \n\(\)]+)",
            line,
        )
        # Drop legacy individual-class imports for any class that we swapped.
        # (May leave multi-line imports partially intact; fall back to skip.)
        if m:
            classes = m.group(1)
            target_classes = [
                cls for cls, _, _ in _MAPPING if cls in classes
            ]
            if target_classes:
                continue
        new_lines.append(line)
        if not inserted_factory_import and line.startswith("from app.") or line.startswith("import "):
            # placeholder anchor — we'll add the factory line after this block ends
            pass

    out = "".join(new_lines)

    # Inject the factory import near the top of the file.
    factory_import = (
        "from src.repositories import (\n    "
        + ",\n    ".join(factory_names)
        + ",\n)\n"
    )
    # Place it right after the last top-level import line we can find.
    lines = out.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("from ") or line.startswith("import "):
            insert_at = i + 1
    lines.insert(insert_at, factory_import)
    return "".join(lines)


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    changed = 0
    for root in ("app", "services", "cli"):
        for path in (repo_root / root).rglob("*.py"):
            count, needed = _swap_file(path)
            if needed:
                changed += 1
                print(f"  {path.relative_to(repo_root)} → +{', '.join(needed)}")
    print(f"\nSwapped {changed} files.")


if __name__ == "__main__":
    main()
