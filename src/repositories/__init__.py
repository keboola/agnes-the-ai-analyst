"""Repository factory — Postgres-backed app state.

Every ``<name>_repo()`` function returns a ready-to-use repository instance
bound to the singleton ``src.db_pg`` engine. ``AGNES_DB_URL`` (or
``DATABASE_URL``) must be set; the engine getter raises if it isn't.

Callsites import factory functions instead of repository classes:

    from src.repositories import users_repo
    repo = users_repo()

DuckDB is reserved for analytics — see ``src.db.get_analytics_db`` /
``get_analytics_db_readonly`` for the ATTACH-based view layer over
``/data/extracts/*/extract.duckdb``. Business-data repositories no
longer touch DuckDB.
"""
from __future__ import annotations

from typing import Any

# Analytics DB stays on DuckDB — re-export so callers keep one import path.
from src.db import get_analytics_db

__all__ = [
    "get_analytics_db",
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
]


def _pg_engine() -> Any:
    from src.db_pg import get_engine
    return get_engine()


# ---------------------------------------------------------------------------
# core user / RBAC
# ---------------------------------------------------------------------------

def users_repo() -> Any:
    from src.repositories.users_pg import UsersPgRepository
    return UsersPgRepository(_pg_engine())


def user_groups_repo() -> Any:
    from src.repositories.user_groups_pg import UserGroupsPgRepository
    return UserGroupsPgRepository(_pg_engine())


def user_group_members_repo() -> Any:
    from src.repositories.user_group_members_pg import UserGroupMembersPgRepository
    return UserGroupMembersPgRepository(_pg_engine())


def resource_grants_repo() -> Any:
    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    return ResourceGrantsPgRepository(_pg_engine())


def audit_repo() -> Any:
    from src.repositories.audit_pg import AuditPgRepository
    return AuditPgRepository(_pg_engine())


# ---------------------------------------------------------------------------
# ops triad
# ---------------------------------------------------------------------------

def table_registry_repo() -> Any:
    from src.repositories.table_registry_pg import TableRegistryPgRepository
    return TableRegistryPgRepository(_pg_engine())


def sync_state_repo() -> Any:
    from src.repositories.sync_state_pg import SyncStatePgRepository
    return SyncStatePgRepository(_pg_engine())


# ---------------------------------------------------------------------------
# config / templates / tokens
# ---------------------------------------------------------------------------

def metric_repo() -> Any:
    from src.repositories.metrics_pg import MetricPgRepository
    return MetricPgRepository(_pg_engine())


def claude_md_template_repo() -> Any:
    from src.repositories.claude_md_template_pg import ClaudeMdTemplatePgRepository
    return ClaudeMdTemplatePgRepository(_pg_engine())


def welcome_template_repo() -> Any:
    from src.repositories.welcome_template_pg import WelcomeTemplatePgRepository
    return WelcomeTemplatePgRepository(_pg_engine())


def news_template_repo() -> Any:
    from src.repositories.news_template_pg import NewsTemplatePgRepository
    return NewsTemplatePgRepository(_pg_engine())


def access_token_repo() -> Any:
    from src.repositories.access_tokens_pg import AccessTokenPgRepository
    return AccessTokenPgRepository(_pg_engine())


def profile_repo() -> Any:
    from src.repositories.profiles_pg import ProfilePgRepository
    return ProfilePgRepository(_pg_engine())


# ---------------------------------------------------------------------------
# lookup / cache / settings
# ---------------------------------------------------------------------------

def view_ownership_repo() -> Any:
    from src.repositories.view_ownership_pg import ViewOwnershipPgRepository
    return ViewOwnershipPgRepository(_pg_engine())


def column_metadata_repo() -> Any:
    from src.repositories.column_metadata_pg import ColumnMetadataPgRepository
    return ColumnMetadataPgRepository(_pg_engine())


def bq_metadata_cache_repo() -> Any:
    from src.repositories.bq_metadata_cache_pg import BqMetadataCachePgRepository
    return BqMetadataCachePgRepository(_pg_engine())


def sync_settings_repo() -> Any:
    from src.repositories.sync_settings_pg import SyncSettingsPgRepository
    return SyncSettingsPgRepository(_pg_engine())


def notifications_telegram_repo() -> Any:
    from src.repositories.notifications_pg import TelegramPgRepository
    return TelegramPgRepository(_pg_engine())


def notifications_pending_code_repo() -> Any:
    from src.repositories.notifications_pg import PendingCodePgRepository
    return PendingCodePgRepository(_pg_engine())


def notifications_script_repo() -> Any:
    from src.repositories.notifications_pg import ScriptPgRepository
    return ScriptPgRepository(_pg_engine())


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------

def session_processor_state_repo() -> Any:
    from src.repositories.session_processor_state_pg import (
        SessionProcessorStatePgRepository,
    )
    return SessionProcessorStatePgRepository(_pg_engine())


def observability_views_repo() -> Any:
    from src.repositories.observability_views_pg import (
        ObservabilityViewsPgRepository,
    )
    return ObservabilityViewsPgRepository(_pg_engine())


def usage_repo() -> Any:
    from src.repositories.usage_pg import UsagePgRepository
    return UsagePgRepository(_pg_engine())


# ---------------------------------------------------------------------------
# store / marketplace
# ---------------------------------------------------------------------------

def marketplace_registry_repo() -> Any:
    from src.repositories.marketplace_registry_pg import (
        MarketplaceRegistryPgRepository,
    )
    return MarketplaceRegistryPgRepository(_pg_engine())


def marketplace_plugins_repo() -> Any:
    from src.repositories.marketplace_plugins_pg import (
        MarketplacePluginsPgRepository,
    )
    return MarketplacePluginsPgRepository(_pg_engine())


def store_entities_repo() -> Any:
    from src.repositories.store_entities_pg import StoreEntitiesPgRepository
    return StoreEntitiesPgRepository(_pg_engine())


def user_store_installs_repo() -> Any:
    from src.repositories.user_store_installs_pg import (
        UserStoreInstallsPgRepository,
    )
    return UserStoreInstallsPgRepository(_pg_engine())


def user_curated_subscriptions_repo() -> Any:
    from src.repositories.user_curated_subscriptions_pg import (
        UserCuratedSubscriptionsPgRepository,
    )
    return UserCuratedSubscriptionsPgRepository(_pg_engine())


def store_submissions_repo() -> Any:
    from src.repositories.store_submissions_pg import (
        StoreSubmissionsPgRepository,
    )
    return StoreSubmissionsPgRepository(_pg_engine())


# ---------------------------------------------------------------------------
# knowledge
# ---------------------------------------------------------------------------

def knowledge_repo() -> Any:
    from src.repositories.knowledge_pg import KnowledgePgRepository
    return KnowledgePgRepository(_pg_engine())
