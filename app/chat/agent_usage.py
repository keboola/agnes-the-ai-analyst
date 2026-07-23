"""Per-session token usage + agent-config fingerprint helpers shared by the
agent-as-API surface (Task 9: `app/api/agent_runtime.py`) and its
background job worker (`app/worker/kinds.py::_run_agent_response`).

Factored out of both call sites so neither the API router nor the worker
module needs to import the other — the worker layer stays independent of
FastAPI routing, and the API layer stays independent of the job registry.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from app.chat.agent_profile import compute_effective_scope

_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")

# Public-facing usage dict keys (short form) -> the `llm_usage` row column
# each sums from. Kept distinct from the DB column names so the API
# response shape doesn't leak internal schema naming.
_USAGE_KEY_MAP = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_creation": "cache_creation_tokens",
}

# Cap on how many recent `llm_usage` rows to scan per agent when summing a
# single session's usage — generous for a one-shot turn (which writes at
# most a handful of rows), while still bounded rather than unbounded.
_USAGE_SCAN_LIMIT = 1000


def usage_for_session(agent_id: str, session_id: str) -> Dict[str, int]:
    """Sum this session's `llm_usage` rows into the API's short-form usage
    shape: ``{"input", "output", "cache_read", "cache_creation", "total"}``.

    Callers should flush ``app.api.broker_agent_policy.usage_accumulator``
    first (best-effort — the accumulator batches writes, so a just-finished
    turn's rows may still be sitting in memory otherwise). Not done here so
    this module has no import-time dependency on the broker.
    """
    from src.repositories import llm_usage_repo

    rows = llm_usage_repo().list_for_agent(agent_id, limit=_USAGE_SCAN_LIMIT)
    totals = {k: 0 for k in _USAGE_KEY_MAP}
    for row in rows:
        if row.get("session_id") != session_id:
            continue
        for short_key, column in _USAGE_KEY_MAP.items():
            totals[short_key] += int(row.get(column) or 0)
    totals["total"] = sum(totals.values())
    return totals


def agent_config_hash(agent: Dict[str, Any]) -> str:
    """First 16 hex chars of the sha256 of a canonical JSON encoding of
    the agent's effective scope + the config fields that shape its
    behavior (system prompt, model, slug).

    Deterministic per agent config: two calls against an unchanged agent
    row produce the same hash; any scope-mode or field change produces a
    different one — cheap signal for callers to detect "did this agent's
    effective configuration change since I last called it".
    """
    from src.repositories import agents_repo

    scope_items = agents_repo().get_scope(agent["id"])
    effective_scope = compute_effective_scope(agent, scope_items)
    material = {
        "scope": effective_scope,
        "system_prompt": agent.get("system_prompt") or "",
        "model": agent.get("model") or "",
        "slug": agent.get("slug") or "",
    }
    canonical = json.dumps(material, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
