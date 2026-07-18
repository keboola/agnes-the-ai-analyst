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

Backend dispatch is a *declarative registry*, not a hand-written
two-way ``if`` per repo. :data:`_REGISTRY` maps each repo key to a
``{backend: (module_path, class_name)}`` table, and :func:`_build`
resolves the active backend, imports the class lazily, and constructs
it with that backend's connection argument (see :data:`_ARG_PROVIDERS`).

Adding a new backend (e.g. ``duckdb_quack``, see
``src/db_state_machine.py``) is therefore localised:

  1. teach :func:`use_pg` / a future ``active_backend()`` to return the
     new backend key,
  2. add the new key to :data:`_ARG_PROVIDERS` (how to obtain its
     connection/engine),
  3. fill the new column in :data:`_REGISTRY` (one ``(module, class)``
     per repo).

The dispatch logic in :func:`_build` is backend-count-agnostic — no
per-repo function changes — and ``tests/test_repository_registry.py``
verifies the table stays complete and symmetric across backends.
"""

from __future__ import annotations

import os
from importlib import import_module
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
    "glossary_repo",
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
    "reports_repo",
    # Store / marketplace
    "marketplace_registry_repo",
    "marketplace_plugins_repo",
    "store_entities_repo",
    "store_entity_votes_repo",
    "user_store_installs_repo",
    "user_curated_subscriptions_repo",
    "store_submissions_repo",
    "store_lint_repo",
    # Knowledge
    "knowledge_repo",
    # Data packages / memory / recipes / subscriptions
    "data_packages_repo",
    "authoring_suggestions_repo",
    "memory_mining_consent_repo",
    "memory_domain_suggestions_repo",
    "memory_domains_repo",
    "recipes_repo",
    "user_stack_subscriptions_repo",
    # MCP / Cowork
    "mcp_sources_repo",
    "per_user_secrets_repo",
    "shared_secrets_repo",
    "system_secrets_repo",
    "tool_registry_repo",
    "setup_tokens_repo",
    # Source connections
    "source_connections_repo",
    "connection_secrets_repo",
    # Cloud chat
    "chat_session_repo",
    "chat_message_repo",
    "user_workdirs_repo",
    "chat_session_participants_repo",
    # OAuth 2.1 MCP connector
    "oauth_clients_repo",
    # Collections
    "file_corpora_repo",
    "corpus_files_repo",
    "corpus_chunks_repo",
    # Maintained digests (K4, #799)
    "knowledge_digests_repo",
    # Chat sandbox secret broker tickets
    "ticket_repo",
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
# backend dispatch
# ---------------------------------------------------------------------------

#: Backend keys. New backends append here (and to the data structures below).
DUCKDB = "duckdb"
PG = "pg"


def _active_backend() -> str:
    """The backend key the current request/process resolves to."""
    return PG if use_pg() else DUCKDB


#: How to obtain the constructor argument for each backend. DuckDB repos take
#: a fresh cursor on the singleton system DB; PG repos take the singleton
#: engine. A new backend registers its connection/engine provider here.
#:
#: The providers resolve ``get_system_db`` / ``_pg_engine`` by NAME at call
#: time (the lambda looks them up in this module's globals when invoked), not
#: by capturing the function object at import. This preserves the pre-registry
#: behaviour where each factory called ``get_system_db()`` in its body — tests
#: that ``patch("src.repositories.get_system_db", ...)`` to redirect the system
#: DB must still take effect.
_ARG_PROVIDERS = {
    DUCKDB: lambda: get_system_db(),
    PG: lambda: _pg_engine(),
}

#: ``repo_key -> {backend: (module_path, class_name)}``. The single source of
#: truth for which class implements each repo on each backend. Lazy import by
#: dotted path keeps import cost identical to the old per-function local
#: imports and avoids import cycles (e.g. ``app.*`` repos).
_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    # core user / RBAC
    "users": {
        DUCKDB: ("src.repositories.users", "UserRepository"),
        PG: ("src.repositories.users_pg", "UsersPgRepository"),
    },
    "user_groups": {
        DUCKDB: ("src.repositories.user_groups", "UserGroupsRepository"),
        PG: ("src.repositories.user_groups_pg", "UserGroupsPgRepository"),
    },
    "user_group_members": {
        DUCKDB: ("src.repositories.user_group_members", "UserGroupMembersRepository"),
        PG: ("src.repositories.user_group_members_pg", "UserGroupMembersPgRepository"),
    },
    "resource_grants": {
        DUCKDB: ("src.repositories.resource_grants", "ResourceGrantsRepository"),
        PG: ("src.repositories.resource_grants_pg", "ResourceGrantsPgRepository"),
    },
    "audit": {
        DUCKDB: ("src.repositories.audit", "AuditRepository"),
        PG: ("src.repositories.audit_pg", "AuditPgRepository"),
    },
    # ops triad
    "table_registry": {
        DUCKDB: ("src.repositories.table_registry", "TableRegistryRepository"),
        PG: ("src.repositories.table_registry_pg", "TableRegistryPgRepository"),
    },
    "sync_state": {
        DUCKDB: ("src.repositories.sync_state", "SyncStateRepository"),
        PG: ("src.repositories.sync_state_pg", "SyncStatePgRepository"),
    },
    # config / templates / tokens
    "metric": {
        DUCKDB: ("src.repositories.metrics", "MetricRepository"),
        PG: ("src.repositories.metrics_pg", "MetricPgRepository"),
    },
    "glossary": {
        DUCKDB: ("src.repositories.glossary", "GlossaryRepository"),
        PG: ("src.repositories.glossary_pg", "GlossaryPgRepository"),
    },
    "claude_md_template": {
        DUCKDB: ("src.repositories.claude_md_template", "ClaudeMdTemplateRepository"),
        PG: ("src.repositories.claude_md_template_pg", "ClaudeMdTemplatePgRepository"),
    },
    "welcome_template": {
        DUCKDB: ("src.repositories.welcome_template", "WelcomeTemplateRepository"),
        PG: ("src.repositories.welcome_template_pg", "WelcomeTemplatePgRepository"),
    },
    "news_template": {
        DUCKDB: ("src.repositories.news_template", "NewsTemplateRepository"),
        PG: ("src.repositories.news_template_pg", "NewsTemplatePgRepository"),
    },
    "access_token": {
        DUCKDB: ("src.repositories.access_tokens", "AccessTokenRepository"),
        PG: ("src.repositories.access_tokens_pg", "AccessTokenPgRepository"),
    },
    "profile": {
        DUCKDB: ("src.repositories.profiles", "ProfileRepository"),
        PG: ("src.repositories.profiles_pg", "ProfilePgRepository"),
    },
    # lookup / cache / settings
    "view_ownership": {
        DUCKDB: ("src.repositories.view_ownership", "ViewOwnershipRepository"),
        PG: ("src.repositories.view_ownership_pg", "ViewOwnershipPgRepository"),
    },
    "column_metadata": {
        DUCKDB: ("src.repositories.column_metadata", "ColumnMetadataRepository"),
        PG: ("src.repositories.column_metadata_pg", "ColumnMetadataPgRepository"),
    },
    "bq_metadata_cache": {
        DUCKDB: ("src.repositories.bq_metadata_cache", "BqMetadataCacheRepository"),
        PG: ("src.repositories.bq_metadata_cache_pg", "BqMetadataCachePgRepository"),
    },
    "sync_settings": {
        DUCKDB: ("src.repositories.sync_settings", "SyncSettingsRepository"),
        PG: ("src.repositories.sync_settings_pg", "SyncSettingsPgRepository"),
    },
    "notifications_telegram": {
        DUCKDB: ("src.repositories.notifications", "TelegramRepository"),
        PG: ("src.repositories.notifications_pg", "TelegramPgRepository"),
    },
    "notifications_pending_code": {
        DUCKDB: ("src.repositories.notifications", "PendingCodeRepository"),
        PG: ("src.repositories.notifications_pg", "PendingCodePgRepository"),
    },
    "notifications_script": {
        DUCKDB: ("src.repositories.notifications", "ScriptRepository"),
        PG: ("src.repositories.notifications_pg", "ScriptPgRepository"),
    },
    # telemetry
    "session_processor_state": {
        DUCKDB: ("src.repositories.session_processor_state", "SessionProcessorStateRepository"),
        PG: ("src.repositories.session_processor_state_pg", "SessionProcessorStatePgRepository"),
    },
    "observability_views": {
        DUCKDB: ("src.repositories.observability_views", "ObservabilityViewsRepository"),
        PG: ("src.repositories.observability_views_pg", "ObservabilityViewsPgRepository"),
    },
    "usage": {
        DUCKDB: ("src.repositories.usage", "UsageRepository"),
        PG: ("src.repositories.usage_pg", "UsagePgRepository"),
    },
    "reports": {
        DUCKDB: ("src.repositories.reports", "ReportsRepository"),
        PG: ("src.repositories.reports_pg", "ReportsPgRepository"),
    },
    # store / marketplace
    "marketplace_registry": {
        DUCKDB: ("src.repositories.marketplace_registry", "MarketplaceRegistryRepository"),
        PG: ("src.repositories.marketplace_registry_pg", "MarketplaceRegistryPgRepository"),
    },
    "marketplace_plugins": {
        DUCKDB: ("src.repositories.marketplace_plugins", "MarketplacePluginsRepository"),
        PG: ("src.repositories.marketplace_plugins_pg", "MarketplacePluginsPgRepository"),
    },
    "store_entities": {
        DUCKDB: ("src.repositories.store_entities", "StoreEntitiesRepository"),
        PG: ("src.repositories.store_entities_pg", "StoreEntitiesPgRepository"),
    },
    "store_entity_votes": {
        DUCKDB: ("src.repositories.store_entity_votes", "StoreEntityVotesRepository"),
        PG: ("src.repositories.store_entity_votes_pg", "StoreEntityVotesPgRepository"),
    },
    "user_store_installs": {
        DUCKDB: ("src.repositories.user_store_installs", "UserStoreInstallsRepository"),
        PG: ("src.repositories.user_store_installs_pg", "UserStoreInstallsPgRepository"),
    },
    "user_curated_subscriptions": {
        DUCKDB: ("src.repositories.user_curated_subscriptions", "UserCuratedSubscriptionsRepository"),
        PG: ("src.repositories.user_curated_subscriptions_pg", "UserCuratedSubscriptionsPgRepository"),
    },
    "store_submissions": {
        DUCKDB: ("src.repositories.store_submissions", "StoreSubmissionsRepository"),
        PG: ("src.repositories.store_submissions_pg", "StoreSubmissionsPgRepository"),
    },
    "store_lint": {
        DUCKDB: ("src.repositories.store_lint", "StoreLintRepository"),
        PG: ("src.repositories.store_lint_pg", "StoreLintPgRepository"),
    },
    # knowledge
    "knowledge": {
        DUCKDB: ("src.repositories.knowledge", "KnowledgeRepository"),
        PG: ("src.repositories.knowledge_pg", "KnowledgePgRepository"),
    },
    # data packages / memory / recipes / subscriptions
    "data_packages": {
        DUCKDB: ("src.repositories.data_packages", "DataPackagesRepository"),
        PG: ("src.repositories.data_packages_pg", "DataPackagesPgRepository"),
    },
    "memory_domains": {
        DUCKDB: ("src.repositories.memory_domains", "MemoryDomainsRepository"),
        PG: ("src.repositories.memory_domains_pg", "MemoryDomainsPgRepository"),
    },
    "memory_domain_suggestions": {
        DUCKDB: ("src.repositories.memory_domain_suggestions", "MemoryDomainSuggestionsRepository"),
        PG: ("src.repositories.memory_domain_suggestions_pg", "MemoryDomainSuggestionsPgRepository"),
    },
    "authoring_suggestions": {
        DUCKDB: ("src.repositories.authoring_suggestions", "AuthoringSuggestionsRepository"),
        PG: ("src.repositories.authoring_suggestions_pg", "AuthoringSuggestionsPgRepository"),
    },
    "memory_mining_consent": {
        DUCKDB: ("src.repositories.memory_mining_consent", "MemoryMiningConsentRepository"),
        PG: ("src.repositories.memory_mining_consent_pg", "MemoryMiningConsentPgRepository"),
    },
    "recipes": {
        DUCKDB: ("src.repositories.recipes", "RecipesRepository"),
        PG: ("src.repositories.recipes_pg", "RecipesPgRepository"),
    },
    "user_stack_subscriptions": {
        DUCKDB: ("src.repositories.user_stack_subscriptions", "UserStackSubscriptionsRepository"),
        PG: ("src.repositories.user_stack_subscriptions_pg", "UserStackSubscriptionsPgRepository"),
    },
    # MCP / Cowork
    "mcp_sources": {
        DUCKDB: ("src.repositories.mcp_sources", "MCPSourceRepository"),
        PG: ("src.repositories.mcp_sources_pg", "MCPSourcePgRepository"),
    },
    "per_user_secrets": {
        DUCKDB: ("app.secrets_vault", "PerUserSecretsRepository"),
        PG: ("src.repositories.secrets_vault_pg", "PerUserSecretsPgRepository"),
    },
    "shared_secrets": {
        DUCKDB: ("app.secrets_vault", "SharedSecretsRepository"),
        PG: ("src.repositories.secrets_vault_pg", "SharedSecretsPgRepository"),
    },
    "system_secrets": {
        DUCKDB: ("app.secrets_vault", "SystemSecretsRepository"),
        PG: ("src.repositories.secrets_vault_pg", "SystemSecretsPgRepository"),
    },
    "tool_registry": {
        DUCKDB: ("src.repositories.tool_registry", "ToolRegistryRepository"),
        PG: ("src.repositories.tool_registry_pg", "ToolRegistryPgRepository"),
    },
    "setup_tokens": {
        DUCKDB: ("src.repositories.setup_tokens", "SetupTokenRepository"),
        PG: ("src.repositories.setup_tokens_pg", "SetupTokenPgRepository"),
    },
    # source connections
    "source_connections": {
        DUCKDB: ("src.repositories.source_connections", "SourceConnectionsRepository"),
        PG: ("src.repositories.source_connections_pg", "SourceConnectionsPgRepository"),
    },
    "connection_secrets": {
        DUCKDB: ("app.secrets_vault", "ConnectionSecretsRepository"),
        PG: ("src.repositories.secrets_vault_pg", "ConnectionSecretsPgRepository"),
    },
    # cloud chat — the DuckDB side is a single ChatRepository covering all
    # chat tables; the PG side is split per table.
    "chat_session": {
        DUCKDB: ("app.chat.persistence", "ChatRepository"),
        PG: ("src.repositories.chat_sessions_pg", "ChatSessionPgRepository"),
    },
    "chat_message": {
        DUCKDB: ("app.chat.persistence", "ChatRepository"),
        PG: ("src.repositories.chat_messages_pg", "ChatMessagePgRepository"),
    },
    "user_workdirs": {
        DUCKDB: ("app.chat.persistence", "ChatRepository"),
        PG: ("src.repositories.user_workdirs_pg", "UserWorkdirPgRepository"),
    },
    "chat_session_participants": {
        DUCKDB: ("app.chat.persistence", "ChatRepository"),
        PG: ("src.repositories.chat_session_participants_pg", "ChatSessionParticipantPgRepository"),
    },
    "oauth_clients": {
        DUCKDB: ("src.repositories.oauth_clients", "OAuthClientsRepository"),
        PG: ("src.repositories.oauth_clients_pg", "OAuthClientsPgRepository"),
    },
    # collections
    "file_corpora": {
        DUCKDB: ("src.repositories.file_corpora", "FileCorporaRepository"),
        PG: ("src.repositories.file_corpora_pg", "FileCorporaPgRepository"),
    },
    "corpus_files": {
        DUCKDB: ("src.repositories.corpus_files", "CorpusFilesRepository"),
        PG: ("src.repositories.corpus_files_pg", "CorpusFilesPgRepository"),
    },
    "corpus_chunks": {
        DUCKDB: ("src.repositories.corpus_chunks", "CorpusChunksRepository"),
        PG: ("src.repositories.corpus_chunks_pg", "CorpusChunksPgRepository"),
    },
    # Maintained digests (K4, #799)
    "knowledge_digests": {
        DUCKDB: ("src.repositories.knowledge_digests", "KnowledgeDigestsRepository"),
        PG: ("src.repositories.knowledge_digests_pg", "KnowledgeDigestsPgRepository"),
    },
    # Chat sandbox secret broker tickets
    "ticket": {
        DUCKDB: ("src.repositories.ticket", "TicketRepository"),
        PG: ("src.repositories.ticket_pg", "TicketPgRepository"),
    },
}


def _build(key: str) -> Any:
    """Resolve + construct the repo for ``key`` on the active backend."""
    backend = _active_backend()
    try:
        module_path, class_name = _REGISTRY[key][backend]
    except KeyError as exc:
        raise KeyError(
            f"no '{backend}' repository registered for '{key}' (known: {sorted(_REGISTRY.get(key, {}))})"
        ) from exc
    klass = getattr(import_module(module_path), class_name)
    return klass(_ARG_PROVIDERS[backend]())


# ---------------------------------------------------------------------------
# public factory functions — thin delegates over the registry. Names + return
# contract are unchanged; callsites are unaffected.
# ---------------------------------------------------------------------------


# core user / RBAC
def users_repo() -> Any:
    return _build("users")


def user_groups_repo() -> Any:
    return _build("user_groups")


def user_group_members_repo() -> Any:
    return _build("user_group_members")


def resource_grants_repo() -> Any:
    return _build("resource_grants")


def audit_repo() -> Any:
    return _build("audit")


# ops triad
def table_registry_repo() -> Any:
    return _build("table_registry")


def sync_state_repo() -> Any:
    return _build("sync_state")


# config / templates / tokens
def metric_repo() -> Any:
    return _build("metric")


def glossary_repo() -> Any:
    return _build("glossary")


def claude_md_template_repo() -> Any:
    return _build("claude_md_template")


def welcome_template_repo() -> Any:
    return _build("welcome_template")


def news_template_repo() -> Any:
    return _build("news_template")


def access_token_repo() -> Any:
    return _build("access_token")


def profile_repo() -> Any:
    return _build("profile")


# lookup / cache / settings
def view_ownership_repo() -> Any:
    return _build("view_ownership")


def column_metadata_repo() -> Any:
    return _build("column_metadata")


def bq_metadata_cache_repo() -> Any:
    return _build("bq_metadata_cache")


def sync_settings_repo() -> Any:
    return _build("sync_settings")


def notifications_telegram_repo() -> Any:
    return _build("notifications_telegram")


def notifications_pending_code_repo() -> Any:
    return _build("notifications_pending_code")


def notifications_script_repo() -> Any:
    return _build("notifications_script")


# telemetry
def session_processor_state_repo() -> Any:
    return _build("session_processor_state")


def observability_views_repo() -> Any:
    return _build("observability_views")


def usage_repo() -> Any:
    return _build("usage")


def reports_repo() -> Any:
    return _build("reports")


# store / marketplace
def marketplace_registry_repo() -> Any:
    return _build("marketplace_registry")


def marketplace_plugins_repo() -> Any:
    return _build("marketplace_plugins")


def store_entities_repo() -> Any:
    return _build("store_entities")


def store_entity_votes_repo() -> Any:
    return _build("store_entity_votes")


def user_store_installs_repo() -> Any:
    return _build("user_store_installs")


def user_curated_subscriptions_repo() -> Any:
    return _build("user_curated_subscriptions")


def store_submissions_repo() -> Any:
    return _build("store_submissions")


def store_lint_repo() -> Any:
    return _build("store_lint")


# knowledge
def knowledge_repo() -> Any:
    return _build("knowledge")


# data packages / memory / recipes / subscriptions
def data_packages_repo() -> Any:
    return _build("data_packages")


def memory_domains_repo() -> Any:
    return _build("memory_domains")


def memory_domain_suggestions_repo() -> Any:
    return _build("memory_domain_suggestions")


def authoring_suggestions_repo() -> Any:
    return _build("authoring_suggestions")


def memory_mining_consent_repo() -> Any:
    return _build("memory_mining_consent")


def recipes_repo() -> Any:
    return _build("recipes")


def user_stack_subscriptions_repo() -> Any:
    return _build("user_stack_subscriptions")


# MCP / Cowork
def mcp_sources_repo() -> Any:
    return _build("mcp_sources")


def per_user_secrets_repo() -> Any:
    return _build("per_user_secrets")


def shared_secrets_repo() -> Any:
    return _build("shared_secrets")


def source_connections_repo() -> Any:
    return _build("source_connections")


def connection_secrets_repo() -> Any:
    return _build("connection_secrets")


def system_secrets_repo() -> Any:
    return _build("system_secrets")


def tool_registry_repo() -> Any:
    return _build("tool_registry")


def setup_tokens_repo() -> Any:
    return _build("setup_tokens")


# cloud chat
def chat_session_repo() -> Any:
    return _build("chat_session")


def chat_message_repo() -> Any:
    return _build("chat_message")


def user_workdirs_repo() -> Any:
    return _build("user_workdirs")


def chat_session_participants_repo() -> Any:
    return _build("chat_session_participants")


def oauth_clients_repo() -> Any:
    return _build("oauth_clients")


# collections
def file_corpora_repo() -> Any:
    return _build("file_corpora")


def corpus_files_repo() -> Any:
    return _build("corpus_files")


def corpus_chunks_repo() -> Any:
    return _build("corpus_chunks")


# Maintained digests (K4, #799)
def knowledge_digests_repo() -> Any:
    return _build("knowledge_digests")


# chat sandbox secret broker tickets
def ticket_repo() -> Any:
    return _build("ticket")
