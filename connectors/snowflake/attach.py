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
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from src.orchestrator_security import escape_sql_string_literal, is_attach_host_allowed

logger = logging.getLogger(__name__)

# Dollar-quote tag for the PEM inside CREATE SECRET. Named once so the guard in
# `_private_key_pem_and_passphrase` and the SQL below can never drift apart.
_DOLLAR_TAG = "$PK$"

SF_ALIAS = "sf"
SF_EXTENSION = "snowflake"
SF_TOKEN_ENV = "SNOWFLAKE_PASSWORD"

# Account strings can include region/location separators and dots.
_SAFE_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Database/warehouse/user/role are Snowflake identifiers; keep the regex
# linear-time and conservative.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _duckdb_extension_dir() -> Path:
    """Return the DuckDB community-extension directory for this version/platform."""
    import duckdb
    import platform as _platform

    machine = _platform.machine().lower()
    system = sys.platform
    if system == "linux":
        base = "linux"
    elif system == "darwin":
        base = "osx"
    elif system.startswith("win"):
        base = "windows"
    else:
        base = system
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        arch = machine
    return Path.home() / ".duckdb" / "extensions" / f"v{duckdb.__version__}" / f"{base}_{arch}"


def _find_adbc_driver_library() -> Path | None:
    """Locate the ADBC Snowflake shared library shipped with ``adbc-driver-snowflake``."""
    try:
        import adbc_driver_snowflake

        pkg_dir = Path(adbc_driver_snowflake.__file__).parent
        for name in (
            "libadbc_driver_snowflake.so",
            "libadbc_driver_snowflake.dylib",
            "adbc_driver_snowflake.dll",
        ):
            candidate = pkg_dir / name
            if candidate.exists():
                return candidate
    except Exception:
        pass

    for p in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep):
        for name in ("libadbc_driver_snowflake.so", "libadbc_driver_snowflake.dylib", "adbc_driver_snowflake.dll"):
            candidate = Path(p) / name
            if candidate.exists():
                return candidate

    for p in ("/usr/local/lib", "/usr/lib"):
        for name in ("libadbc_driver_snowflake.so", "libadbc_driver_snowflake.dylib", "adbc_driver_snowflake.dll"):
            candidate = Path(p) / name
            if candidate.exists():
                return candidate
    return None


def install_snowflake_adbc_driver(*, missing_ok: bool = True) -> None:
    """Copy the ADBC Snowflake driver into DuckDB's extension directory.

    DuckDB's ``snowflake`` community extension looks for ``libadbc_driver_snowflake.*``
    in ``~/.duckdb/extensions/v<version>/<platform>/``. The ``adbc-driver-snowflake``
    Python package ships the matching shared library, but in its own site-packages
    directory, so this helper performs a one-time copy to the directory DuckDB
    actually searches. It is safe to call repeatedly: if the driver is already
    present it returns immediately.
    """
    ext_dir = _duckdb_extension_dir()
    source = _find_adbc_driver_library()

    if source is None:
        if missing_ok:
            logger.warning(
                "adbc_driver_snowflake not found; Snowflake extension may fail to load. "
                "Install it (scripts/install-adbc-driver.sh) or set LD_LIBRARY_PATH."
            )
            return
        raise RuntimeError(
            "ADBC Snowflake driver not found. Install it with scripts/install-adbc-driver.sh "
            "or 'uv pip install --python .venv/bin/python adbc-driver-snowflake'."
        )

    target = ext_dir / source.name
    if target.exists():
        return

    ext_dir.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_suffix(f"{target.suffix}.tmp")
    try:
        shutil.copy2(source, tmp_target)
        os.replace(tmp_target, target)
        logger.info("Copied ADBC Snowflake driver to %s", target)
    except Exception:
        if tmp_target.exists():
            tmp_target.unlink(missing_ok=True)
        raise


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


def _private_key_pem_and_passphrase(token: str, passphrase: str | None = None) -> tuple[str, str | None]:
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

    # The PEM goes into the CREATE SECRET statement inside `$PK$ … $PK$`
    # dollar-quoting (a PEM carries newlines, so it is not written as a plain
    # quoted literal). That was safe while this function REBUILT the key via
    # `private_key.private_bytes(...)` — generated output cannot contain the
    # tag. Now that the operator's bytes are forwarded verbatim so DuckDB can
    # decrypt them itself, the tag has to be refused explicitly: a key whose
    # text contains `$PK$` would close the literal early and inject the rest of
    # itself as SQL into a statement that carries a credential. A real PEM is
    # base64 plus `-----BEGIN/END-----` armour and never contains `$`.
    if _DOLLAR_TAG in raw:
        raise ValueError("snowflake private key contains the SQL dollar-quote tag; refusing to build a secret")

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
            f"PRIVATE_KEY {_DOLLAR_TAG}{private_key_pem}{_DOLLAR_TAG}, "
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
            f"Snowflake host {url!r} is not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST; refusing to send credential"
        )

    params = parse_remote_attach_url(url)
    secret_name = f"sf_secret_{alias}"

    secret_sql = _create_snowflake_secret_sql(secret_name, params, token, passphrase)
    conn.execute(secret_sql)
    conn.execute(f"ATTACH '' AS {alias} (TYPE {SF_EXTENSION}, SECRET {secret_name}, READ_ONLY)")
    logger.info("Attached Snowflake database %r as catalog %r", params["database"], alias)
