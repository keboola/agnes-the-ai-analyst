"""Resolve Snowflake connection settings from instance.yaml / env / vault."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

SF_TOKEN_ENV = "SNOWFLAKE_PASSWORD"
SF_PRIVATE_KEY_ENV = "SNOWFLAKE_PRIVATE_KEY"
SF_PRIVATE_KEY_PASSPHRASE_ENV = "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"


def _resolve_secret(name: str) -> str:
    """Resolve a named credential: env var first, then the vault."""
    from app.datasource_secrets import datasource_secret

    value = os.environ.get(name, "")
    if not value:
        try:
            value = datasource_secret(name) or ""
        except Exception:
            value = ""
    return value


def resolve_snowflake_settings() -> Optional[dict[str, Any]]:
    """Return Snowflake settings, or ``None`` if the instance is not configured.

    Resolution order mirrors the other connectors: non-secret coordinates live
    in ``data_source.snowflake`` (instance.yaml or /admin/server-config); the
    credential (password or key-pair) comes from the environment variable named
    by ``token_env`` / ``private_key_env`` (default ``SNOWFLAKE_PASSWORD`` /
    ``SNOWFLAKE_PRIVATE_KEY``), falling back to ``app.datasource_secrets`` for
    the vault-backed value.
    """
    from app.instance_config import get_value

    account = get_value("data_source", "snowflake", "account", default="") or ""
    user = get_value("data_source", "snowflake", "user", default="") or ""
    database = get_value("data_source", "snowflake", "database", default="") or ""
    warehouse = get_value("data_source", "snowflake", "warehouse", default="") or ""
    role = get_value("data_source", "snowflake", "role", default="") or ""
    auth_type = get_value("data_source", "snowflake", "auth_type", default="password") or "password"

    if auth_type == "key_pair":
        private_key_env = get_value("data_source", "snowflake", "private_key_env", default=SF_PRIVATE_KEY_ENV) or SF_PRIVATE_KEY_ENV
        private_key_passphrase_env = (
            get_value("data_source", "snowflake", "private_key_passphrase_env", default=SF_PRIVATE_KEY_PASSPHRASE_ENV)
            or SF_PRIVATE_KEY_PASSPHRASE_ENV
        )
        private_key = _resolve_secret(private_key_env)
        private_key_passphrase = _resolve_secret(private_key_passphrase_env)
        if not (account and user and private_key and database and warehouse):
            return None
        return {
            "account": account,
            "user": user,
            "database": database,
            "warehouse": warehouse,
            "role": role,
            "auth_type": auth_type,
            "private_key": private_key,
            "private_key_passphrase": private_key_passphrase,
            "private_key_env": private_key_env,
            "private_key_passphrase_env": private_key_passphrase_env,
        }

    token_env = get_value("data_source", "snowflake", "token_env", default=SF_TOKEN_ENV) or SF_TOKEN_ENV
    password = _resolve_secret(token_env)
    if not (account and user and password and database and warehouse):
        return None

    return {
        "account": account,
        "user": user,
        "password": password,
        "database": database,
        "warehouse": warehouse,
        "role": role,
        "auth_type": auth_type,
        "token_env": token_env,
    }
