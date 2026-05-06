"""Auto-generate and persist secrets that survive container restarts."""
import logging
import os
import secrets
from pathlib import Path

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
