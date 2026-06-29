"""CLI configuration — token storage, server URL, sync state."""

import json
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Optional


# In-process override for `get_token()`. Used by `agnes init --token X` and
# `agnes auth import-token` to force a specific token for the duration of a
# scoped block, EVEN WHEN `~/.config/agnes/token.json` already holds a
# different (possibly stale) token. Without this override, `get_token()`
# reads the on-disk token first and the explicit `--token` argument is
# silently ignored — the bug Devin Review caught at cli/commands/init.py:99.
#
# A ContextVar is used (not a plain global) so concurrent callers — async
# tasks, threads — each see their own override, and a leaked override in
# one task can't corrupt another. `_token_override.set(...)` returns a
# token used to reset; the `_with_token_override` context manager scopes it.
_token_override: ContextVar[Optional[str]] = ContextVar(
    "agnes_cli_token_override", default=None,
)


@contextmanager
def _with_token_override(token: Optional[str]) -> Iterator[None]:
    """Set `_token_override` for the duration of the block.

    `get_token()` checks the override BEFORE reading `token.json`, so any
    in-block call returns the supplied token regardless of on-disk state.
    Restores the prior override (if any) on exit so nested overrides nest
    correctly.
    """
    if not token:
        yield
        return
    reset_token = _token_override.set(token)
    try:
        yield
    finally:
        _token_override.reset(reset_token)


def _config_dir() -> Path:
    d = Path(os.environ.get("AGNES_CONFIG_DIR", os.path.expanduser("~/.config/agnes")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_server_url() -> str:
    config = load_config()
    return os.environ.get("AGNES_SERVER", config.get("server", "http://localhost:8000"))


def get_token() -> Optional[str]:
    # In-process override wins over BOTH the on-disk file and the env var.
    # Set by `_with_token_override(...)`; used by `agnes init --token X`
    # to force the explicit arg through the verify call even when a stale
    # `~/.config/agnes/token.json` exists.
    if (override := _token_override.get()) is not None:
        return override
    # Chat-sandbox context: AGNES_SESSION_ID is set only by the chat
    # runner's spawn env, alongside a FRESHLY-minted short-lived session
    # JWT in AGNES_TOKEN (ChatManager._spawn_runner re-mints on every
    # spawn/respawn). Any token.json found here — e.g. written by an
    # in-session `agnes init`, or replayed workspace state — is by
    # definition staler than the env credential and must not shadow it,
    # or a respawned runner 401s with the previous spawn's expired token.
    # Empty/unset env (e.g. the AGNES_SESSION_JWT_SEED fallback minted
    # "") still falls through to the file rather than returning a blank
    # credential. Analyst laptops (no AGNES_SESSION_ID) keep the
    # historical file-over-env order — token.json written by `agnes
    # init` stays canonical there.
    if os.environ.get("AGNES_SESSION_ID"):
        if env_token := os.environ.get("AGNES_TOKEN"):
            return env_token
    token_file = _config_dir() / "token.json"
    if token_file.exists():
        data = json.loads(token_file.read_text(encoding="utf-8"))
        return data.get("access_token")
    return os.environ.get("AGNES_TOKEN")


def save_token(token: str, email: str, role: Optional[str] = None):
    """Persist token + email to ~/.config/agnes/token.json.

    The ``role`` parameter is accepted for back-compat with older callers
    but is no longer written — authorization derives from group memberships
    server-side, not from a CLI-cached label. Old token.json files with a
    ``role`` field are still readable; the field is simply ignored.

    The file holds a plaintext bearer token, so it is written with mode
    ``0o600`` (owner read/write only) rather than left at the ambient umask
    (commonly ``0o644`` — world-readable). The chmod happens on a temp file
    in the same dir *before* the atomic rename, so a reader never observes a
    world-readable transient state. ``os.fchmod`` is best-effort and
    Windows-safe: on filesystems / platforms that don't honor it the token
    still lands and native ACLs apply (mirrors
    ``cli/lib/initial_workspace.py``).
    """
    body = json.dumps({
        "access_token": token,
        "email": email,
    }, indent=2)
    config_dir = _config_dir()
    token_file = config_dir / "token.json"
    fd, tmp_str = tempfile.mkstemp(prefix=".token.", dir=str(config_dir))
    tmp_path = Path(tmp_str)
    try:
        try:
            os.write(fd, body.encode("utf-8"))
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, NotImplementedError, OSError):
                # Windows / filesystem that doesn't honor fchmod — native
                # ACLs apply, token contents still land.
                pass
        finally:
            os.close(fd)
        os.replace(tmp_path, token_file)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def clear_token():
    token_file = _config_dir() / "token.json"
    if token_file.exists():
        token_file.unlink()


def load_config() -> dict:
    config_file = _config_dir() / "config.yaml"
    if config_file.exists():
        import yaml
        return yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    return {}


def get_sync_state() -> dict:
    state_file = _config_dir() / "sync_state.json"
    if state_file.exists():
        return json.loads(state_file.read_text(encoding="utf-8"))
    return {}


def save_sync_state(state: dict):
    state_file = _config_dir() / "sync_state.json"
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def save_config(data: dict):
    """Persist server URL and other config to config.yaml."""
    import yaml

    config_file = _config_dir() / "config.yaml"
    existing = {}
    if config_file.exists():
        existing = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    existing.update(data)
    config_file.write_text(yaml.dump(existing, default_flow_style=False), encoding="utf-8")


def get_workspace_root() -> Optional[str]:
    """Absolute path of the analyst workspace anchored at `agnes init`.

    This is the single source of truth for where Claude Code session
    transcripts live: `agnes push` encodes it to the projects-dir folder
    name and scans that folder. Written by `agnes init` and back-filled by
    `agnes self-upgrade` for clients installed before the config key existed.
    Returns ``None`` when unset (push then uploads nothing).
    """
    config = load_config()
    val = config.get("workspace_root")
    return val or None


def set_workspace_root(path: str) -> None:
    """Persist the workspace root to config.yaml (merges, doesn't clobber)."""
    save_config({"workspace_root": str(path)})
