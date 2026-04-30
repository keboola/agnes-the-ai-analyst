"""CLI configuration — token storage, server URL, sync state."""

import json
import os
from pathlib import Path
from typing import Optional


def _config_dir() -> Path:
    d = Path(os.environ.get("DA_CONFIG_DIR", os.path.expanduser("~/.config/da")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_server_url() -> str:
    config = load_config()
    return os.environ.get("DA_SERVER", config.get("server", "http://localhost:8000"))


def get_token() -> Optional[str]:
    token_file = _config_dir() / "token.json"
    if token_file.exists():
        data = json.loads(token_file.read_text())
        return data.get("access_token")
    return os.environ.get("DA_TOKEN")


def save_token(token: str, email: str, role: Optional[str] = None):
    """Persist token + email to ~/.config/da/token.json.

    The ``role`` parameter is accepted for back-compat with older callers
    but is no longer written — authorization derives from group memberships
    server-side, not from a CLI-cached label. Old token.json files with a
    ``role`` field are still readable; the field is simply ignored.
    """
    token_file = _config_dir() / "token.json"
    token_file.write_text(json.dumps({
        "access_token": token,
        "email": email,
    }, indent=2))


def clear_token():
    token_file = _config_dir() / "token.json"
    if token_file.exists():
        token_file.unlink()


def load_config() -> dict:
    config_file = _config_dir() / "config.yaml"
    if config_file.exists():
        import yaml
        return yaml.safe_load(config_file.read_text()) or {}
    return {}


def get_sync_state() -> dict:
    state_file = _config_dir() / "sync_state.json"
    if state_file.exists():
        return json.loads(state_file.read_text())
    return {}


def save_sync_state(state: dict):
    state_file = _config_dir() / "sync_state.json"
    state_file.write_text(json.dumps(state, indent=2))


def save_config(data: dict):
    """Persist server URL and other config to config.yaml."""
    import yaml

    config_file = _config_dir() / "config.yaml"
    existing = {}
    if config_file.exists():
        existing = yaml.safe_load(config_file.read_text()) or {}
    existing.update(data)
    config_file.write_text(yaml.dump(existing, default_flow_style=False))
