"""Auto-title generation for chat sessions.

After the first assistant turn lands, the manager calls
:func:`generate_title` with the first user message. We ask Haiku 4.5 for
a 2–5 word title and write it back to ``chat_sessions.title``. The
result is broadcast as a ``session_renamed`` frame so the sidebar +
thread header update live.

Design notes
------------
- **Best-effort, never blocking.** Any failure (no key, network error,
  rate limit, refusal, weird response) returns ``None``. The session
  keeps its ``Untitled chat`` fallback — chats never break because
  Haiku is down.
- **Sync SDK in a thread.** The Anthropic SDK call is synchronous; we
  run it via ``asyncio.to_thread`` from the manager so the WS pump
  isn't blocked while Haiku is thinking. Mirrors the existing pattern
  in ``connectors/llm/anthropic_provider.py``.
- **Tight cap on input + output.** First user message is clipped to
  the first ~600 chars (enough signal for a title, keeps token cost
  per call <500 input + ~16 output). Result is stripped, quote-trimmed,
  and capped at 60 chars before being persisted.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Haiku is the right tier for this — fast, cheap, and the task is
# trivial. The model id matches the one used by other Haiku call-sites
# in the codebase (Corporate Memory batch extraction).
_TITLE_MODEL = "claude-haiku-4-5-20251001"
_TITLE_MAX_TOKENS = 24
_MESSAGE_CLIP_CHARS = 600
_TITLE_MAX_CHARS = 60

_SYSTEM_PROMPT = (
    "You produce a concise title (2–6 words, sentence case, no trailing "
    "punctuation, no quotes) summarizing the topic of a chat conversation "
    "given its first user message. Reply with the title only — no preamble, "
    "no explanation."
)


def _strip_title(raw: str) -> Optional[str]:
    """Normalize Haiku's reply into a stored title.

    Trims whitespace, strips wrapping quotes / brackets, drops trailing
    punctuation, and caps length. Returns ``None`` for empty or
    pathological replies so the caller can keep the default.
    """
    if not raw:
        return None
    text = raw.strip()
    # Drop wrapping quotes/brackets the model sometimes adds despite the
    # system prompt asking it not to.
    for opener, closer in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("[", "]"), ("(", ")")):
        if text.startswith(opener) and text.endswith(closer) and len(text) >= 2:
            text = text[1:-1].strip()
    # Trailing punctuation looks awkward in a sidebar item.
    text = text.rstrip(".!?,:;")
    text = text.strip()
    if not text:
        return None
    # Collapse internal whitespace runs (newlines, tabs) to single spaces.
    text = " ".join(text.split())
    if len(text) > _TITLE_MAX_CHARS:
        text = text[: _TITLE_MAX_CHARS - 1].rstrip() + "…"
    return text


def _generate_title_sync(user_message: str, *, api_key: str) -> Optional[str]:
    """Synchronous Haiku call. Returns the trimmed title or ``None``.

    Lives in its own function so :func:`generate_title` can dispatch it
    onto a worker thread without dragging anthropic SDK init into the
    event loop on every call.
    """
    try:
        import anthropic  # local import keeps test envs without the SDK clean
    except ImportError:  # pragma: no cover - SDK is a hard dep for chat
        logger.debug("anthropic SDK missing; skipping auto-title")
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=8.0)
        resp = client.messages.create(
            model=_TITLE_MODEL,
            max_tokens=_TITLE_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message[:_MESSAGE_CLIP_CHARS]}],
        )
    except Exception:
        logger.exception("auto-title Haiku call failed; keeping default title")
        return None
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return _strip_title("".join(parts))


async def generate_title(user_message: str) -> Optional[str]:
    """Ask Haiku for a short title for a conversation. Best-effort.

    Returns the cleaned title string, or ``None`` if the API key is
    missing, the SDK isn't installed, the call fails, or the reply is
    empty/garbage. The caller MUST treat ``None`` as "leave the title
    alone" — never as an error.
    """
    if not user_message or not user_message.strip():
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY unset; skipping auto-title")
        return None
    import asyncio
    return await asyncio.to_thread(_generate_title_sync, user_message, api_key=api_key)
