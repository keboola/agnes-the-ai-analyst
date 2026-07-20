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
                sa.text("SELECT secret_value_enc FROM mcp_secrets WHERE source_id = :source_id"),
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
                "mcp_secrets row for %s failed to decrypt — vault key rotated? Falling back to env-var lookup.",
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
                sa.text("SELECT 1 FROM mcp_secrets WHERE source_id = :source_id LIMIT 1"),
                {"source_id": source_id},
            ).fetchone()
        return row is not None


class SystemSecretsPgRepository:
    """Server-wide system secrets keyed by ``name`` (PG).

    Signature-compatible with ``app.secrets_vault.SystemSecretsRepository``.
    """

    def __init__(self, engine: Engine):
        self._engine = engine

    def upsert(self, name: str, value: str) -> None:
        token = encrypt_secret(value)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO system_secrets
                           (name, secret_value_enc, updated_at)
                       VALUES (:name, :token, CURRENT_TIMESTAMP)
                       ON CONFLICT (name) DO UPDATE SET
                           secret_value_enc = EXCLUDED.secret_value_enc,
                           updated_at       = EXCLUDED.updated_at"""
                ),
                {"name": name, "token": token},
            )

    def get(self, name: str) -> Optional[str]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT secret_value_enc FROM system_secrets WHERE name = :name"),
                {"name": name},
            ).fetchone()
        if row is None:
            return None
        token = row[0]
        if not isinstance(token, (bytes, bytearray, memoryview)):
            return None
        try:
            return decrypt_secret(bytes(token))
        except (InvalidToken, RuntimeError):
            logger.warning(
                "system_secrets row for %s failed to decrypt — vault key rotated or malformed? Treating as unset.",
                name,
            )
            return None

    def delete(self, name: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM system_secrets WHERE name = :name"),
                {"name": name},
            )

    def has(self, name: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT 1 FROM system_secrets WHERE name = :name LIMIT 1"),
                {"name": name},
            ).fetchone()
        return row is not None

    def list_names_with_prefix(self, prefix: str) -> list[str]:
        """Names of every row whose ``name`` starts with ``prefix``, sorted.

        Signature-compatible with the DuckDB sibling
        (``app.secrets_vault.SystemSecretsRepository.list_names_with_prefix``)
        — see its docstring for why ``position(... IN ...)`` is used instead
        of ``LIKE``.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT name FROM system_secrets WHERE position(:prefix IN name) = 1 ORDER BY name"),
                {"prefix": prefix},
            ).fetchall()
        return [r[0] for r in rows]


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
                    "SELECT secret_value_enc FROM mcp_user_secrets WHERE source_id = :source_id AND user_id = :user_id"
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
                source_id,
                user_id,
            )
            return None

    def delete(self, source_id: str, user_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM mcp_user_secrets WHERE source_id = :source_id AND user_id = :user_id"),
                {"source_id": source_id, "user_id": user_id},
            )

    def has(self, source_id: str, user_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT 1 FROM mcp_user_secrets WHERE source_id = :source_id AND user_id = :user_id LIMIT 1"),
                {"source_id": source_id, "user_id": user_id},
            ).fetchone()
        return row is not None

    def get_updated_at(self, source_id: str, user_id: str) -> Optional[str]:
        """ISO-8601 timestamp of the last upsert for ``(source_id, user_id)``,
        or ``None`` when not connected. Never returns the secret value."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT updated_at FROM mcp_user_secrets WHERE source_id = :source_id AND user_id = :user_id"),
                {"source_id": source_id, "user_id": user_id},
            ).fetchone()
        return row[0].isoformat() if row and row[0] is not None else None

    def list_for_source(self, source_id: str) -> list[str]:
        """List of user_ids that have stored a secret for this source.

        Powers the admin diagnostic ``agnes admin mcp source who-has-secret``
        without leaking any cipher text — secret values are never returned.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT user_id FROM mcp_user_secrets WHERE source_id = :source_id ORDER BY user_id"),
                {"source_id": source_id},
            ).fetchall()
        return [r[0] for r in rows]


class ConnectionSecretsPgRepository:
    """Vault scope for source_connections tokens (PG).

    Signature-compatible with ``app.secrets_vault.ConnectionSecretsRepository``.
    """

    def __init__(self, engine: Engine):
        self._engine = engine

    def upsert(self, connection_id: str, value: str) -> None:
        # Store the Fernet token as text (URL-safe base64) — matches the
        # `ciphertext TEXT` column and the DuckDB sibling. Storing raw bytes
        # into a text column round-trips through a bytes-repr string and then
        # fails to decrypt on read.
        token = encrypt_secret(value).decode()
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO connection_secrets
                           (connection_id, ciphertext, updated_at)
                       VALUES (:connection_id, :token, CURRENT_TIMESTAMP)
                       ON CONFLICT (connection_id) DO UPDATE SET
                           ciphertext = EXCLUDED.ciphertext,
                           updated_at = EXCLUDED.updated_at"""
                ),
                {"connection_id": connection_id, "token": token},
            )

    def get(self, connection_id: str) -> Optional[str]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT ciphertext FROM connection_secrets WHERE connection_id = :connection_id"),
                {"connection_id": connection_id},
            ).fetchone()
        if row is None:
            return None
        token = row[0]
        if not isinstance(token, (bytes, bytearray, memoryview)):
            # stored as text (Fernet token is URL-safe base64)
            token = token.encode() if isinstance(token, str) else bytes(token)
        else:
            token = bytes(token)
        try:
            return decrypt_secret(token)
        except InvalidToken:
            logger.warning(
                "connection_secrets row for %s failed to decrypt — vault key rotated?",
                connection_id,
            )
            return None

    def delete(self, connection_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM connection_secrets WHERE connection_id = :connection_id"),
                {"connection_id": connection_id},
            )

    def has(self, connection_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT 1 FROM connection_secrets WHERE connection_id = :connection_id LIMIT 1"),
                {"connection_id": connection_id},
            ).fetchone()
        return row is not None
