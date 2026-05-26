"""SQLAlchemy models for the knowledge cluster:
knowledge_items, knowledge_contradictions, knowledge_item_relations,
verification_evidence, knowledge_votes, knowledge_item_user_dismissed.

Mirrors src/db.py:126-246.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Double,
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
    status: Mapped[str] = mapped_column(
        String, server_default=text("'pending'"), nullable=False
    )
    contributors: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source_user: Mapped[str | None] = mapped_column(String, nullable=True)
    audience: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    entities: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source_type: Mapped[str] = mapped_column(
        String, server_default=text("'claude_local_md'"), nullable=False
    )
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes: Mapped[str | None] = mapped_column(String, nullable=True)
    sensitivity: Mapped[str] = mapped_column(
        String, server_default=text("'internal'"), nullable=False
    )
    is_personal: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    resolved: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), nullable=False
    )
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
    resolved: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), nullable=False
    )
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

    __table_args__ = (
        Index("idx_verification_evidence_item", "item_id"),
    )


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

    __table_args__ = (
        PrimaryKeyConstraint("item_id", "user_id"),
    )


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
