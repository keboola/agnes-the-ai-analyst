"""Chat feature config (loaded from instance.yaml `chat:` block)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ChatConfig:
    enabled: bool = False
    require_isolation: bool = True
    concurrency_per_user: int = 3
    idle_ttl_seconds: int = 30 * 60
    per_tool_call_seconds: int = 90
    per_session_bq_scan_bytes: int = 20 * 1024**3
    daily_anthropic_spend_usd: float = 20.0
    max_session_seconds: int = 4 * 3600
    max_session_tokens: int = 200_000
    rate_messages_per_hour: int = 100
    tool_calls_per_turn_budget: int = 50
    marketplace_sha_debounce_seconds: int = 5 * 60


def load_chat_config(instance_yaml: Path) -> ChatConfig:
    if not instance_yaml.exists():
        return ChatConfig()
    data = yaml.safe_load(instance_yaml.read_text()) or {}
    raw = data.get("chat", {}) or {}
    return ChatConfig(
        enabled=bool(raw.get("enabled", False)),
        require_isolation=bool(raw.get("require_isolation", True)),
        concurrency_per_user=int(raw.get("concurrency_per_user", 3)),
        idle_ttl_seconds=int(raw.get("idle_ttl_seconds", 30 * 60)),
        per_tool_call_seconds=int(raw.get("per_tool_call_seconds", 90)),
        per_session_bq_scan_bytes=int(raw.get("per_session_bq_scan_bytes", 20 * 1024**3)),
        daily_anthropic_spend_usd=float(raw.get("daily_anthropic_spend_usd", 20.0)),
        max_session_seconds=int(raw.get("max_session_seconds", 4 * 3600)),
        max_session_tokens=int(raw.get("max_session_tokens", 200_000)),
        rate_messages_per_hour=int(raw.get("rate_messages_per_hour", 100)),
        tool_calls_per_turn_budget=int(raw.get("tool_calls_per_turn_budget", 50)),
        marketplace_sha_debounce_seconds=int(raw.get("marketplace_sha_debounce_seconds", 5 * 60)),
    )
