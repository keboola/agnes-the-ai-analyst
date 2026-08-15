"""Shared first-login provisioning — the single write path for every auth
provider that auto-creates accounts (Google OAuth, Keboola OAuth).

Extracted verbatim from the Google callback so the four steps can never
drift apart per provider: create user → Everyone membership → v39
system-plugin fanout → deactivated-account rejection. (The Google-specific
Workspace group sync stays in google.py — it runs for returning users too
and is not provisioning.)
"""

import logging
import uuid

from src.repositories import users_repo

logger = logging.getLogger(__name__)


class UserDeactivatedError(Exception):
    """Raised when the identity maps to a deactivated Agnes account."""


def ensure_user(email: str, name: str, *, source: str) -> dict:
    """Return the user for ``email``, creating it on first login.

    ``source`` tags the Everyone-membership write (audit trail), e.g.
    ``"auth.google:first-signin"``.

    Raises :class:`UserDeactivatedError` for a deactivated account —
    callers translate that to their surface's 401/redirect.
    """
    repo = users_repo()
    user = repo.get_by_email(email)
    if not user:
        user_id = str(uuid.uuid4())
        repo.create(id=user_id, email=email, name=name)
        # Issue #748: auto-grant Everyone at creation (source='system_seed')
        # unless AGNES_GROUP_EVERYONE_EMAIL maps Everyone to a Workspace
        # group. Creation-time only: never called again for a returning
        # user, so an admin's manual removal later sticks.
        try:
            from app.auth.group_sync import ensure_everyone_membership

            ensure_everyone_membership(user_id, added_by=source)
        except Exception:
            logger.exception("ensure_everyone_membership failed for new user %s", email)
        # v39: subscribe new user to every system plugin so the mandatory
        # tier reaches them on their first session without an admin
        # reconcile. Fail-soft.
        try:
            from src.repositories import user_curated_subscriptions_repo

            user_curated_subscriptions_repo().fanout_system_for_user(user_id)
        except Exception:
            logger.exception("system-plugin fanout failed for new user %s", email)
        user = repo.get_by_email(email)
    if not bool(user.get("active", True)):
        raise UserDeactivatedError(email)
    return user
