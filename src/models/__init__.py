"""SQLAlchemy models for Agnes's Postgres-backed app state.

Importing this package registers every model on ``src.db_pg.Base.metadata``;
Alembic's autogenerate reads that metadata to detect drift.

Add a new model by creating ``src/models/<name>.py`` that imports ``Base``
from ``src.db_pg`` and adds it to the ``__all__`` re-export below.
"""

from __future__ import annotations

from src.models.audit import AuditLog
from src.models.chat import ChatMessage, ChatSession, UserWorkdir
from src.models.collections import CorpusChunk, CorpusFile, FileCorpus
from src.models.config import InstanceTemplate, MetricDefinition, PersonalAccessToken
from src.models.connections import ConnectionSecret, SourceConnection
from src.models.data_packages import DataPackage, DataPackageTable, DataPackageTool
from src.models.knowledge import (
    KnowledgeContradiction,
    KnowledgeItem,
    KnowledgeItemDomain,
    KnowledgeItemRelation,
    KnowledgeItemUserDismissed,
    KnowledgeVote,
    MemoryDomain,
    MemoryDomainSuggestion,
    VerificationEvidence,
)
from src.models.lookup import (
    BqMetadataCache,
    ColumnMetadata,
    UserSyncSettings,
    ViewOwnership,
)
from src.models.misc import (
    NewsTemplate,
    PendingCode,
    ScriptRegistry,
    TableProfile,
    TelegramLink,
)
from src.models.ops import SyncHistory, SyncState, TableRegistry
from src.models.recipes import Recipe
from src.models.store import (
    MarketplacePlugin,
    MarketplaceRegistry,
    StoreEntity,
    StoreEntityVote,
    StoreSubmission,
    UserPluginOptout,
    UserStackSubscription,
    UserStoreInstall,
)
from src.models.telemetry import (
    SessionProcessorState,
    UsageEvent,
    UsageMarketplaceItemDaily,
    UsageMarketplaceItemWindow,
    UsageSessionSummary,
    UsageToolDaily,
    UserObservabilityView,
)
from src.models.mcp import (
    MCPSecret,
    MCPSource,
    MCPUserSecret,
    SetupToken,
    ToolGrant,
    ToolRegistry,
)
from src.models.rbac import (
    ResourceGrant,
    User,
    UserGroup,
    UserGroupMember,
)
from src.models.vault import SystemSecret


__all__ = [
    "AuditLog",
    "BqMetadataCache",
    "ChatMessage",
    "ChatSession",
    "ColumnMetadata",
    "ConnectionSecret",
    "CorpusChunk",
    "CorpusFile",
    "FileCorpus",
    "DataPackage",
    "DataPackageTable",
    "DataPackageTool",
    "InstanceTemplate",
    "KnowledgeContradiction",
    "KnowledgeItem",
    "KnowledgeItemDomain",
    "KnowledgeItemRelation",
    "KnowledgeItemUserDismissed",
    "KnowledgeVote",
    "MCPSecret",
    "MCPSource",
    "MCPUserSecret",
    "MarketplacePlugin",
    "MarketplaceRegistry",
    "MemoryDomain",
    "MemoryDomainSuggestion",
    "MetricDefinition",
    "NewsTemplate",
    "PendingCode",
    "PersonalAccessToken",
    "Recipe",
    "ResourceGrant",
    "SetupToken",
    "ScriptRegistry",
    "SessionProcessorState",
    "SourceConnection",
    "StoreEntity",
    "StoreEntityVote",
    "StoreSubmission",
    "SyncHistory",
    "SyncState",
    "SystemSecret",
    "TableProfile",
    "TableRegistry",
    "TelegramLink",
    "VerificationEvidence",
    "UsageEvent",
    "UsageMarketplaceItemDaily",
    "UsageMarketplaceItemWindow",
    "UsageSessionSummary",
    "UsageToolDaily",
    "ToolGrant",
    "ToolRegistry",
    "User",
    "UserGroup",
    "UserGroupMember",
    "UserObservabilityView",
    "UserPluginOptout",
    "UserStackSubscription",
    "UserStoreInstall",
    "UserSyncSettings",
    "UserWorkdir",
    "ViewOwnership",
]
