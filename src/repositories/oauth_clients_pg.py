"""Postgres-backed repository for OAuth 2.1 client registrations and tokens.

Mirrors ``src/repositories/oauth_clients.py``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


def _hash(value: str) -> str:
    """sha256 of an OAuth auth code / access / refresh token — only the digest
    is persisted (audit M4). Mirrors ``src/repositories/oauth_clients.py``."""
    return hashlib.sha256(value.encode()).hexdigest()


class OAuthClientsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    def upsert_client(
        self,
        client_id: str,
        redirect_uris: list[str] | None = None,
        client_secret: str | None = None,
        client_name: str | None = None,
        client_metadata: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO oauth_clients
                        (client_id, client_secret, redirect_uris, client_name,
                         client_metadata, created_at)
                    VALUES (:client_id, :client_secret, :redirect_uris, :client_name,
                            :client_metadata, :created_at)
                    ON CONFLICT (client_id) DO UPDATE SET
                        client_secret   = EXCLUDED.client_secret,
                        redirect_uris   = EXCLUDED.redirect_uris,
                        client_name     = EXCLUDED.client_name,
                        client_metadata = EXCLUDED.client_metadata
                    """
                ),
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uris": json.dumps(redirect_uris or []),
                    "client_name": client_name,
                    "client_metadata": json.dumps(client_metadata or {}),
                    "created_at": now,
                },
            )

    def get_client(self, client_id: str) -> Optional[dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM oauth_clients WHERE client_id = :c"),
                    {"c": client_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        data = dict(row)
        data["redirect_uris"] = json.loads(data["redirect_uris"] or "[]")
        data["client_metadata"] = json.loads(data["client_metadata"] or "{}")
        return data

    # ------------------------------------------------------------------
    # Authorization codes
    # ------------------------------------------------------------------

    def save_auth_code(
        self,
        code: str,
        client_id: str,
        scopes: list[str],
        code_challenge: str,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        expires_at: float,
        subject: str | None = None,
        resource: str | None = None,
        state: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO oauth_auth_codes
                        (code, client_id, scopes, code_challenge, redirect_uri,
                         redirect_uri_provided_explicitly, expires_at, subject, resource, state)
                    VALUES (:code, :client_id, :scopes, :code_challenge, :redirect_uri,
                            :redirect_uri_provided_explicitly, :expires_at, :subject, :resource, :state)
                    ON CONFLICT (code) DO UPDATE SET
                        client_id   = EXCLUDED.client_id,
                        scopes      = EXCLUDED.scopes,
                        code_challenge = EXCLUDED.code_challenge,
                        redirect_uri = EXCLUDED.redirect_uri,
                        redirect_uri_provided_explicitly = EXCLUDED.redirect_uri_provided_explicitly,
                        expires_at  = EXCLUDED.expires_at,
                        subject     = EXCLUDED.subject,
                        resource    = EXCLUDED.resource,
                        state       = EXCLUDED.state
                    """
                ),
                {
                    "code": _hash(code),
                    "client_id": client_id,
                    "scopes": json.dumps(scopes),
                    "code_challenge": code_challenge,
                    "redirect_uri": redirect_uri,
                    "redirect_uri_provided_explicitly": redirect_uri_provided_explicitly,
                    "expires_at": expires_at,
                    "subject": subject,
                    "resource": resource,
                    "state": state,
                },
            )

    def get_auth_code(self, code: str) -> Optional[dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM oauth_auth_codes WHERE code = :c"),
                    {"c": _hash(code)},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        data = dict(row)
        data["scopes"] = json.loads(data["scopes"] or "[]")
        data["redirect_uri_provided_explicitly"] = bool(data["redirect_uri_provided_explicitly"])
        return data

    def delete_auth_code(self, code: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM oauth_auth_codes WHERE code = :c"),
                {"c": _hash(code)},
            )

    # ------------------------------------------------------------------
    # Access tokens
    # ------------------------------------------------------------------

    def save_access_token(
        self,
        token: str,
        client_id: str,
        scopes: list[str],
        expires_at: int | None,
        subject: str | None = None,
        resource: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO oauth_access_tokens
                        (token, client_id, scopes, expires_at, subject, resource, created_at)
                    VALUES (:token, :client_id, :scopes, :expires_at, :subject, :resource, :created_at)
                    ON CONFLICT (token) DO UPDATE SET
                        client_id  = EXCLUDED.client_id,
                        scopes     = EXCLUDED.scopes,
                        expires_at = EXCLUDED.expires_at,
                        subject    = EXCLUDED.subject,
                        resource   = EXCLUDED.resource
                    """
                ),
                {
                    "token": _hash(token),
                    "client_id": client_id,
                    "scopes": json.dumps(scopes),
                    "expires_at": expires_at,
                    "subject": subject,
                    "resource": resource,
                    "created_at": now,
                },
            )

    def get_access_token(self, token: str) -> Optional[dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM oauth_access_tokens WHERE token = :t"),
                    {"t": _hash(token)},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        data = dict(row)
        data["scopes"] = json.loads(data["scopes"] or "[]")
        return data

    def revoke_access_token(self, token: str) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE oauth_access_tokens SET revoked_at = :now WHERE token = :t"),
                {"now": now, "t": _hash(token)},
            )

    # ------------------------------------------------------------------
    # Refresh tokens
    # ------------------------------------------------------------------

    def save_refresh_token(
        self,
        token: str,
        client_id: str,
        scopes: list[str],
        subject: str | None = None,
        expires_at: int | None = None,
        resource: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO oauth_refresh_tokens
                        (token, client_id, scopes, expires_at, subject, resource, created_at)
                    VALUES (:token, :client_id, :scopes, :expires_at, :subject, :resource, :created_at)
                    ON CONFLICT (token) DO UPDATE SET
                        client_id  = EXCLUDED.client_id,
                        scopes     = EXCLUDED.scopes,
                        expires_at = EXCLUDED.expires_at,
                        subject    = EXCLUDED.subject,
                        resource   = EXCLUDED.resource
                    """
                ),
                {
                    "token": _hash(token),
                    "client_id": client_id,
                    "scopes": json.dumps(scopes),
                    "expires_at": expires_at,
                    "subject": subject,
                    "resource": resource,
                    "created_at": now,
                },
            )

    def get_refresh_token(self, token: str) -> Optional[dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM oauth_refresh_tokens WHERE token = :t"),
                    {"t": _hash(token)},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        data = dict(row)
        data["scopes"] = json.loads(data["scopes"] or "[]")
        return data

    def revoke_refresh_token(self, token: str) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE oauth_refresh_tokens SET revoked_at = :now WHERE token = :t"),
                {"now": now, "t": _hash(token)},
            )
