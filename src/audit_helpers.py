"""Shared helpers for audit logging."""

from app.auth.scheduler_token import SCHEDULER_USER_EMAIL


def client_kind_from_user(user) -> str:
    """Detect CLI vs web vs scheduler from the auth state.

    Order of precedence:
    1. scheduler user → 'scheduler'
    2. PAT-authenticated (token_type='pat' set by get_current_user) → 'cli'
    3. anything else → 'web'

    ``user`` is a plain dict for almost every caller, but a restricted
    principal (``SessionPrincipal`` / ``AgentPrincipal`` — co-session or
    agent-session, V1d) is a frozen dataclass with no ``.get``. Neither kind
    of token is ever PAT- or scheduler-authenticated, so "web" is the
    correct answer without needing to know which principal shape it is —
    this must stay a supertype-agnostic ``not isinstance(user, dict)`` check
    (not an import of ``PRINCIPAL_TYPES``) so a future principal kind can't
    reintroduce this crash by omission.
    """
    if user is None or not isinstance(user, dict):
        return "web"
    if user.get("email") == SCHEDULER_USER_EMAIL:
        return "scheduler"
    if user.get("token_type") == "pat":
        return "cli"
    return "web"
