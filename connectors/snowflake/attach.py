"""Snowflake remote ATTACH helpers.

DuckDB's ``snowflake`` community extension connects to a Snowflake account
through a SECRET that carries ACCOUNT, USER, PASSWORD or KEY-PAIR, DATABASE
and WAREHOUSE. The non-secret coordinates are packed into the ``_remote_attach``
``url`` column so the orchestrator can gate credential egress with the host
allowlist before creating the SECRET and ATTACHing the catalog.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives import serialization

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
_PEM_BLOCK_RE = re.compile(
    r"(-----BEGIN [A-Z0-9 _]+-----)\s*(.*?)\s*(-----END [A-Z0-9 _]+-----)",
    re.DOTALL,
)


def _normalize_key_text(text: str) -> str:
    """Unescape literal newlines and normalize CRLF/CR to LF."""
    s = text.replace("\\r\\n", "\n").replace("\\r", "\n").replace("\\n", "\n")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.strip()


def _load_private_key(data: bytes, passphrase: str | None = None):
    """Load a PEM or DER private key, optionally decrypting it."""
    password = passphrase.encode("utf-8") if passphrase else None

    if b"-----BEGIN" in data:
        try:
            return serialization.load_pem_private_key(data, password=password)
        except TypeError as exc:
            msg = str(exc)
            if "Password was given but private key is not encrypted" in msg:
                return serialization.load_pem_private_key(data, password=None)
            raise ValueError(msg) from exc
        except Exception as exc:
            raise ValueError(f"could not parse PEM private key: {exc}") from exc

    try:
        return serialization.load_der_private_key(data, password=password)
    except TypeError as exc:
        msg = str(exc)
        if "Password was given but private key is not encrypted" in msg:
            return serialization.load_der_private_key(data, password=None)
        raise ValueError(msg) from exc
    except Exception as exc:
        raise ValueError(f"could not parse DER private key: {exc}") from exc


def _serialize_unencrypted_pkcs8_pem(key) -> str:
    """Return a PKCS#8 unencrypted PEM for the loaded key."""
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _try_decode_base64_key(text: str) -> bytes:
    """Decode a base64-only key string, tolerating whitespace and URL-safe alphabet."""
    cleaned = re.sub(r"\s+", "", text)
    if not cleaned:
        raise ValueError("empty base64 key")
    for candidate in (cleaned, cleaned.replace("-", "+").replace("_", "/")):
        try:
            return base64.b64decode(candidate, validate=True)
        except Exception:
            continue
    raise ValueError("not a valid base64 key")


def _reformat_pem_body(s: str) -> str:
    """Rewrap a single-line base64 PEM body to 64-char lines.

    Encrypted PKCS#1 blocks that carry header lines (``Proc-Type:``) are left
    untouched so the header formatting is not corrupted.
    """

    def repl(m: re.Match[str]) -> str:
        begin, body, end = m.group(1), m.group(2), m.group(3)
        if ":" in body:
            return m.group(0)
        cleaned = re.sub(r"\s+", "", body)
        if not re.fullmatch(r"[A-Za-z0-9+/=]+", cleaned):
            return m.group(0)
        wrapped = "\n".join(cleaned[i : i + 64] for i in range(0, len(cleaned), 64))
        return f"{begin}\n{wrapped}\n{end}"

    return _PEM_BLOCK_RE.sub(repl, s)


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
    """Return True when ``token`` is a PEM, JSON-wrapped PEM, or base64 DER key."""
    t = (token or "").strip()
    if "-----BEGIN" in t and "-----END" in t:
        return True
    if t.startswith("{") and t.endswith("}"):
        try:
            data = json.loads(t)
        except json.JSONDecodeError:
            return False
        return isinstance(data, dict) and "private_key" in data
    # Heuristic for a pasted base64-only DER key (Snowflake keys are long RSA
    # keys; a short base64 password is left to the password path).
    cleaned = re.sub(r"\s+", "", t)
    if len(cleaned) >= 256:
        for candidate in (cleaned, cleaned.replace("-", "+").replace("_", "/")):
            try:
                decoded = base64.b64decode(candidate, validate=True)
                return decoded and decoded[0] == 0x30
            except Exception:
                continue
    return False


def _private_key_pem_and_passphrase(token: str, passphrase: str | None = None) -> tuple[str, str | None]:
    """Extract and normalize a private key into an unencrypted PKCS#8 PEM.

    The DuckDB Snowflake ADBC driver only accepts unencrypted PKCS#8 inline
    keys. This function tolerates pasted PEMs with escaped ``\\n``/``\\r\\n``,
    Windows line endings, PKCS#1 ``RSA PRIVATE KEY`` blocks, encrypted keys,
    and base64-only DER blobs. The optional passphrase is used to decrypt the
    key and is not forwarded to the generated SQL.
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

    if not raw.strip():
        raise ValueError("empty snowflake private key")

    # The PEM goes into the CREATE SECRET statement inside `$PK$ … $PK$`
    # dollar-quoting. A key whose text contains that tag would close the literal
    # early and inject the rest of itself as SQL into a statement that carries
    # a credential. A real key never contains `$`.
    if _DOLLAR_TAG in raw:
        raise ValueError("snowflake private key contains the SQL dollar-quote tag; refusing to build a secret")

    text = _normalize_key_text(raw)

    if "-----BEGIN" in text and "-----END" in text:
        pem_text = _reformat_pem_body(text)
        try:
            key = _load_private_key(pem_text.encode("utf-8"), passphrase)
        except ValueError as exc:
            raise ValueError(f"snowflake private key is not a valid PEM/DER key: {exc}") from exc
        return _serialize_unencrypted_pkcs8_pem(key), None

    try:
        der = _try_decode_base64_key(text)
    except ValueError as exc:
        raise ValueError("snowflake private key is not a PEM block or valid base64 key") from exc

    try:
        key = _load_private_key(der, passphrase)
    except ValueError as exc:
        raise ValueError(f"snowflake private key is not a valid PEM/DER key: {exc}") from exc

    return _serialize_unencrypted_pkcs8_pem(key), None


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
