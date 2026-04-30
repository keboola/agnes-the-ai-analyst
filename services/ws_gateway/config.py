"""Configuration for WebSocket Gateway."""

import os


def _get_required_env(key: str) -> str:
    """Get a required environment variable or fail fast."""
    value = os.environ.get(key)
    if not value:
        raise ValueError(f"Missing required environment variable: {key}")
    return value


WS_GATEWAY_HOST: str = os.environ.get("WS_GATEWAY_HOST", "127.0.0.1")
WS_GATEWAY_PORT: int = int(os.environ.get("WS_GATEWAY_PORT", "8765"))
WS_DISPATCH_SOCKET: str = os.environ.get("WS_DISPATCH_SOCKET", "/run/ws-gateway/ws.sock")
DESKTOP_JWT_SECRET: str = _get_required_env("DESKTOP_JWT_SECRET")
HEARTBEAT_INTERVAL_SECONDS: int = int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "30"))
HEARTBEAT_TIMEOUT_MISSED: int = int(os.environ.get("HEARTBEAT_TIMEOUT_MISSED", "3"))
MAX_CONNECTIONS_PER_USER: int = int(os.environ.get("MAX_CONNECTIONS_PER_USER", "5"))
