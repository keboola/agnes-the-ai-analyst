"""Shared contract for audit repositories.

Both the DuckDB-backed ``AuditRepository`` (``audit.py``) and the
Postgres-backed ``AuditPgRepository`` (``audit_pg.py``) implement this
Protocol. Tests in ``tests/db_pg/test_audit_contract.py`` parametrise
across both implementations; if either drifts from the shared surface,
the contract test fails red.

This is the pattern used for every repository that gets ported to
Postgres. Add a new repo by:
  1. Define the Protocol here (or in a sibling file).
  2. Write contract tests parametrised across [duckdb_impl, pg_impl].
  3. Build the PG impl until contract tests pass.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Tuple


class AuditRepositoryProtocol(Protocol):
    """The minimal observable surface of an Agnes audit repository."""

    def log(
        self,
        user_id: Optional[str] = None,
        action: str = "",
        resource: Optional[str] = None,
        params: Optional[dict] = None,
        result: Optional[str] = None,
        duration_ms: Optional[int] = None,
        *,
        params_before: Optional[dict] = None,
        client_ip: Optional[str] = None,
        client_kind: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> str:
        """Record one audit event; return the new row's id."""
        ...

    def query(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        action_prefix: Optional[str] = None,
        action_in: Optional[List[str]] = None,
        resource: Optional[str] = None,
        result_pattern: Optional[str] = None,
        correlation_id: Optional[str] = None,
        q: Optional[str] = None,
        cursor: Optional[Tuple[datetime, str]] = None,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], Optional[Tuple[datetime, str]]]:
        """Filtered list of audit rows + next-page cursor."""
        ...

    def query_actions(
        self,
        actions: List[str],
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Rows whose action is in the given list, newest first."""
        ...

    def query_for_resources(
        self,
        resources: List[str],
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Activity timeline for one or more resource refs."""
        ...

    def count_for_user(self, user_id: str) -> int:
        """Total audit rows recorded for one user."""
        ...

    def query_governance(
        self,
        *,
        action: Optional[str] = None,
        prefixes: Tuple[str, ...] = ("corporate_memory.", "km_"),
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Governance audit feed: corporate_memory.* + legacy km_* rows."""
        ...

    def facets(
        self,
        *,
        since: datetime,
        scheduler_actions: List[str],
        limit: int = 50,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Distinct facet buckets (users/actions/results/resources/sources)."""
        ...

    def kpis(
        self,
        *,
        since: datetime,
    ) -> Dict[str, Any]:
        """Headline KPIs: events_total, active_users, errors, p95."""
        ...
