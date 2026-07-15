"""Cloud-chat readiness — secret presence + live key validation.

The chat feature depends on server-env secrets:
  - ``ANTHROPIC_API_KEY`` — the agent (and auto-title) call Claude with it.
  - ``E2B_API_KEY``       — the sandbox provider spawns microVMs with it
                            (only when ``provider='e2b'``).
  - ``JWT_SECRET_KEY``    — desktop / WS auth tokens are signed with it.

The startup gates in ``app/main.py`` refuse to build ``ChatManager`` when any
required secret is missing (chat then 503s). This module surfaces that state
to admins **without leaking values** (presence only), and offers live
"does the key actually work" probes — a present-but-invalid key otherwise
only fails at the first sandbox spawn, deep inside a user's chat.
"""

from __future__ import annotations

import os
from typing import Any, Optional

# Env-var names chat reads — the single source of truth for what it needs.
ENV_ANTHROPIC = "ANTHROPIC_API_KEY"
ENV_E2B = "E2B_API_KEY"
ENV_JWT = "JWT_SECRET_KEY"

# Mirror the threshold app/main.py's ``_chat_jwt_secret_ok`` enforces.
_JWT_MIN_BYTES = 32


def _is_set(env_name: str) -> bool:
    return bool(os.environ.get(env_name, "").strip())


def secret_status(chat_config: Any) -> dict:
    """Presence-only readiness snapshot. Never returns secret values.

    ``chat_config`` may be ``None`` (treated as disabled). Each secret entry
    carries ``set`` (is it present/strong-enough) and ``required`` (does the
    current config actually need it), so the UI can show "missing but needed"
    distinctly from "unset and that's fine".
    """
    enabled = bool(getattr(chat_config, "enabled", False))
    provider = getattr(chat_config, "provider", "e2b") or "e2b"
    e2b_needed = enabled and provider == "e2b"

    jwt_val = os.environ.get(ENV_JWT, "")
    jwt_ok = len(jwt_val.encode()) >= _JWT_MIN_BYTES

    secrets = {
        "anthropic_api_key": {"set": _is_set(ENV_ANTHROPIC), "required": enabled},
        "e2b_api_key": {"set": _is_set(ENV_E2B), "required": e2b_needed},
        "jwt_secret_key": {"set": jwt_ok, "required": enabled},
        "e2b_template_id": {
            "set": bool(getattr(chat_config, "e2b_template_id", None)),
            "required": e2b_needed,
        },
    }
    missing = sorted(k for k, v in secrets.items() if v["required"] and not v["set"])
    return {
        "enabled": enabled,
        "provider": provider,
        "secrets": secrets,
        "missing": missing,
        "ready": enabled and not missing,
    }


def _classify(exc: Exception) -> str:
    """Turn an SDK exception into a short admin-facing reason.

    Distinguishes auth failures (wrong key) from everything else (network,
    rate-limit, provider outage) so the admin knows whether to fix the key
    or look at connectivity.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    name = type(exc).__name__
    if status in (401, 403) or "authentication" in name.lower() or "permission" in name.lower():
        return f"authentication failed ({name})"
    msg = str(exc).strip()
    return f"{name}: {msg[:200]}" if msg else name


async def test_e2b_key(api_key: Optional[str] = None, *, timeout: float = 8.0) -> dict:
    """Probe the E2B API key with a cheap authenticated call.

    Uses ``AsyncSandbox.list`` (lists running sandboxes) — it hits the E2B
    API and authenticates without spinning up a microVM. Returns
    ``{ok, detail}``. Falls back to the env key when ``api_key`` is omitted.

    Note: on the modern e2b SDK ``AsyncSandbox.list`` is NOT a coroutine — it
    synchronously returns an ``AsyncSandboxPaginator``; the authenticated round
    trip happens when its first page is awaited. Awaiting ``list`` itself
    raises ``TypeError: object AsyncSandboxPaginator can't be used in 'await'
    expression``, so we await ``next_items()`` instead.
    """
    key = (api_key or os.environ.get(ENV_E2B, "")).strip()
    if not key:
        return {"ok": False, "detail": "E2B_API_KEY not set"}
    try:
        from e2b import AsyncSandbox

        paginator = AsyncSandbox.list(api_key=key, request_timeout=timeout)
        await paginator.next_items()
    except Exception as exc:  # noqa: BLE001 — classify, never raise to the admin
        return {"ok": False, "detail": _classify(exc)}
    return {"ok": True, "detail": "E2B API key valid"}


def _test_anthropic_sync(key: str, timeout: float) -> dict:
    try:
        import anthropic
    except ImportError:
        return {"ok": False, "detail": "anthropic SDK not installed"}
    # Reuse the cheap Haiku model the auto-title path already uses; a
    # 1-token completion is enough to authenticate the key.
    from app.chat.auto_title import _TITLE_MODEL

    try:
        client = anthropic.Anthropic(api_key=key, timeout=timeout)
        client.messages.create(
            model=_TITLE_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as exc:  # noqa: BLE001 — classify, never raise to the admin
        return {"ok": False, "detail": _classify(exc)}
    return {"ok": True, "detail": "Anthropic API key valid"}


async def test_anthropic_key(api_key: Optional[str] = None, *, timeout: float = 8.0) -> dict:
    """Probe the Anthropic API key with a 1-token Haiku completion.

    Returns ``{ok, detail}``. Runs the blocking SDK call on a worker thread
    so it never stalls the event loop. Falls back to the env key when
    ``api_key`` is omitted.
    """
    key = (api_key or os.environ.get(ENV_ANTHROPIC, "")).strip()
    if not key:
        return {"ok": False, "detail": "ANTHROPIC_API_KEY not set"}
    import asyncio

    return await asyncio.to_thread(_test_anthropic_sync, key, timeout)
