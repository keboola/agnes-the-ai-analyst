"""Remove ``from src.repositories.<mod> import XxxRepository`` lines
that the bulk swap missed because they were INSIDE a function body
(indented) rather than at module top-level. Also rewrites any
``XxxRepository(<conn-ish>)`` lingering near them to factory calls.

The factory imports themselves stay in the file from the earlier
sweep — this just deletes the dead inline imports + any class-name
references they unblock.
"""
from __future__ import annotations

import re
from pathlib import Path


_CLASS_TO_FACTORY = {
    "UserRepository": "users_repo",
    "UserGroupsRepository": "user_groups_repo",
    "UserGroupMembersRepository": "user_group_members_repo",
    "ResourceGrantsRepository": "resource_grants_repo",
    "AuditRepository": "audit_repo",
    "TableRegistryRepository": "table_registry_repo",
    "SyncStateRepository": "sync_state_repo",
    "MetricRepository": "metric_repo",
    "ClaudeMdTemplateRepository": "claude_md_template_repo",
    "WelcomeTemplateRepository": "welcome_template_repo",
    "NewsTemplateRepository": "news_template_repo",
    "AccessTokenRepository": "access_token_repo",
    "ProfileRepository": "profile_repo",
    "ViewOwnershipRepository": "view_ownership_repo",
    "ColumnMetadataRepository": "column_metadata_repo",
    "BqMetadataCacheRepository": "bq_metadata_cache_repo",
    "SyncSettingsRepository": "sync_settings_repo",
    "TelegramRepository": "notifications_telegram_repo",
    "PendingCodeRepository": "notifications_pending_code_repo",
    "ScriptRepository": "notifications_script_repo",
    "SessionProcessorStateRepository": "session_processor_state_repo",
    "ObservabilityViewsRepository": "observability_views_repo",
    "UsageRepository": "usage_repo",
    "MarketplaceRegistryRepository": "marketplace_registry_repo",
    "MarketplacePluginsRepository": "marketplace_plugins_repo",
    "StoreEntitiesRepository": "store_entities_repo",
    "UserStoreInstallsRepository": "user_store_installs_repo",
    "UserCuratedSubscriptionsRepository": "user_curated_subscriptions_repo",
    "StoreSubmissionsRepository": "store_submissions_repo",
    "KnowledgeRepository": "knowledge_repo",
}


def _swap(path: Path) -> int:
    text = path.read_text()
    original = text

    # 1. Strip inline function-local imports of repo classes.
    #    Matches `<indent>from src.repositories.<mod> import (<class>(, <class>)*)`
    #    Multi-class on one line is split.
    out_lines: list[str] = []
    i = 0
    lines = text.splitlines(keepends=True)
    while i < len(lines):
        line = lines[i]
        m = re.match(
            r"^(\s*)from src\.repositories\.[A-Za-z_]+ import\s+(?:\(\s*)?([A-Za-z_, \n]+)(?:\s*\))?\n",
            line,
        )
        # Multi-line `from ... import (\n  A,\n  B,\n)` form
        if line.lstrip().startswith("from src.repositories.") and "(" in line and ")" not in line:
            # Capture until close paren
            j = i + 1
            while j < len(lines) and ")" not in lines[j]:
                j += 1
            # Skip the whole block
            i = j + 1
            continue
        if m:
            classes = [c.strip() for c in m.group(2).split(",") if c.strip()]
            if any(c in _CLASS_TO_FACTORY for c in classes):
                # Drop the line entirely
                i += 1
                continue
        out_lines.append(line)
        i += 1
    text = "".join(out_lines)

    # 2. Rewrite any leftover ``XxxRepository(arg)`` to factory().
    for cls, factory in _CLASS_TO_FACTORY.items():
        text = re.sub(
            rf"{cls}\([a-zA-Z_][a-zA-Z_0-9.]*\)",
            f"{factory}()",
            text,
        )
        # Also catch the no-arg path that occasionally appears
        text = re.sub(rf"{cls}\(\)", f"{factory}()", text)

    # 3. Rewrite type hints / bare references ``repo: XxxRepository`` →
    #    just drop the annotation (let it be inferred / kept untyped).
    for cls in _CLASS_TO_FACTORY:
        text = re.sub(rf":\s*{cls}\b", "", text)

    # 4. Strip explicit `XxxRepository._row_to_dict(...)` static calls — they
    #    were class-static; the factory has no such method. Replace with
    #    a noop pass-through (caller must adapt) — emit a TODO comment.
    for cls in _CLASS_TO_FACTORY:
        if f"{cls}._" in text or f"{cls}.staticmethod" in text:
            text = re.sub(
                rf"{cls}\._row_to_dict\(.*?\)",
                "{}  # TODO _row_to_dict no longer accessible from a factory; refactor",
                text,
                flags=re.DOTALL,
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
