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


def identity_for_audit(user) -> tuple:
    """``(user_id, email)`` for audit-log rows and quota-key bookkeeping
    only — NEVER for an authorization decision (a narrowed principal must
    not inherit its owner's admin bit; see ``_bq_guardrail_inputs`` in
    ``app/api/query.py``).

    A restricted principal (co-session / agent-session, V1d) is a frozen
    dataclass with no ``.get``: an ``AgentPrincipal`` reports its owner
    (the request legitimately runs on the owner's behalf, just
    intersection-narrowed); a ``SessionPrincipal`` reports neither.
    Supertype-agnostic ``isinstance(user, dict)`` check for the same
    future-proofing reason as ``client_kind_from_user`` above.
    """
    if user is None:
        return None, None
    if not isinstance(user, dict):
        return getattr(user, "owner_user_id", None), getattr(user, "owner_email", None)
    return user.get("id"), user.get("email")
