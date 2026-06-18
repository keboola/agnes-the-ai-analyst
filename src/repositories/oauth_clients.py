"""DuckDB-backed repository for OAuth 2.1 client registrations and tokens.

Stores:
  - oauth_clients       — dynamic client registrations (RFC 7591)
  - oauth_auth_codes    — short-lived PKCE authorization codes
  - oauth_access_tokens — issued access tokens (for load_access_token)
  - oauth_refresh_tokens — issued refresh tokens
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import duckdb


class OAuthClientsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

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
        self.conn.execute(
            """
            INSERT INTO oauth_clients
                (client_id, client_secret, redirect_uris, client_name,
                 client_metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (client_id) DO UPDATE SET
                client_secret   = excluded.client_secret,
                redirect_uris   = excluded.redirect_uris,
                client_name     = excluded.client_name,
                client_metadata = excluded.client_metadata
            """,
            [
                client_id,
                client_secret,
                json.dumps(redirect_uris or []),
                client_name,
                json.dumps(client_metadata or {}),
                now,
            ],
        )

    def get_client(self, client_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM oauth_clients WHERE client_id = ?",
            [client_id],
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self.conn.description]
        data = dict(zip(cols, row))
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
        self.conn.execute(
            """
            INSERT INTO oauth_auth_codes
                (code, client_id, scopes, code_challenge, redirect_uri,
                 redirect_uri_provided_explicitly, expires_at, subject, resource, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (code) DO UPDATE SET
                client_id   = excluded.client_id,
                scopes      = excluded.scopes,
                code_challenge = excluded.code_challenge,
                redirect_uri = excluded.redirect_uri,
                redirect_uri_provided_explicitly = excluded.redirect_uri_provided_explicitly,
                expires_at  = excluded.expires_at,
                subject     = excluded.subject,
                resource    = excluded.resource,
                state       = excluded.state
            """,
            [
                code,
                client_id,
                json.dumps(scopes),
                code_challenge,
                redirect_uri,
                redirect_uri_provided_explicitly,
                expires_at,
                subject,
                resource,
                state,
            ],
        )

    def get_auth_code(self, code: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM oauth_auth_codes WHERE code = ?",
            [code],
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self.conn.description]
        data = dict(zip(cols, row))
        data["scopes"] = json.loads(data["scopes"] or "[]")
        data["redirect_uri_provided_explicitly"] = bool(data["redirect_uri_provided_explicitly"])
        return data

    def delete_auth_code(self, code: str) -> None:
        self.conn.execute(
            "DELETE FROM oauth_auth_codes WHERE code = ?",
            [code],
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
        self.conn.execute(
            """
            INSERT INTO oauth_access_tokens
                (token, client_id, scopes, expires_at, subject, resource, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (token) DO UPDATE SET
                client_id  = excluded.client_id,
                scopes     = excluded.scopes,
                expires_at = excluded.expires_at,
                subject    = excluded.subject,
                resource   = excluded.resource
            """,
            [token, client_id, json.dumps(scopes), expires_at, subject, resource, now],
        )

    def get_access_token(self, token: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM oauth_access_tokens WHERE token = ?",
            [token],
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self.conn.description]
        data = dict(zip(cols, row))
        data["scopes"] = json.loads(data["scopes"] or "[]")
        return data

    def revoke_access_token(self, token: str) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE oauth_access_tokens SET revoked_at = ? WHERE token = ?",
            [now, token],
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
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """
            INSERT INTO oauth_refresh_tokens
                (token, client_id, scopes, expires_at, subject, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (token) DO UPDATE SET
                client_id  = excluded.client_id,
                scopes     = excluded.scopes,
                expires_at = excluded.expires_at,
                subject    = excluded.subject
            """,
            [token, client_id, json.dumps(scopes), expires_at, subject, now],
        )

    def get_refresh_token(self, token: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM oauth_refresh_tokens WHERE token = ?",
            [token],
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self.conn.description]
        data = dict(zip(cols, row))
        data["scopes"] = json.loads(data["scopes"] or "[]")
        return data

    def revoke_refresh_token(self, token: str) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE oauth_refresh_tokens SET revoked_at = ? WHERE token = ?",
            [now, token],
        )
