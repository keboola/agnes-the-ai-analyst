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
    # Hosts/CIDRs the sandbox may reach outbound, enforced at the E2B VM
    # level via SandboxNetworkOpts.allow_out. Empty = provider computes a
    # default (broker host + loopback). NEVER include api.anthropic.com
    # directly once the broker is live — Anthropic traffic goes via the relay.
    egress_allow_out: list[str] = field(default_factory=list)
    # Per-spawn workspace push cap (Q1, 100 MB default). Files past this
    # cap → WorkspaceTooLarge → user-facing error frame.
    e2b_workspace_max_bytes: int = 100 * 1024 * 1024
    # Lifecycle when the last sink detaches: "pause" (E2B snapshot, resumable)
    # or "kill" (legacy cost-minimizing behavior).
    # Deprecated: use on_detach instead of e2b_kill_on_ws_disconnect.
    on_detach: str = "pause"
    detach_linger_seconds: int = 60
    # Grace window (Tier 1, restart-invariant reuse — see
    # docs/brainstorms/2026-07-23-chat-e2b-architecture-comparison.md §5) a
    # detached-but-still-live sandbox is kept warm before ``_linger_then_pause``
    # actually pauses it: a follow-up message inside this window reuses the
    # already-live sandbox instead of paying a fresh cold spawn. This is the
    # canonical knob ``ChatManager`` reads for that sleep; ``detach_linger_seconds``
    # is kept as a back-compat alias — ``load_chat_config`` defaults
    # ``idle_grace_seconds`` to whatever ``detach_linger_seconds`` resolves to
    # when the operator's instance.yaml doesn't set ``idle_grace_seconds``
    # explicitly, so existing configs need no changes.
    idle_grace_seconds: int = 60
    paused_ttl_seconds: int = 7 * 24 * 3600
    # Back-compat echo only — new code reads on_detach, never this field.
    e2b_kill_on_ws_disconnect: bool = True
    # When true, the runner bootstraps the user's RBAC-filtered marketplace
    # plugins into each sandbox at spawn (clone + `claude plugin install` +
    # load via setting_sources) so the agent can use marketplace skills.
    # Off by default: it adds ~10-15 s of per-spawn latency, only worthwhile
    # once the operator's marketplace actually ships skill/agent content
    # (an empty placeholder plugin contributes nothing). Independent of the
    # always-on plugin.json sanitization in the marketplace packager.
    bootstrap_marketplace: bool = False
    # How the chat broker authenticates to Anthropic. ``api_key`` (default) uses
    # the static ``ANTHROPIC_API_KEY``. ``workload_identity`` mints a short-lived
    # token from the workload's own OIDC identity via Anthropic Workload Identity
    # Federation (``app/auth/wif.py``) — no long-lived key anywhere. Opt-in; the
    # federation env (ANTHROPIC_FEDERATION_RULE_ID / ORGANIZATION_ID /
    # SERVICE_ACCOUNT_ID / IDENTITY_TOKEN[_FILE]) must be configured for it. Any
    # value other than ``workload_identity`` falls back to ``api_key``.
    llm_auth: str = "api_key"
    # Agent-as-API broker policy (Task 8, agent-profiles V1a): models a
    # brokered chat-completion request may use besides the calling agent's
    # own pinned model — e.g. a cheap utility model every agent is allowed
    # to fall back to regardless of its persona's primary model. An agent
    # with no pinned model has NO model policy at all (every model is
    # allowed; utility_models is irrelevant for it). Empty by default (no
    # extra models allowed). Enforced in
    # ``app/api/broker_agent_policy.py::check_model``.
    agent_api_utility_models: list[str] = field(default_factory=list)
    # TTL (seconds) for the cached agent-monthly-token-total the broker
    # checks on every brokered call before enforcing ``token_budget_monthly``
    # (``app/api/broker_agent_policy.py::cached_month_total``). Trades
    # budget-enforcement freshness for avoiding a ledger table read on every
    # LLM call.
    agent_api_budget_cache_ttl_s: int = 60
    # Per-session artifact harvest caps (V1b Task 5,
    # ``app.chat.artifact_harvest``): ``agent_api_artifact_max_bytes`` is a
    # CUMULATIVE cap on the total bytes harvested for a session across all
    # its files (not a per-file limit) — a file is skipped (logged, scan
    # continues) once harvesting it would push the running total over the
    # cap. The scan stops entirely after ``agent_api_artifact_max_files``
    # files harvested. Both are best-effort guardrails against a run that
    # fills the outputs dir with an unbounded number/size of files, not a
    # retention policy — retention (GC of already-harvested rows/blobs) is
    # a later task.
    agent_api_artifact_max_bytes: int = 25 * 1024 * 1024
    agent_api_artifact_max_files: int = 20
    # Outbound webhook delivery (V1b Task 6, `app.chat.webhook_delivery`):
    # consecutive delivery failures after which a webhook is auto-disabled
    # (`agent_webhooks_repo().disable`) rather than retried forever against
    # a permanently-broken (or no-longer-owned) endpoint.
    agent_api_webhook_max_failures: int = 5
    # Agent memory notebook write endpoint (V1c Task 4, `app.api.agent_memory`):
    # per-write content size cap, rolling hourly write-rate cap (both
    # regardless of `memory_write_mode`), and a total-pending backlog cap
    # (C3) — independent of the hourly rate — that a `propose`-mode agent
    # writing steadily just under the rate limit would otherwise blow past
    # forever, since nothing else shrinks the pending set except the
    # owner's own review/approve action.
    agent_memory_max_chars: int = 2000
    agent_memory_writes_per_hour: int = 20
    agent_memory_max_pending: int = 100
    # Age threshold (days) past which a `pending` memory row is considered
    # stale for a future reaper job. INERT — nothing reads this field today
    # (NOT YET enforced anywhere); see `app.api.agent_memory.remember`'s
    # docstring for why reaping is deferred rather than wired into
    # `count_pending` today. Landed now so that reaper has a config knob to
    # read once it exists — an operator setting this expecting it to bound
    # the pending cap will be silently misled until the reaper lands.
    agent_memory_pending_ttl_days: int = 30
    slack: "SlackConfig" = field(default_factory=SlackConfig)


def _parse_slack_config(raw_chat: dict) -> SlackConfig:
    _s = raw_chat.get("slack")
    raw_slack = _s if isinstance(_s, dict) else {}
    raw_value = raw_slack.get("transport", "http")
    transport = str(raw_value).strip().lower() if raw_value is not None else ""
    if transport not in ("http", "socket"):
        logger.warning(
            "unknown slack transport %r in chat.slack.transport — falling back to 'http'",
            transport,
        )
        transport = "http"
    return SlackConfig(transport=transport)


def _parse_on_detach(raw: dict) -> str:
    on_detach = str(raw.get("on_detach", "")).strip().lower()
    if on_detach not in ("pause", "kill"):
        if on_detach:
            logger.warning("unknown chat.on_detach %r — falling back to 'pause'", on_detach)
        if "e2b_kill_on_ws_disconnect" in raw and bool(raw["e2b_kill_on_ws_disconnect"]):
            logger.warning("chat.e2b_kill_on_ws_disconnect is deprecated; use chat.on_detach: kill")
            on_detach = "kill"
        else:
            on_detach = "pause"
    return on_detach


def load_chat_config(instance_yaml: Path) -> ChatConfig:
    if not instance_yaml.exists():
        return ChatConfig()
    data = yaml.safe_load(instance_yaml.read_text()) or {}
    raw = data.get("chat", {}) or {}
    detach_linger_seconds = int(raw.get("detach_linger_seconds", 60))
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
        egress_allow_out=list(raw.get("egress_allow_out") or []),
        e2b_workspace_max_bytes=int(raw.get("e2b_workspace_max_bytes", 100 * 1024 * 1024)),
        on_detach=_parse_on_detach(raw),
        detach_linger_seconds=detach_linger_seconds,
        # Falls back to detach_linger_seconds's own resolved value when the
        # operator's instance.yaml doesn't set idle_grace_seconds explicitly
        # — see ChatConfig.idle_grace_seconds's docstring.
        idle_grace_seconds=int(raw.get("idle_grace_seconds", detach_linger_seconds)),
        paused_ttl_seconds=int(raw.get("paused_ttl_seconds", 7 * 24 * 3600)),
        e2b_kill_on_ws_disconnect=bool(raw.get("e2b_kill_on_ws_disconnect", True)),
        bootstrap_marketplace=bool(raw.get("bootstrap_marketplace", False)),
        llm_auth=str((raw.get("llm") or {}).get("auth", "api_key")).strip().lower() or "api_key",
        agent_api_utility_models=list(raw.get("agent_api_utility_models") or []),
        agent_api_budget_cache_ttl_s=int(raw.get("agent_api_budget_cache_ttl_s", 60)),
        agent_api_artifact_max_bytes=int(raw.get("agent_api_artifact_max_bytes", 25 * 1024 * 1024)),
        agent_api_artifact_max_files=int(raw.get("agent_api_artifact_max_files", 20)),
        agent_api_webhook_max_failures=int(raw.get("agent_api_webhook_max_failures", 5)),
        agent_memory_max_chars=int(raw.get("agent_memory_max_chars", 2000)),
        agent_memory_writes_per_hour=int(raw.get("agent_memory_writes_per_hour", 20)),
        agent_memory_max_pending=int(raw.get("agent_memory_max_pending", 100)),
        # Inert (see the field's own comment above) — parsed and stored for
        # forward-compat with the not-yet-built reaper, not read anywhere yet.
        agent_memory_pending_ttl_days=int(raw.get("agent_memory_pending_ttl_days", 30)),
        slack=_parse_slack_config(raw),
    )
