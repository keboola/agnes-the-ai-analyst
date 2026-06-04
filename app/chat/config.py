"""Chat feature config (loaded from instance.yaml `chat:` block)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlackConfig:
    # "http" (default, Events API webhook) | "socket" (Socket Mode WS).
    # Unknown values are normalized to "http" at parse time with a warning.
    # Tokens (SLACK_BOT_TOKEN / SLACK_APP_TOKEN / SLACK_SIGNING_SECRET) are
    # deliberately NOT stored here — resolved at use site via slack_secret
    # (env > vault) so they never leak into a frozen-config echo (e.g.
    # /admin/server-config).
    transport: str = "http"


@dataclass(frozen=True)
class ChatConfig:
    enabled: bool = False
    # Sandbox provider id. ``e2b`` is the only production-supported
    # value; future variants (mock_e2b for tests, sandbox-as-a-service
    # alternatives) would extend the gate in ``app/main.py``.
    provider: str = "e2b"
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
    # E2B template id (``agnes-chat`` for the default operator build per
    # Q2 — single mutable ``:latest`` tag). Required when
    # ``chat.enabled=true`` and ``provider=e2b``; startup gate refuses
    # otherwise. Operator obtains this from ``e2b template build``.
    e2b_template_id: Optional[str] = None
    # Per-spawn workspace push cap (Q1, 100 MB default). Files past this
    # cap → WorkspaceTooLarge → user-facing error frame.
    e2b_workspace_max_bytes: int = 100 * 1024 * 1024
    # Q3 (additional gate alongside idle_ttl_seconds): kill the sandbox
    # the moment the WS disconnects rather than letting the idle reaper
    # close it later. Cuts billable sandbox-minutes on UI close.
    e2b_kill_on_ws_disconnect: bool = True
    # When true, the runner bootstraps the user's RBAC-filtered marketplace
    # plugins into each sandbox at spawn (clone + `claude plugin install` +
    # load via setting_sources) so the agent can use marketplace skills.
    # Off by default: it adds ~10-15 s of per-spawn latency, only worthwhile
    # once the operator's marketplace actually ships skill/agent content
    # (an empty placeholder plugin contributes nothing). Independent of the
    # always-on plugin.json sanitization in the marketplace packager.
    bootstrap_marketplace: bool = False
    slack: "SlackConfig" = field(default_factory=SlackConfig)


def _parse_slack_config(raw_chat: dict) -> SlackConfig:
    _s = raw_chat.get("slack")
    raw_slack = _s if isinstance(_s, dict) else {}
    raw_value = raw_slack.get("transport", "http")
    transport = str(raw_value).strip().lower() if raw_value is not None else ""
    if transport not in ("http", "socket"):
        logger.warning(
            "unknown slack transport %r in chat.slack.transport — "
            "falling back to 'http'", transport,
        )
        transport = "http"
    return SlackConfig(transport=transport)


def load_chat_config(instance_yaml: Path) -> ChatConfig:
    if not instance_yaml.exists():
        return ChatConfig()
    data = yaml.safe_load(instance_yaml.read_text()) or {}
    raw = data.get("chat", {}) or {}
    return ChatConfig(
        enabled=bool(raw.get("enabled", False)),
        provider=str(raw.get("provider", "e2b")),
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
        e2b_template_id=raw.get("e2b_template_id") or None,
        e2b_workspace_max_bytes=int(raw.get("e2b_workspace_max_bytes", 100 * 1024 * 1024)),
        e2b_kill_on_ws_disconnect=bool(raw.get("e2b_kill_on_ws_disconnect", True)),
        bootstrap_marketplace=bool(raw.get("bootstrap_marketplace", False)),
        slack=_parse_slack_config(raw),
    )
