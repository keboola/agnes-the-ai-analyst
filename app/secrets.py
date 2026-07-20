"""Auto-generate and persist secrets that survive container restarts."""
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _state_dir() -> Path:
    """Return path to writable state directory.

    STATE_DIR env var takes precedence; otherwise defaults to
    ${DATA_DIR}/state for backward compatibility with deployments
    that nest state under the data disk. See docs/state-dir.md.
    """
    state = os.environ.get("STATE_DIR", "")
    if state:
        return Path(state)
    return Path(os.environ.get("DATA_DIR", "./data")) / "state"


# Module-level lock guarding read-modify-write of `.env_overlay`. Without it,
# two admins clicking "Save" on /admin/marketplaces (or /admin/server-config
# Initial Workspace section) in the same second can race on the same file:
# both read [X, Y], one writes [X, Y, A], the other writes [X, Y, B] and
# silently clobbers A. The lock is process-local; we rely on the app being
# the sole writer to `${STATE_DIR}/.env_overlay` (no out-of-process tools
# touch it).
_overlay_lock = threading.Lock()


def persist_overlay_token(env_name: str, value: Optional[str]) -> None:
    """Atomically update a key in ``${STATE_DIR}/.env_overlay`` and ``os.environ``.

    Single shared helper for every code path that writes a secret to the
    overlay file (today: marketplaces PATs + initial-workspace template
    PAT). The whole read-merge-write is serialized by ``_overlay_lock``.

    ``value=None`` or ``value=""`` removes the key from the overlay and the
    process env. A non-empty value writes/replaces the key.

    Path resolution matches ``app/main.py``'s startup-time read; without
    this alignment, PATs persisted under the flat-mount layout
    (``STATE_DIR=/data-state``) would land at ``/data/state/.env_overlay``
    while the app reads from ``/data-state/.env_overlay``, silently
    dropping the token on the next restart.
    """
    overlay_path = _state_dir() / ".env_overlay"

    with _overlay_lock:
        overlay_path.parent.mkdir(parents=True, exist_ok=True)

        existing: dict[str, str] = {}
        if overlay_path.exists():
            for line in overlay_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()

        if value:
            existing[env_name] = value
            os.environ[env_name] = value
        else:
            existing.pop(env_name, None)
            os.environ.pop(env_name, None)

        overlay_path.write_text(
            "\n".join(f"{k}={v}" for k, v in existing.items())
            + ("\n" if existing else "")
        )
        try:
            overlay_path.chmod(0o600)
        except OSError:
            pass


def _load_or_generate(env_var: str, file_name: str) -> str:
    """Load secret from env var, or from file, or generate and persist."""
    val = os.environ.get(env_var, "")
    if val:
        return val
    secret_path = _state_dir() / file_name
    if secret_path.exists():
        val = secret_path.read_text().strip()
        if val:
            return val
        logger.warning("Secret file %s is empty, regenerating", secret_path)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    val = secrets.token_hex(32)
    secret_path.write_text(val)
    try:
        secret_path.chmod(0o600)
    except OSError:
        pass  # chmod not supported on all platforms (e.g., Windows)
    logger.info(
        "Auto-generated %s -> %s (set %s in .env to use a fixed value)",
        file_name, secret_path, env_var,
    )
    return val


def get_jwt_secret() -> str:
    """Get JWT secret key from env, file, or auto-generate."""
    return _load_or_generate("JWT_SECRET_KEY", ".jwt_secret")


def get_session_secret() -> str:
    """Get session secret from env, file, or auto-generate."""
    return _load_or_generate("SESSION_SECRET", ".session_secret")


# --- OAuth client_secret encryption at rest (#869) -------------------------
# The MCP OAuth SDK authenticates clients by comparing the presented secret
# against the value our provider's get_client() returns, so a one-way hash
# can't be verified inside the SDK's client-auth path. Instead we encrypt the
# client_secret at rest (reversible with an app-held key) so a DB/backup leak
# no longer exposes usable client secrets in plaintext; get_client decrypts to
# the raw value the SDK still compares by equality.
_CLIENT_SECRET_ENC_PREFIX = "enc:v1:"
_fernet_lock = threading.Lock()
_fernet_cached = None


def _client_secret_fernet():
    """Lazily build the Fernet used for client_secret encryption.

    The key is derived (sha256) from an app-managed secret so any key string —
    the auto-generated hex or an operator's ``AGNES_OAUTH_ENC_KEY`` override —
    yields a valid 32-byte Fernet key. Cached; the derivation runs once.
    """
    global _fernet_cached
    if _fernet_cached is not None:
        return _fernet_cached
    with _fernet_lock:
        if _fernet_cached is None:
            import base64
            import hashlib
            from cryptography.fernet import Fernet

            key_material = _load_or_generate("AGNES_OAUTH_ENC_KEY", ".oauth_enc_key")
            fernet_key = base64.urlsafe_b64encode(hashlib.sha256(key_material.encode()).digest())
            _fernet_cached = Fernet(fernet_key)
    return _fernet_cached


def encrypt_client_secret(raw: Optional[str]) -> Optional[str]:
    """Encrypt an OAuth client_secret for storage. Idempotent and total:
    returns ``None``/empty unchanged, and returns an already-encrypted value
    (``enc:v1:`` prefix) untouched so re-persisting a decrypted-then-stored
    row never double-encrypts."""
    if not raw or raw.startswith(_CLIENT_SECRET_ENC_PREFIX):
        return raw
    token = _client_secret_fernet().encrypt(raw.encode()).decode()
    return _CLIENT_SECRET_ENC_PREFIX + token


def decrypt_client_secret(stored: Optional[str]) -> Optional[str]:
    """Decrypt a stored client_secret back to the raw value.

    Legacy rows written before encryption (no ``enc:v1:`` prefix) are returned
    verbatim, so existing clients keep working until their next re-registration
    re-encrypts them. If decryption fails (key rotated/corrupt), we fail
    **closed** — return an unmatchable sentinel so the SDK's constant-time
    secret comparison rejects the client, rather than returning ``None`` (which
    the SDK treats as "no secret required" and would let the client in)."""
    if not stored or not stored.startswith(_CLIENT_SECRET_ENC_PREFIX):
        return stored
    token = stored[len(_CLIENT_SECRET_ENC_PREFIX):]
    try:
        return _client_secret_fernet().decrypt(token.encode()).decode()
    except Exception:
        logger.error(
            "OAuth client_secret decryption failed (key rotated or ciphertext "
            "corrupt); failing client authentication closed"
        )
        return "\x00invalid-decrypt-" + secrets.token_hex(16)
