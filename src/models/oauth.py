"""SQLAlchemy models for OAuth 2.1 client registrations and tokens (DuckDB v80).

Mirrors:
  - oauth_clients        (RFC 7591 dynamic client registration)
  - oauth_auth_codes     (PKCE authorization codes)
  - oauth_access_tokens  (issued access tokens)
  - oauth_refresh_tokens (refresh tokens)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String, primary_key=True)
    client_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    redirect_uris: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    client_name: Mapped[str | None] = mapped_column(String, nullable=True)
    client_metadata: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class OAuthAuthCode(Base):
    __tablename__ = "oauth_auth_codes"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[str] = mapped_column(String, nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    code_challenge: Mapped[str] = mapped_column(String, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String, nullable=False)
    redirect_uri_provided_explicitly: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    resource: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)


class OAuthAccessToken(Base):
    __tablename__ = "oauth_access_tokens"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[str] = mapped_column(String, nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    resource: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class OAuthRefreshToken(Base):
    __tablename__ = "oauth_refresh_tokens"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[str] = mapped_column(String, nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
