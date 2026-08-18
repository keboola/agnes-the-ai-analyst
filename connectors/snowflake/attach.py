"""Snowflake remote ATTACH helpers.

DuckDB's ``snowflake`` community extension connects to a Snowflake account
through a SECRET that carries ACCOUNT, USER, PASSWORD or KEY-PAIR, DATABASE
and WAREHOUSE. The non-secret coordinates are packed into the ``_remote_attach``
``url`` column so the orchestrator can gate credential egress with the host
allowlist before creating the SECRET and ATTACHing the catalog.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from src.orchestrator_security import escape_sql_string_literal, is_attach_host_allowed

logger = logging.getLogger(__name__)

SF_ALIAS = "sf"
SF_EXTENSION = "snowflake"
SF_TOKEN_ENV = "SNOWFLAKE_PASSWORD"

# Account strings can include region/location separators and dots.
_SAFE_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Database/warehouse/user/role are Snowflake identifiers; keep the regex
# linear-time and conservative.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _is_safe_segment(value: str, name: str) -> str:
    """Validate a URL path/query segment, raising ``ValueError`` if unsafe."""
    if not value or not _SAFE_SEGMENT_RE.match(value):
        raise ValueError(f"unsafe or empty snowflake {name}: {value!r}")
    return value


def _is_safe_account(value: str) -> str:
    if not value or not _SAFE_ACCOUNT_RE.match(value):
        raise ValueError(f"unsafe or empty snowflake account: {value!r}")
    return value


def build_remote_attach_url(
    account: str,
    database: str,
    warehouse: str,
    user: str,
    role: str = "",
) -> str:
    """Pack Snowflake connection coordinates into a single HTTPS URL.

    The account may already end with ``.snowflakecomputing.com``; otherwise
    the canonical host suffix is appended. Query parameters carry the
    database, warehouse, user and optional role. The password is intentionally
    NOT included here — it travels through the ``token_env`` column.
    """
    account = _is_safe_account(account.strip())
    database = _is_safe_segment(database.strip(), "database")
    warehouse = _is_safe_segment(warehouse.strip(), "warehouse")
    user = _is_safe_segment(user.strip(), "user")

    if account.lower().endswith(".snowflakecomputing.com"):
        host = account
    else:
        host = f"{account}.snowflakecomputing.com"

    query: dict[str, str] = {
        "database": database,
        "warehouse": warehouse,
        "user": user,
    }
    role = (role or "").strip()
    if role:
        query["role"] = _is_safe_segment(role, "role")

    return f"https://{host}?{urlencode(query)}"


def parse_remote_attach_url(url: str) -> dict[str, str]:
    """Reverse ``build_remote_attach_url`` for the orchestrator side."""
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(
            f"snowflake _remote_attach url must be https://<account>.snowflakecomputing.com?...; got {url!r}"
        )

    host = parts.hostname
    if host.lower().endswith(".snowflakecomputing.com"):
        account = host[: -len(".snowflakecomputing.com")]
    else:
        account = host

    account = _is_safe_account(account)

    qs = parse_qs(parts.query, keep_blank_values=True)

    def _first(key: str) -> str:
        vals = qs.get(key, [])
        return vals[0] if vals else ""

    database = _is_safe_segment(_first("database"), "database")
    warehouse = _is_safe_segment(_first("warehouse"), "warehouse")
    user = _is_safe_segment(_first("user"), "user")
    role = _first("role").strip()
    if role:
        role = _is_safe_segment(role, "role")

    return {
        "account": account,
        "database": database,
        "warehouse": warehouse,
        "user": user,
        "role": role,
    }


def _looks_like_key_pair(token: str) -> bool:
    """Return True when ``token`` is a PEM key or a JSON key envelope."""
    t = (token or "").strip()
    if "-----BEGIN" in t and "-----END" in t:
        return True
    if t.startswith("{") and t.endswith("}"):
        try:
            data = json.loads(t)
        except json.JSONDecodeError:
            return False
        return isinstance(data, dict) and "private_key" in data
    return False


def _private_key_pem_and_passphrase(
    token: str, passphrase: str | None = None
) -> tuple[str, str | None]:
    """Extract a PEM or JSON-wrapped PEM and the matching passphrase.

    DuckDB's Snowflake extension accepts either an unencrypted PKCS#8 PEM or an
    encrypted PEM plus ``PRIVATE_KEY_PASSPHRASE``; it decrypts the key itself,
    so we never need to unwrap it locally.
    """
    raw = (token or "").strip()
    if not raw:
        raise ValueError("empty snowflake private key")

    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"snowflake private key is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("snowflake private key JSON must be an object")
        raw = data.get("private_key") or ""
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("snowflake private key JSON missing 'private_key'")
        if not passphrase:
            passphrase = data.get("passphrase") or None

    if "-----BEGIN" not in raw or "-----END" not in raw:
        raise ValueError("snowflake private key is not a PEM block")

    return raw, passphrase


def _create_snowflake_secret_sql(
    secret_name: str,
    params: dict[str, str],
    token: str,
    passphrase: str | None = None,
) -> str:
    """Return the ``CREATE OR REPLACE SECRET`` SQL for Snowflake."""
    role_sql = ""
    if params.get("role"):
        role_sql = f", ROLE '{escape_sql_string_literal(params['role'])}'"

    if _looks_like_key_pair(token):
        private_key_pem, key_passphrase = _private_key_pem_and_passphrase(token, passphrase)
        passphrase_sql = ""
        if key_passphrase:
            passphrase_sql = f", PRIVATE_KEY_PASSPHRASE '{escape_sql_string_literal(key_passphrase)}'"
        return (
            f"CREATE OR REPLACE SECRET {secret_name} ("
            f"TYPE snowflake, "
            f"ACCOUNT '{escape_sql_string_literal(params['account'])}', "
            f"USER '{escape_sql_string_literal(params['user'])}', "
            f"AUTH_TYPE 'key_pair', "
            f"PRIVATE_KEY $PK${private_key_pem}$PK$, "
            f"DATABASE '{escape_sql_string_literal(params['database'])}', "
            f"WAREHOUSE '{escape_sql_string_literal(params['warehouse'])}'"
            f"{role_sql}"
            f"{passphrase_sql})"
        )

    return (
        f"CREATE OR REPLACE SECRET {secret_name} ("
        f"TYPE snowflake, "
        f"ACCOUNT '{escape_sql_string_literal(params['account'])}', "
        f"USER '{escape_sql_string_literal(params['user'])}', "
        f"PASSWORD '{escape_sql_string_literal(token)}', "
        f"DATABASE '{escape_sql_string_literal(params['database'])}', "
        f"WAREHOUSE '{escape_sql_string_literal(params['warehouse'])}'"
        f"{role_sql})"
    )


def attach_snowflake(
    conn,
    *,
    alias: str,
    url: str,
    token: str,
    passphrase: str | None = None,
) -> None:
    """Install/load the Snowflake extension, create a SECRET, and ATTACH.

    ``url`` carries the non-secret coordinates; ``token`` is either the
    Snowflake password or a PEM / JSON-wrapped private key. Identifiers for
    ``alias`` and the derived SECRET name must be safe before this is called
    (the orchestrator validates the alias).
    """
    if not is_attach_host_allowed(url):
        raise ValueError(
            f"Snowflake host {url!r} is not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST; "
            "refusing to send credential"
        )

    params = parse_remote_attach_url(url)
    secret_name = f"sf_secret_{alias}"

    secret_sql = _create_snowflake_secret_sql(secret_name, params, token, passphrase)
    conn.execute(secret_sql)
    conn.execute(
        f"ATTACH '' AS {alias} (TYPE {SF_EXTENSION}, SECRET {secret_name}, READ_ONLY)"
    )
    logger.info("Attached Snowflake database %r as catalog %r", params["database"], alias)
