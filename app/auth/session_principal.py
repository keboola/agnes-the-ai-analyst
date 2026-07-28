"""SessionPrincipal — the auth subject of a live co-drive session.

A co-session is driven by 2+ humans. Its effective authority is the
*intersection* of all live participants' grants (never any one user's full
set, never the Admin god-mode short-circuit). The resolver builds this from
``chat_session_participants WHERE left_at IS NULL`` on every request; the JWT
carries no participant identity (SR-4), so this object is always live-fresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class SessionPrincipal:
    session_id: str
    participant_user_ids: list[str]
    participant_emails: list[str]
    intersection: dict[str, frozenset[str]]  # resource_type -> allowed resource_ids


@dataclass(frozen=True)
class AgentPrincipal:
    """Auth subject of a live agent-scoped session (V1d).

    Effective authority = the owner's grants ∩ the agent's declared scope.
    Never the owner's full set, never the Admin god-mode short-circuit — an
    agent is a *restriction* of its owner, never an elevation. Like
    ``SessionPrincipal`` the intersection is rebuilt live per request (the
    token bakes in no grants), so revoking a grant or narrowing the agent
    takes effect on the next request with no stale-replay window.
    """

    session_id: str
    agent_id: str
    owner_user_id: str
    owner_email: str
    intersection: dict[str, frozenset[str]]


#: Either restricted principal. Consumers that mean "not a full user dict —
#: use the intersection, deny admin" should branch on this union, not on one
#: member, so a new principal kind cannot silently bypass a seam.
Principal = Union[SessionPrincipal, AgentPrincipal]
