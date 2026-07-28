"""Shared helpers for audit logging."""

from app.auth.scheduler_token import SCHEDULER_USER_EMAIL


# One scheduler rule for the whole codebase — the same predicate
# AuditRepository.last_scheduler_tick() has always used. Facet/KPI/timeline
# classification must not maintain a second (stale) list of action names.
SCHEDULER_ACTION_SQL = "(action LIKE 'run_%' OR action = 'marketplace.sync_all')"

# Row → source bucket. Plain SQL, identical semantics on DuckDB and Postgres.
AUDIT_SOURCE_CASE_SQL = (
    "CASE "
    "WHEN client_kind IS NOT NULL AND client_kind != '' THEN client_kind "
    f"WHEN {SCHEDULER_ACTION_SQL} THEN 'scheduler' "
    "WHEN user_id IS NULL THEN 'system' "
    "ELSE 'other' END"
)

# Row → result class. Read-side classification only — raw result values are
# preserved; see classify_result() for the Python mirror the guard test pins.
RESULT_CLASS_CASE_SQL = (
    "CASE "
    "WHEN result IS NULL THEN 'none' "
    "WHEN result IN ('success', 'ok') THEN 'success' "
    "WHEN result LIKE 'error%' THEN 'error' "
    "WHEN result IN ('denied', 'blocked', 'invalid_password', 'deactivated') THEN 'denied' "
    "ELSE 'other' END"
)

RESULT_CLASSES = ("success", "error", "denied", "none", "other")


def classify_result(value: "str | None") -> str:
    """Python mirror of RESULT_CLASS_CASE_SQL (kept in lockstep by tests)."""
    if value is None:
        return "none"
    if value in ("success", "ok"):
        return "success"
    if value.startswith("error"):
        return "error"
    if value in ("denied", "blocked", "invalid_password", "deactivated"):
        return "denied"
    return "other"


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
