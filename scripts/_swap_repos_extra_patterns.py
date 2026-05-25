"""Catch-up swap for ``XxxRepository(arbitrary_conn_var)`` patterns the
first pass missed (it only matched ``(conn)``). Sweeps app/ services/ cli/.

Each match is mechanically rewritten to its factory-function call
(no argument), e.g.::

    AuditRepository(_audit_conn).log(...)  →  audit_repo().log(...)

Existing factory imports stay untouched. No new imports added — if a
file was missing a factory import after the first pass, this script
won't help; run ``_normalize_factory_imports.py`` after.
"""
from __future__ import annotations

import re
from pathlib import Path


_MAPPING = [
    ("UserRepository", "users_repo"),
    ("UserGroupsRepository", "user_groups_repo"),
    ("UserGroupMembersRepository", "user_group_members_repo"),
    ("ResourceGrantsRepository", "resource_grants_repo"),
    ("AuditRepository", "audit_repo"),
    ("TableRegistryRepository", "table_registry_repo"),
    ("SyncStateRepository", "sync_state_repo"),
    ("MetricRepository", "metric_repo"),
    ("ClaudeMdTemplateRepository", "claude_md_template_repo"),
    ("WelcomeTemplateRepository", "welcome_template_repo"),
    ("NewsTemplateRepository", "news_template_repo"),
    ("AccessTokenRepository", "access_token_repo"),
    ("ProfileRepository", "profile_repo"),
    ("ViewOwnershipRepository", "view_ownership_repo"),
    ("ColumnMetadataRepository", "column_metadata_repo"),
    ("BqMetadataCacheRepository", "bq_metadata_cache_repo"),
    ("SyncSettingsRepository", "sync_settings_repo"),
    ("TelegramRepository", "notifications_telegram_repo"),
    ("PendingCodeRepository", "notifications_pending_code_repo"),
    ("ScriptRepository", "notifications_script_repo"),
    ("SessionProcessorStateRepository", "session_processor_state_repo"),
    ("ObservabilityViewsRepository", "observability_views_repo"),
    ("UsageRepository", "usage_repo"),
    ("MarketplaceRegistryRepository", "marketplace_registry_repo"),
    ("MarketplacePluginsRepository", "marketplace_plugins_repo"),
    ("StoreEntitiesRepository", "store_entities_repo"),
    ("UserStoreInstallsRepository", "user_store_installs_repo"),
    ("UserCuratedSubscriptionsRepository", "user_curated_subscriptions_repo"),
    ("StoreSubmissionsRepository", "store_submissions_repo"),
    ("KnowledgeRepository", "knowledge_repo"),
]


def _swap(path: Path) -> int:
    text = path.read_text()
    original = text
    for cls, factory in _MAPPING:
        # Any single-arg constructor call: XxxRepository(<one identifier>)
        # — replace with factory(). Excludes empty parens to keep
        # already-swapped lines alone.
        text = re.sub(
            rf"{cls}\([a-zA-Z_][a-zA-Z_0-9.]*\)",
            f"{factory}()",
            text,
        )
    if text != original:
        path.write_text(text)
        return 1
    return 0


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    changed = 0
    for root in ("app", "services", "cli"):
        for path in (repo_root / root).rglob("*.py"):
            if _swap(path):
                changed += 1
                print(f"  swapped {path.relative_to(repo_root)}")
    print(f"\nSwapped {changed} files.")


if __name__ == "__main__":
    main()
