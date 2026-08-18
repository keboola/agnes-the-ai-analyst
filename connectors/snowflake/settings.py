"""Resolve Snowflake connection settings from instance.yaml / env / vault."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

SF_TOKEN_ENV = "SNOWFLAKE_PASSWORD"


def resolve_snowflake_settings() -> Optional[dict[str, Any]]:
    """Return Snowflake settings, or ``None`` if the instance is not configured.

    Resolution order mirrors the other connectors: non-secret coordinates live
    in ``data_source.snowflake`` (instance.yaml or /admin/server-config); the
    password comes from the environment variable named by ``token_env`` (default
    ``SNOWFLAKE_PASSWORD``), falling back to ``app.datasource_secrets`` for the
    vault-backed value.
    """
    from app.datasource_secrets import datasource_secret
    from app.instance_config import get_value

    account = get_value("data_source", "snowflake", "account", default="") or ""
    user = get_value("data_source", "snowflake", "user", default="") or ""
    database = get_value("data_source", "snowflake", "database", default="") or ""
    warehouse = get_value("data_source", "snowflake", "warehouse", default="") or ""
    role = get_value("data_source", "snowflake", "role", default="") or ""
    token_env = get_value("data_source", "snowflake", "token_env", default=SF_TOKEN_ENV) or SF_TOKEN_ENV

    password = os.environ.get(token_env, "")
    if not password:
        try:
            password = datasource_secret(token_env) or ""
        except Exception:
            password = ""

    if not (account and user and password and database and warehouse):
        return None

    return {
        "account": account,
        "user": user,
        "password": password,
        "database": database,
        "warehouse": warehouse,
        "role": role,
        "token_env": token_env,
    }
