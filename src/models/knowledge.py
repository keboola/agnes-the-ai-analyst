"""SQLAlchemy models for the knowledge cluster:
knowledge_items, knowledge_contradictions, knowledge_item_relations,
verification_evidence, knowledge_votes, knowledge_item_user_dismissed,
memory_domains, knowledge_item_domains, memory_domain_suggestions.

Mirrors src/db.py:126-246 (core knowledge cluster) and src/db.py:585-644
(v49+ Memory Domains: first-class domain entities, M:N bridge to
knowledge_items, and the v55 admin suggestion queue).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String, server_default=text("'pending'"), nullable=False)
    contributors: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source_user: Mapped[str | None] = mapped_column(String, nullable=True)
    audience: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    entities: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source_type: Mapped[str] = mapped_column(String, server_default=text("'claude_local_md'"), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes: Mapped[str | None] = mapped_column(String, nullable=True)
    sensitivity: Mapped[str] = mapped_column(String, server_default=text("'internal'"), nullable=False)
    is_personal: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # v50+ Required-onboarding flag (matches the Required column on Memory
    # Domains / Data Packages — same semantics across all three). Nullable
    # because legacy DuckDB rows predate the column and don't carry a value.
    is_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    __table_args__ = (
        Index("idx_knowledge_items_status", "status"),
        Index("idx_knowledge_items_category", "category"),
        Index("idx_knowledge_items_domain", "domain"),
        Index("idx_knowledge_items_source_user", "source_user"),
    )


class KnowledgeContradiction(Base):
    __tablename__ = "knowledge_contradictions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    item_a_id: Mapped[str] = mapped_column(String, nullable=False)
    item_b_id: Mapped[str] = mapped_column(String, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str | None] = mapped_column(String, nullable=True)
    suggested_resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class KnowledgeItemRelation(Base):
    __tablename__ = "knowledge_item_relations"

    item_a_id: Mapped[str] = mapped_column(String, nullable=False)
    item_b_id: Mapped[str] = mapped_column(String, nullable=False)
    relation_type: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float | None] = mapped_column(Double, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        PrimaryKeyConstraint("item_a_id", "item_b_id", "relation_type"),
        Index("idx_knowledge_item_relations_resolved", "resolved"),
    )


class VerificationEvidence(Base):
    __tablename__ = "verification_evidence"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    item_id: Mapped[str] = mapped_column(String, nullable=False)
    source_user: Mapped[str | None] = mapped_column(String, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    detection_type: Mapped[str | None] = mapped_column(String, nullable=True)
    user_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (Index("idx_verification_evidence_item", "item_id"),)


class KnowledgeVote(Base):
    __tablename__ = "knowledge_votes"

    item_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    vote: Mapped[int | None] = mapped_column(Integer, nullable=True)
    voted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (PrimaryKeyConstraint("item_id", "user_id"),)


class KnowledgeItemUserDismissed(Base):
    __tablename__ = "knowledge_item_user_dismissed"

    user_id: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[str] = mapped_column(String, nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "item_id"),
        Index("idx_knowledge_item_user_dismissed_user", "user_id"),
    )


class MemoryDomain(Base):
    """v49 first-class domain entity (replaces the v15 scalar
    ``knowledge_items.domain`` string).

    Cover-image / status / soft-delete columns mirror the
    ``data_packages`` shape (same admin upload contract, same
    ``'prod'`` lifecycle default, same ``deleted_at IS NULL`` filter
    convention). ``created_by`` is NULLABLE in DuckDB DDL — mirror it
    so alembic autogenerate doesn't see drift.
    """

    __tablename__ = "memory_domains"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    color: Mapped[str | None] = mapped_column(String, nullable=True)
    # v50: admin-uploaded cover image (same contract as
    # data_packages.cover_image_url).
    cover_image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # v51: lifecycle pill ('prod' / 'poc' / 'coming-soon' / 'draft'). No
    # ``category`` column on Memory Domains — the domain IS the
    # classification.
    status: Mapped[str] = mapped_column(String, server_default=text("'prod'"), nullable=False)
    # v54: soft-delete column. DELETE handlers set this; list/get filter
    # ``deleted_at IS NULL``.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class KnowledgeItemDomain(Base):
    """M:N bridge between ``knowledge_items`` and ``memory_domains``.

    The DuckDB DDL declares both ``REFERENCES knowledge_items(id)`` and
    ``REFERENCES memory_domains(id)`` but no ``ON DELETE`` clause.
    Mirroring the data_packages_tables precedent (Task 1A.1), we
    declare the FK with ``ON DELETE CASCADE`` on the *owning-side*
    parent (``memory_domains``) so a hard DELETE doesn't leave orphans,
    and omit the FK on ``item_id`` — repository code clears the
    junction explicitly when an item is removed, matching the
    asymmetric pattern in ``DataPackageTable`` (FK on ``package_id``,
    bare ``table_id``).
    """

    __tablename__ = "knowledge_item_domains"

    item_id: Mapped[str] = mapped_column(String, nullable=False)
    domain_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("memory_domains.id", ondelete="CASCADE"),
        nullable=False,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    added_by: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("item_id", "domain_id"),
        Index("idx_knowledge_item_domains_domain", "domain_id"),
    )


class MemoryDomainSuggestion(Base):
    """v55 admin queue: non-admin users propose a domain from the
    /corporate-memory empty state.

    Approve = create a real ``memory_domains`` row + set
    ``status='approved'`` + record ``created_domain_id`` for the
    deep-link. Reject = ``status='rejected'`` with optional
    ``resolution_note``. Resolved rows stay around for audit /
    requester visibility. No FK on ``created_by`` — a deleted user
    must not cascade-nuke their suggestion history.
    """

    __tablename__ = "memory_domain_suggestions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'pending' / 'approved' / 'rejected'. DuckDB DDL has no NOT NULL
    # but a server default — mirror the data_packages.status convention
    # and treat as nullable=False on the PG side.
    status: Mapped[str] = mapped_column(String, server_default=text("'pending'"), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # On approve, the ``memory_domains.id`` of the freshly created row
    # — lets the admin queue deep-link to the result. No FK because
    # rejected suggestions never populate this column.
    created_domain_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (Index("idx_memory_domain_suggestions_status", "status"),)


class AuthoringSuggestion(Base):
    """v77 generic non-admin suggestion queue for the authoring studio
    (data-package / mcp / marketplace / corporate-memory). Mirrors
    ``src/db.py`` ``authoring_suggestions``."""

    __tablename__ = "authoring_suggestions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    domain: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String, server_default=text("'pending'"), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_resource_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (Index("idx_authoring_suggestions_status", "status"),)


class MemoryMiningConsent(Base):
    """v78 per-user opt-IN to having one's session transcripts mined into
    shared corporate memory (privacy gate). Mirrors ``src/db.py``
    ``memory_mining_consent``."""

    __tablename__ = "memory_mining_consent"

    user_email: Mapped[str] = mapped_column(String, primary_key=True)
    opted_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
