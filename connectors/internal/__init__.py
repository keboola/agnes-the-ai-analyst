"""Internal data source — Agnes serving its own telemetry tables through
the same query plumbing analysts use for Keboola / BigQuery / Jira.

The motivation is recursive observability: AI agents on the analyst side
can query their own usage history (which tools fail, which sessions
stalled, which audit actions they trigger) through the same DuckDB-shaped
catalog they already know. Admins get the unfiltered view; everyone else
gets row-level scoped views built per-request.

Three tables exposed today (all read-only):
- ``agnes_sessions``  → ``usage_session_summary`` filtered by ``username``
- ``agnes_usage``     → ``usage_events`` filtered by ``username``
- ``agnes_audit``     → ``audit_log`` filtered by ``user_id``

Source-of-truth contract:
- ``connectors/internal/access.py`` owns the table → (source, filter column,
  filter value resolver) mapping. Adding a new internal table is one row
  in ``INTERNAL_TABLES`` plus an ``ensure_registered`` entry.
- The /api/query path in ``app/api/query.py`` checks for internal-table
  references and routes the SQL to ``execute_internal_query`` instead of
  the analytics-DB path. Mixing internal tables with BQ / local registry
  rows in a single SQL statement is rejected in v1.
"""
