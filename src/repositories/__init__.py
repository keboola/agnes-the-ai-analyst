"""Repository factory — backend selection lives here, not at the callsites.

Each ``<name>_repo()`` function returns a ready-to-use repository instance:

* ``DATABASE_URL`` (or legacy ``AGNES_DB_URL``) unset  → DuckDB ``system.duckdb`` repos (legacy, default)
* ``DATABASE_URL`` (or legacy ``AGNES_DB_URL``) set    → Postgres-backed ``*_pg`` repos

Callsites import factory functions instead of repository classes:

    # Before
    from src.repositories.users import UserRepository
    repo = UserRepository(conn)

    # After
    from src.repositories import users_repo
    repo = users_repo()

The choice is computed per-call, so a process that flips the env var in
tests (via ``monkeypatch.setenv``) immediately routes to the new
backend. DuckDB repos accept a fresh cursor on the singleton system DB;
PG repos accept the singleton engine.

We DO NOT mix backends within a request: every factory consults the
same env var, so within one request all repos resolve to the same
side. Cross-repo transactional guarantees are then identical to the
pre-existing single-conn behaviour.
"""
from __future__ import annotations

import os
from typing import Any

# Re-exports of the legacy DuckDB connection helpers — many callers
# still ``from src.repositories import get_system_db``. Keep them here
# until those imports are migrated to call the factory directly.
from src.db import get_analytics_db, get_system_db

__all__ = [
    "get_system_db",
    "get_analytics_db",
    "use_pg",
    # Core user / RBAC cluster
    "users_repo",
    "user_groups_repo",
    "user_group_members_repo",
    "resource_grants_repo",
    "audit_repo",
    # Ops cluster
    "table_registry_repo",
    "sync_state_repo",
    # Config / templates / tokens
    "metric_repo",
    "claude_md_template_repo",
    "welcome_template_repo",
    "news_template_repo",
    "access_token_repo",
    "profile_repo",
    # Lookup / cache
    "view_ownership_repo",
    "column_metadata_repo",
    "bq_metadata_cache_repo",
    "sync_settings_repo",
    "notifications_telegram_repo",
    "notifications_pending_code_repo",
    "notifications_script_repo",
    # Telemetry
    "session_processor_state_repo",
    "observability_views_repo",
    "usage_repo",
    # Store / marketplace
    "marketplace_registry_repo",
    "marketplace_plugins_repo",
    "store_entities_repo",
    "user_store_installs_repo",
    "user_curated_subscriptions_repo",
    "store_submissions_repo",
    # Knowledge
    "knowledge_repo",
    # Data packages / memory / recipes / subscriptions
    "data_packages_repo",
    "memory_domain_suggestions_repo",
    "memory_domains_repo",
    "recipes_repo",
    "user_stack_subscriptions_repo",
    # MCP / Cowork
    "mcp_sources_repo",
    "per_user_secrets_repo",
    "tool_registry_repo",
    "setup_tokens_repo",
    # Cloud chat
    "chat_session_repo",
    "chat_message_repo",
    "user_workdirs_repo",
    "chat_session_participants_repo",
]


def use_pg() -> bool:
    """Return True when the active backend is Postgres (side-car or cloud).

    Precedence:
      1. ``instance.yaml::database.backend`` (admin-controlled).
      2. ``DATABASE_URL`` env var presence (12-factor convention).
      3. ``AGNES_DB_URL`` env var presence (legacy alias).
    """
    try:
        from src.db_state_machine import BackendState, _OVERLAY_PATH, read_backend_state
        state, _ = read_backend_state()
        if state in (
            BackendState.SIDE_CAR,
            BackendState.CLOUD,
            BackendState.SIDE_CAR_IN_PROGRESS,
            BackendState.CLOUD_IN_PROGRESS,
        ):
            return True
        # Only treat DUCKDB as authoritative when the overlay actually exists;
        # otherwise fall through to the env-var fallback (fresh-install default).
        if state == BackendState.DUCKDB and _OVERLAY_PATH.exists():
            return False
    except Exception:
        pass

    # Env-var fallback
    return bool(os.environ.get("DATABASE_URL") or os.environ.get("AGNES_DB_URL"))


def _pg_engine() -> Any:
    from src.db_pg import get_engine
    return get_engine()


# ---------------------------------------------------------------------------
# core user / RBAC
# ---------------------------------------------------------------------------

def users_repo() -> Any:
    if use_pg():
        from src.repositories.users_pg import UsersPgRepository
        return UsersPgRepository(_pg_engine())
    from src.repositories.users import UserRepository
    return UserRepository(get_system_db())


def user_groups_repo() -> Any:
    if use_pg():
        from src.repositories.user_groups_pg import UserGroupsPgRepository
        return UserGroupsPgRepository(_pg_engine())
    from src.repositories.user_groups import UserGroupsRepository
    return UserGroupsRepository(get_system_db())


def user_group_members_repo() -> Any:
    if use_pg():
        from src.repositories.user_group_members_pg import UserGroupMembersPgRepository
        return UserGroupMembersPgRepository(_pg_engine())
    from src.repositories.user_group_members import UserGroupMembersRepository
    return UserGroupMembersRepository(get_system_db())


def resource_grants_repo() -> Any:
    if use_pg():
        from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
        return ResourceGrantsPgRepository(_pg_engine())
    from src.repositories.resource_grants import ResourceGrantsRepository
    return ResourceGrantsRepository(get_system_db())


def audit_repo() -> Any:
    if use_pg():
        from src.repositories.audit_pg import AuditPgRepository
        return AuditPgRepository(_pg_engine())
    from src.repositories.audit import AuditRepository
    return AuditRepository(get_system_db())


# ---------------------------------------------------------------------------
# ops triad
# ---------------------------------------------------------------------------

def table_registry_repo() -> Any:
    if use_pg():
        from src.repositories.table_registry_pg import TableRegistryPgRepository
        return TableRegistryPgRepository(_pg_engine())
    from src.repositories.table_registry import TableRegistryRepository
    return TableRegistryRepository(get_system_db())


def sync_state_repo() -> Any:
    if use_pg():
        from src.repositories.sync_state_pg import SyncStatePgRepository
        return SyncStatePgRepository(_pg_engine())
    from src.repositories.sync_state import SyncStateRepository
    return SyncStateRepository(get_system_db())


# ---------------------------------------------------------------------------
# config / templates / tokens
# ---------------------------------------------------------------------------

def metric_repo() -> Any:
    if use_pg():
        from src.repositories.metrics_pg import MetricPgRepository
        return MetricPgRepository(_pg_engine())
    from src.repositories.metrics import MetricRepository
    return MetricRepository(get_system_db())


def claude_md_template_repo() -> Any:
    if use_pg():
        from src.repositories.claude_md_template_pg import ClaudeMdTemplatePgRepository
        return ClaudeMdTemplatePgRepository(_pg_engine())
    from src.repositories.claude_md_template import ClaudeMdTemplateRepository
    return ClaudeMdTemplateRepository(get_system_db())


def welcome_template_repo() -> Any:
    if use_pg():
        from src.repositories.welcome_template_pg import WelcomeTemplatePgRepository
        return WelcomeTemplatePgRepository(_pg_engine())
    from src.repositories.welcome_template import WelcomeTemplateRepository
    return WelcomeTemplateRepository(get_system_db())


def news_template_repo() -> Any:
    if use_pg():
        from src.repositories.news_template_pg import NewsTemplatePgRepository
        return NewsTemplatePgRepository(_pg_engine())
    from src.repositories.news_template import NewsTemplateRepository
    return NewsTemplateRepository(get_system_db())


def access_token_repo() -> Any:
    if use_pg():
        from src.repositories.access_tokens_pg import AccessTokenPgRepository
        return AccessTokenPgRepository(_pg_engine())
    from src.repositories.access_tokens import AccessTokenRepository
    return AccessTokenRepository(get_system_db())


def profile_repo() -> Any:
    if use_pg():
        from src.repositories.profiles_pg import ProfilePgRepository
        return ProfilePgRepository(_pg_engine())
    from src.repositories.profiles import ProfileRepository
    return ProfileRepository(get_system_db())


# ---------------------------------------------------------------------------
# lookup / cache / settings
# ---------------------------------------------------------------------------

def view_ownership_repo() -> Any:
    if use_pg():
        from src.repositories.view_ownership_pg import ViewOwnershipPgRepository
        return ViewOwnershipPgRepository(_pg_engine())
    from src.repositories.view_ownership import ViewOwnershipRepository
    return ViewOwnershipRepository(get_system_db())


def column_metadata_repo() -> Any:
    if use_pg():
        from src.repositories.column_metadata_pg import ColumnMetadataPgRepository
        return ColumnMetadataPgRepository(_pg_engine())
    from src.repositories.column_metadata import ColumnMetadataRepository
    return ColumnMetadataRepository(get_system_db())


def bq_metadata_cache_repo() -> Any:
    if use_pg():
        from src.repositories.bq_metadata_cache_pg import BqMetadataCachePgRepository
        return BqMetadataCachePgRepository(_pg_engine())
    from src.repositories.bq_metadata_cache import BqMetadataCacheRepository
    return BqMetadataCacheRepository(get_system_db())


def sync_settings_repo() -> Any:
    if use_pg():
        from src.repositories.sync_settings_pg import SyncSettingsPgRepository
        return SyncSettingsPgRepository(_pg_engine())
    from src.repositories.sync_settings import SyncSettingsRepository
    return SyncSettingsRepository(get_system_db())


def notifications_telegram_repo() -> Any:
    if use_pg():
        from src.repositories.notifications_pg import TelegramPgRepository
        return TelegramPgRepository(_pg_engine())
    from src.repositories.notifications import TelegramRepository
    return TelegramRepository(get_system_db())


def notifications_pending_code_repo() -> Any:
    if use_pg():
        from src.repositories.notifications_pg import PendingCodePgRepository
        return PendingCodePgRepository(_pg_engine())
    from src.repositories.notifications import PendingCodeRepository
    return PendingCodeRepository(get_system_db())


def notifications_script_repo() -> Any:
    if use_pg():
        from src.repositories.notifications_pg import ScriptPgRepository
        return ScriptPgRepository(_pg_engine())
    from src.repositories.notifications import ScriptRepository
    return ScriptRepository(get_system_db())


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------

def session_processor_state_repo() -> Any:
    if use_pg():
        from src.repositories.session_processor_state_pg import (
            SessionProcessorStatePgRepository,
        )
        return SessionProcessorStatePgRepository(_pg_engine())
    from src.repositories.session_processor_state import (
        SessionProcessorStateRepository,
    )
    return SessionProcessorStateRepository(get_system_db())


def observability_views_repo() -> Any:
    if use_pg():
        from src.repositories.observability_views_pg import (
            ObservabilityViewsPgRepository,
        )
        return ObservabilityViewsPgRepository(_pg_engine())
    from src.repositories.observability_views import ObservabilityViewsRepository
    return ObservabilityViewsRepository(get_system_db())


def usage_repo() -> Any:
    if use_pg():
        from src.repositories.usage_pg import UsagePgRepository
        return UsagePgRepository(_pg_engine())
    from src.repositories.usage import UsageRepository
    return UsageRepository(get_system_db())


# ---------------------------------------------------------------------------
# store / marketplace
# ---------------------------------------------------------------------------

def marketplace_registry_repo() -> Any:
    if use_pg():
        from src.repositories.marketplace_registry_pg import (
            MarketplaceRegistryPgRepository,
        )
        return MarketplaceRegistryPgRepository(_pg_engine())
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository
    return MarketplaceRegistryRepository(get_system_db())


def marketplace_plugins_repo() -> Any:
    if use_pg():
        from src.repositories.marketplace_plugins_pg import (
            MarketplacePluginsPgRepository,
        )
        return MarketplacePluginsPgRepository(_pg_engine())
    from src.repositories.marketplace_plugins import MarketplacePluginsRepository
    return MarketplacePluginsRepository(get_system_db())


def store_entities_repo() -> Any:
    if use_pg():
        from src.repositories.store_entities_pg import StoreEntitiesPgRepository
        return StoreEntitiesPgRepository(_pg_engine())
    from src.repositories.store_entities import StoreEntitiesRepository
    return StoreEntitiesRepository(get_system_db())


def user_store_installs_repo() -> Any:
    if use_pg():
        from src.repositories.user_store_installs_pg import (
            UserStoreInstallsPgRepository,
        )
        return UserStoreInstallsPgRepository(_pg_engine())
    from src.repositories.user_store_installs import UserStoreInstallsRepository
    return UserStoreInstallsRepository(get_system_db())


def user_curated_subscriptions_repo() -> Any:
    if use_pg():
        from src.repositories.user_curated_subscriptions_pg import (
            UserCuratedSubscriptionsPgRepository,
        )
        return UserCuratedSubscriptionsPgRepository(_pg_engine())
    from src.repositories.user_curated_subscriptions import (
        UserCuratedSubscriptionsRepository,
    )
    return UserCuratedSubscriptionsRepository(get_system_db())


def store_submissions_repo() -> Any:
    if use_pg():
        from src.repositories.store_submissions_pg import (
            StoreSubmissionsPgRepository,
        )
        return StoreSubmissionsPgRepository(_pg_engine())
    from src.repositories.store_submissions import StoreSubmissionsRepository
    return StoreSubmissionsRepository(get_system_db())


# ---------------------------------------------------------------------------
# knowledge
# ---------------------------------------------------------------------------

def knowledge_repo() -> Any:
    if use_pg():
        from src.repositories.knowledge_pg import KnowledgePgRepository
        return KnowledgePgRepository(_pg_engine())
    from src.repositories.knowledge import KnowledgeRepository
    return KnowledgeRepository(get_system_db())


# ---------------------------------------------------------------------------
# data packages / memory / recipes / subscriptions
# ---------------------------------------------------------------------------

def data_packages_repo() -> Any:
    if use_pg():
        from src.repositories.data_packages_pg import DataPackagesPgRepository
        return DataPackagesPgRepository(_pg_engine())
    from src.repositories.data_packages import DataPackagesRepository
    return DataPackagesRepository(get_system_db())


def memory_domains_repo() -> Any:
    if use_pg():
        from src.repositories.memory_domains_pg import MemoryDomainsPgRepository
        return MemoryDomainsPgRepository(_pg_engine())
    from src.repositories.memory_domains import MemoryDomainsRepository
    return MemoryDomainsRepository(get_system_db())


def memory_domain_suggestions_repo() -> Any:
    if use_pg():
        from src.repositories.memory_domain_suggestions_pg import (
            MemoryDomainSuggestionsPgRepository,
        )
        return MemoryDomainSuggestionsPgRepository(_pg_engine())
    from src.repositories.memory_domain_suggestions import (
        MemoryDomainSuggestionsRepository,
    )
    return MemoryDomainSuggestionsRepository(get_system_db())


def recipes_repo() -> Any:
    if use_pg():
        from src.repositories.recipes_pg import RecipesPgRepository
        return RecipesPgRepository(_pg_engine())
    from src.repositories.recipes import RecipesRepository
    return RecipesRepository(get_system_db())


def user_stack_subscriptions_repo() -> Any:
    if use_pg():
        from src.repositories.user_stack_subscriptions_pg import (
            UserStackSubscriptionsPgRepository,
        )
        return UserStackSubscriptionsPgRepository(_pg_engine())
    from src.repositories.user_stack_subscriptions import (
        UserStackSubscriptionsRepository,
    )
    return UserStackSubscriptionsRepository(get_system_db())


# ---------------------------------------------------------------------------
# MCP / Cowork
# ---------------------------------------------------------------------------

def mcp_sources_repo() -> Any:
    if use_pg():
        from src.repositories.mcp_sources_pg import MCPSourcePgRepository
        return MCPSourcePgRepository(_pg_engine())
    from src.repositories.mcp_sources import MCPSourceRepository
    return MCPSourceRepository(get_system_db())


def per_user_secrets_repo() -> Any:
    if use_pg():
        from src.repositories.secrets_vault_pg import PerUserSecretsPgRepository
        return PerUserSecretsPgRepository(_pg_engine())
    from app.secrets_vault import PerUserSecretsRepository
    return PerUserSecretsRepository(get_system_db())


def tool_registry_repo() -> Any:
    if use_pg():
        from src.repositories.tool_registry_pg import ToolRegistryPgRepository
        return ToolRegistryPgRepository(_pg_engine())
    from src.repositories.tool_registry import ToolRegistryRepository
    return ToolRegistryRepository(get_system_db())


def setup_tokens_repo() -> Any:
    if use_pg():
        from src.repositories.setup_tokens_pg import SetupTokenPgRepository
        return SetupTokenPgRepository(_pg_engine())
    from src.repositories.setup_tokens import SetupTokenRepository
    return SetupTokenRepository(get_system_db())


# ---------------------------------------------------------------------------
# Cloud chat
# ---------------------------------------------------------------------------

def chat_session_repo() -> Any:
    if use_pg():
        from src.repositories.chat_sessions_pg import ChatSessionPgRepository
        return ChatSessionPgRepository(_pg_engine())
    from app.chat.persistence import ChatRepository
    return ChatRepository(get_system_db())


def chat_message_repo() -> Any:
    if use_pg():
        from src.repositories.chat_messages_pg import ChatMessagePgRepository
        return ChatMessagePgRepository(_pg_engine())
    from app.chat.persistence import ChatRepository
    return ChatRepository(get_system_db())


def user_workdirs_repo() -> Any:
    if use_pg():
        from src.repositories.user_workdirs_pg import UserWorkdirPgRepository
        return UserWorkdirPgRepository(_pg_engine())
    from app.chat.persistence import ChatRepository
    return ChatRepository(get_system_db())


def chat_session_participants_repo() -> Any:
    if use_pg():
        from src.repositories.chat_session_participants_pg import (
            ChatSessionParticipantPgRepository,
        )
        return ChatSessionParticipantPgRepository(_pg_engine())
    from app.chat.persistence import ChatRepository
    return ChatRepository(get_system_db())
