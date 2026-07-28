"""Shared helpers for audit logging."""

from app.auth.scheduler_token import SCHEDULER_USER_EMAIL


def client_kind_from_user(user: dict) -> str:
    """Detect CLI vs web vs scheduler from the auth state.

    Order of precedence:
    1. scheduler user → 'scheduler'
    2. PAT-authenticated (token_type='pat' set by get_current_user) → 'cli'
    3. anything else → 'web'
    """
    if user is None:
        return "web"
    if user.get("email") == SCHEDULER_USER_EMAIL:
        return "scheduler"
    if user.get("token_type") == "pat":
        return "cli"
    return "web"
