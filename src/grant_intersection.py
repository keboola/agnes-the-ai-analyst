"""Set-intersection of co-session participants' grants, per ResourceType.

NEVER applies the Admin god-mode short-circuit (SR-1): each participant's
contribution is their real grant set from _allowed_ids_for_user. An admin
participant contributes the full set, so intersect(full, non_admin) ==
non_admin. Fail-closed: an empty participant list, an unknown participant,
or any participant with zero grants for a type collapses that type (or the
whole result) to empty.

PG-parity: resolves emails through the repository factory and reads grants
through _allowed_ids_for_user (which is factory-routed) — no raw SQL on the
passed conn.
"""
from __future__ import annotations

from typing import Optional

import duckdb

from app.resource_types import ResourceType


def compute_grant_intersection(
    participant_emails: list[str],
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> dict[str, frozenset[str]]:
    if not participant_emails:
        return {}
    from app.auth.access import _allowed_ids_for_user
    from src.repositories import use_pg, users_repo

    def _user_by_email(email: str):
        if conn is not None and not use_pg():
            from src.repositories.users import UserRepository
            return UserRepository(conn).get_by_email(email)
        return users_repo().get_by_email(email)

    user_ids: list[str] = []
    for email in participant_emails:
        row = _user_by_email(email)
        if not row:
            return {}  # unknown participant -> fail closed
        user_ids.append(row["id"])

    result: dict[str, frozenset[str]] = {}
    for rt in ResourceType:
        sets = [_allowed_ids_for_user(uid, rt.value, conn) for uid in user_ids]
        acc: Optional[frozenset[str]] = None
        for s in sets:
            acc = s if acc is None else (acc & s)
        if acc:
            result[rt.value] = acc
    return result
