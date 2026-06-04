"""Postgres-backed MCP secret vault repositories.

Mirrors the DuckDB repositories in ``app/secrets_vault.py``
(``SharedSecretsRepository`` / ``PerUserSecretsRepository``). The Fernet
cipher helpers (``encrypt_secret`` / ``decrypt_secret``) are
backend-agnostic and reused verbatim — only the storage layer differs.

Tables:

* ``mcp_secrets``       — server-wide, one row per ``source_id`` (v65).
* ``mcp_user_secrets``  — per-user, one row per ``(source_id, user_id)`` (v66).

``secret_value_enc`` is a ``BYTEA`` column on Postgres; SQLAlchemy returns
it as ``bytes``, matching the DuckDB ``BLOB`` round-trip.
"""
from __future__ import annotations

import logging
from typing import Optional

import sqlalchemy as sa
from cryptography.fernet import InvalidToken
from sqlalchemy.engine import Engine

from app.secrets_vault import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)


class SharedSecretsPgRepository:
    """Server-wide MCP source secrets (PG). One row per ``source_id``.

    Signature-compatible with ``app.secrets_vault.SharedSecretsRepository``.
    Without this, shared MCP credentials lived only in the DuckDB system file
    even on a Postgres instance — lost on a DuckDB reset, and inconsistent with
    the per-user secrets that already moved to PG.
    """

    def __init__(self, engine: Engine):
        self._engine = engine

    def upsert(self, source_id: str, value: str) -> None:
        token = encrypt_secret(value)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO mcp_secrets
                           (source_id, secret_value_enc, updated_at)
                       VALUES (:source_id, :token, CURRENT_TIMESTAMP)
                       ON CONFLICT (source_id) DO UPDATE SET
                           secret_value_enc = EXCLUDED.secret_value_enc,
                           updated_at       = EXCLUDED.updated_at"""
                ),
                {"source_id": source_id, "token": token},
            )

    def get(self, source_id: str) -> Optional[str]:
        """Decrypted secret for ``source_id`` or ``None`` if absent or
        undecryptable (key rotation). Junk decrypt returns ``None`` so the
        caller can fall back to the env-var path."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT secret_value_enc FROM mcp_secrets "
                    "WHERE source_id = :source_id"
                ),
                {"source_id": source_id},
            ).fetchone()
        if row is None:
            return None
        token = row[0]
        if not isinstance(token, (bytes, bytearray, memoryview)):
            return None
        try:
            return decrypt_secret(bytes(token))
        except InvalidToken:
            logger.warning(
                "mcp_secrets row for %s failed to decrypt — vault key rotated? "
                "Falling back to env-var lookup.",
                source_id,
            )
            return None

    def delete(self, source_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM mcp_secrets WHERE source_id = :source_id"),
                {"source_id": source_id},
            )

    def has(self, source_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM mcp_secrets WHERE source_id = :source_id LIMIT 1"
                ),
                {"source_id": source_id},
            ).fetchone()
        return row is not None


class PerUserSecretsPgRepository:
    """Per-user MCP source secrets (PG). One row per ``(source_id, user_id)``.

    Signature-compatible with
    ``app.secrets_vault.PerUserSecretsRepository``.
    """

    def __init__(self, engine: Engine):
        self._engine = engine

    def upsert(self, source_id: str, user_id: str, value: str) -> None:
        token = encrypt_secret(value)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO mcp_user_secrets
                           (source_id, user_id, secret_value_enc, updated_at)
                       VALUES (:source_id, :user_id, :token, CURRENT_TIMESTAMP)
                       ON CONFLICT (source_id, user_id) DO UPDATE SET
                           secret_value_enc = EXCLUDED.secret_value_enc,
                           updated_at       = EXCLUDED.updated_at"""
                ),
                {"source_id": source_id, "user_id": user_id, "token": token},
            )

    def get(self, source_id: str, user_id: str) -> Optional[str]:
        """Decrypted secret for ``(source_id, user_id)`` or ``None`` if absent
        or undecryptable (key rotation). Junk decrypt returns ``None`` so the
        caller can fall back to the shared path."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT secret_value_enc FROM mcp_user_secrets "
                    "WHERE source_id = :source_id AND user_id = :user_id"
                ),
                {"source_id": source_id, "user_id": user_id},
            ).fetchone()
        if row is None:
            return None
        token = row[0]
        if not isinstance(token, (bytes, bytearray, memoryview)):
            return None
        try:
            return decrypt_secret(bytes(token))
        except InvalidToken:
            logger.warning(
                "mcp_user_secrets row (%s, %s) failed to decrypt — vault key "
                "rotated? Falling back to shared vault / env-var.",
                source_id, user_id,
            )
            return None

    def delete(self, source_id: str, user_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM mcp_user_secrets "
                    "WHERE source_id = :source_id AND user_id = :user_id"
                ),
                {"source_id": source_id, "user_id": user_id},
            )

    def has(self, source_id: str, user_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM mcp_user_secrets "
                    "WHERE source_id = :source_id AND user_id = :user_id LIMIT 1"
                ),
                {"source_id": source_id, "user_id": user_id},
            ).fetchone()
        return row is not None

    def list_for_source(self, source_id: str) -> list[str]:
        """List of user_ids that have stored a secret for this source.

        Powers the admin diagnostic ``agnes admin mcp source who-has-secret``
        without leaking any cipher text — secret values are never returned.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT user_id FROM mcp_user_secrets "
                    "WHERE source_id = :source_id ORDER BY user_id"
                ),
                {"source_id": source_id},
            ).fetchall()
        return [r[0] for r in rows]
