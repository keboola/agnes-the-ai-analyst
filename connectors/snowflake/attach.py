"""Snowflake remote ATTACH helpers.

DuckDB's ``snowflake`` community extension connects to a Snowflake account
through a SECRET that carries ACCOUNT, USER, PASSWORD, DATABASE and WAREHOUSE.
The non-secret coordinates are packed into the ``_remote_attach`` ``url``
column so the orchestrator can gate credential egress with the host allowlist
before creating the SECRET and ATTACHing the catalog.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from src.orchestrator_security import is_attach_host_allowed

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


def attach_snowflake(conn, *, alias: str, url: str, token: str) -> None:
    """Install/load the Snowflake extension, create a SECRET, and ATTACH.

    ``url`` carries the non-secret coordinates; ``token`` is the Snowflake
    password. Identifiers for ``alias`` and the derived SECRET name must be
    safe before this is called (the orchestrator validates the alias).
    """
    from src.orchestrator_security import escape_sql_string_literal

    if not is_attach_host_allowed(url):
        raise ValueError(
            f"Snowflake host {url!r} is not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST; "
            "refusing to send credential"
        )

    params = parse_remote_attach_url(url)
    secret_name = f"sf_secret_{alias}"

    role_sql = ""
    if params.get("role"):
        role_sql = f", ROLE '{escape_sql_string_literal(params['role'])}'"

    conn.execute(
        f"CREATE OR REPLACE SECRET {secret_name} ("
        f"TYPE snowflake, "
        f"ACCOUNT '{escape_sql_string_literal(params['account'])}', "
        f"USER '{escape_sql_string_literal(params['user'])}', "
        f"PASSWORD '{escape_sql_string_literal(token)}', "
        f"DATABASE '{escape_sql_string_literal(params['database'])}', "
        f"WAREHOUSE '{escape_sql_string_literal(params['warehouse'])}'"
        f"{role_sql})"
    )
    conn.execute(
        f"ATTACH '' AS {alias} (TYPE {SF_EXTENSION}, SECRET {secret_name}, READ_ONLY)"
    )
    logger.info("Attached Snowflake database %r as catalog %r", params["database"], alias)
