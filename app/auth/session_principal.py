"""SessionPrincipal — the auth subject of a live co-drive session.

A co-session is driven by 2+ humans. Its effective authority is the
*intersection* of all live participants' grants (never any one user's full
set, never the Admin god-mode short-circuit). The resolver builds this from
``chat_session_participants WHERE left_at IS NULL`` on every request; the JWT
carries no participant identity (SR-4), so this object is always live-fresh.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionPrincipal:
    session_id: str
    participant_user_ids: list[str]
    participant_emails: list[str]
    intersection: dict[str, frozenset[str]]  # resource_type -> allowed resource_ids
