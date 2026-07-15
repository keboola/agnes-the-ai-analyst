"""Table-access checks — thin wrappers over ``app.auth.access.can_access``.

Module exists for legacy import paths (``app/api/data.py``, ``app/api/sync.py``,
``app/api/catalog.py``, ``app/api/v2_*``, ``app/api/query.py``) that already
import ``can_access_table`` / ``get_accessible_tables`` from here. The
authorization itself flows through ``app.auth.access`` — this file is a
shim mapping the table-grain helpers onto the generic resource_grants check.
"""

from typing import Optional

import duckdb
from fastapi import HTTPException

from src.db import get_system_db


def table_not_in_stack_message(table_id: str) -> str:
    """Standardized 403 detail string for table-access denial.

    All CLI surfaces (`agnes query`, `agnes snapshot create`,
    `agnes data <id>/download`, `/api/v2/schema`, `/api/v2/sample`)
    funnel through ``can_access_table`` and return this same string so
    the analyst's mental model stays consistent: "the table I asked
    about isn't in my stack — admin needs to add it to a Data Package".
    """
    return (
        f"Table '{table_id}' is not in your stack. Ask an admin to add it "
        f"to a Data Package you have access to (Required or in your stack), "
        f"then run `agnes pull` to refresh."
    )


def require_table_access(
    user: dict,
    table_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> None:
    """Convenience: ``can_access_table`` or raise 403 with the standard
    message. Centralizes the deny path so every CLI surface returns the
    same actionable error.
    """
    if not can_access_table(user, table_id, conn):
        raise HTTPException(
            status_code=403,
            detail=table_not_in_stack_message(table_id),
        )


def can_access_table(
    user,  # dict | SessionPrincipal
    table_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """True iff the user can read ``table_id``.

    Three sources of access (in precedence order):
      1. Internal data-source tables (``agnes_sessions`` / ``agnes_telemetry``
         / ``agnes_audit``) — implicitly accessible to every authenticated
         user. RBAC there is row-level (the per-request view filters to the
         caller's rows). Admin gets the unscoped view; non-admin gets their
         own rows.
      2. Admin god-mode — members of the Admin system group see every
         registered table (dict users only; SessionPrincipal is never admin).
      3. **Stack-gated**: the table must belong to at least one data
         package in the user's stack (required ∪ subscribed). Per-table
         resource_grants alone NO LONGER grant analyst visibility — the
         unified-stack design routes all analyst access through data
         packages. Admins manage access by adding tables to a package +
         granting the package; ad-hoc per-table grants in
         ``resource_grants`` are a no-op for analysts (still consulted
         for backwards-compat fallback inside admin-only flows).
    """
    from connectors.internal.access import is_internal_table

    if is_internal_table(table_id):
        return True

    from app.auth.session_principal import SessionPrincipal

    if isinstance(user, SessionPrincipal):
        from app.auth.access import can_access_session
        from app.resource_types import ResourceType

        # Co-session: intersection membership, no admin short-circuit, no
        # personal stack. The intersection is the sole authority.
        return can_access_session(user, ResourceType.TABLE.value, table_id)

    user_id = user.get("id")
    if not user_id:
        return False

    should_close = False
    if conn is None:
        conn = get_system_db()
        should_close = True
    try:
        from app.auth.access import is_user_admin

        if is_user_admin(user_id, conn):
            return True

        from app.services.stack_resolver import StackResolver
        from app.resource_types import ResourceType

        resolver = StackResolver(conn)
        pkg_entries = resolver.stack(user_id, ResourceType.DATA_PACKAGE)
        if not pkg_entries:
            return False
        pkg_ids_set = {e.id for e in pkg_entries}
        from src.repositories import data_packages_repo as _dp_repo

        table_pkg_ids = {p["id"] for p in _dp_repo().list_packages_of_table(table_id)}
        return bool(pkg_ids_set & table_pkg_ids)
    finally:
        if should_close:
            conn.close()


def get_accessible_ids(
    user,  # dict | SessionPrincipal
    resource_type: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> Optional[frozenset]:
    """Grant-based accessible id set for ``resource_type``. ``None`` means
    "all" (admin).

    Symmetric to :func:`get_accessible_tables` but generic over any
    ``resource_grants`` resource_type (RECIPE, COLLECTION, DATA_PACKAGE, …)
    and grant-model only — this is NOT the stack-gated table model, so it
    must not be used for ``table`` access (see :func:`can_access_table` /
    :func:`get_accessible_tables` for that).

    For a ``SessionPrincipal``, returns the intersection id-set for
    ``resource_type`` (never ``None`` — no admin god-mode for a co-session).
    """
    from app.auth.session_principal import SessionPrincipal

    if isinstance(user, SessionPrincipal):
        return frozenset(user.intersection.get(resource_type, frozenset()))

    user_id = user.get("id")
    if not user_id:
        return frozenset()

    should_close = False
    if conn is None:
        conn = get_system_db()
        should_close = True
    try:
        from app.auth.access import is_user_admin, _allowed_ids_for_user

        if is_user_admin(user_id, conn):
            return None  # admin sees everything

        return _allowed_ids_for_user(user_id, resource_type, conn)
    finally:
        if should_close:
            conn.close()


def get_accessible_tables(
    user,  # dict | SessionPrincipal
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> Optional[list[str]]:
    """List of table IDs the user can read. ``None`` means "all" (admin).

    Stack-gated for analysts: the set is the union of
      * internal tables (row-level RBAC at query time), and
      * tables belonging to data packages in the user's stack
        (required ∪ subscribed).
    Per-table ``resource_grants(group, 'table', …)`` rows are NO LONGER
    consulted for analyst visibility — see :func:`can_access_table`.

    For a ``SessionPrincipal``, returns the intersection table-ids plus
    internal tables (never ``None`` — no admin god-mode for a co-session).
    """
    from app.auth.session_principal import SessionPrincipal

    if isinstance(user, SessionPrincipal):
        from app.resource_types import ResourceType
        from connectors.internal.access import INTERNAL_TABLES

        result = list(user.intersection.get(ResourceType.TABLE.value, frozenset()))
        for t in INTERNAL_TABLES:
            if t.registry_id not in result:
                result.append(t.registry_id)
        return result

    user_id = user.get("id")
    if not user_id:
        return []

    should_close = False
    if conn is None:
        conn = get_system_db()
        should_close = True
    try:
        from app.auth.access import is_user_admin

        if is_user_admin(user_id, conn):
            return None  # admin sees everything

        from app.services.stack_resolver import StackResolver
        from app.resource_types import ResourceType

        resolver = StackResolver(conn)
        pkg_entries = resolver.stack(user_id, ResourceType.DATA_PACKAGE)
        result: list[str] = []
        if pkg_entries:
            pkg_ids_set = {e.id for e in pkg_entries}
            from src.repositories import data_packages_repo as _dp_repo

            result = _dp_repo().list_member_table_ids(pkg_ids_set)
        # Internal tables — always accessible (row-level RBAC at query time).
        from connectors.internal.access import INTERNAL_TABLES

        for t in INTERNAL_TABLES:
            if t.registry_id not in result:
                result.append(t.registry_id)
        return result
    finally:
        if should_close:
            conn.close()
