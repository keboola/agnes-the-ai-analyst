"""Issue #752: the fully-materialized remote-select path
(`run_remote_select_to_arrow`, backing `agnes query --remote --auto-snapshot`)
must run its billable BigQuery job through the labeled `client.query` helper
(`run_bq_query_to_arrow`) — not the unlabeled DuckDB `bigquery_query()`
extension — whenever the query can be pushed entirely to BQ. Queries that
can't be pushed fall back to the extension path unlabeled.
"""

import contextlib
from unittest.mock import MagicMock

import pyarrow as pa

from app.api import query as query_module


class _FakeAnalytics:
    """Stands in for the read-only analytics DuckDB connection."""

    def __init__(self):
        self.executed: list[str] = []

    def execute(self, sql):
        self.executed.append(sql)
        result = MagicMock()
        result.arrow.return_value = pa.table({"c": [0]})
        return result

    def close(self):
        pass


class _FakeQuota:
    def check_daily_budget(self, user=None):
        pass

    @contextlib.contextmanager
    def acquire(self, user=None):
        yield

    def record_bytes(self, user=None, n=0):
        pass


def _common_patches(monkeypatch, analytics):
    # Admin (allowed is None) → skip the per-view RBAC loop and its
    # information_schema query.
    monkeypatch.setattr(query_module, "get_accessible_tables", lambda user, conn: None)
    monkeypatch.setattr(query_module, "get_analytics_db_readonly", lambda: analytics)
    monkeypatch.setattr(query_module, "find_internal_refs", lambda sql: False)
    # No BQ dry-run set → skip the estimate/quota-record branch.
    monkeypatch.setattr(
        query_module, "_bq_guardrail_inputs", lambda *a, **k: ([], [], None)
    )
    monkeypatch.setattr(
        query_module,
        "job_labels_for",
        lambda user, agent: {
            "agent_name": agent,
            "user_id": (user.get("email") or "").split("@")[0],
        },
    )


def test_rewritten_remote_select_runs_labeled_client_query(monkeypatch):
    analytics = _FakeAnalytics()
    _common_patches(monkeypatch, analytics)

    inner = "SELECT country FROM `data-proj.ds.web_view`"
    monkeypatch.setattr(
        query_module,
        "_bq_remote_execution_plan",
        lambda sql, conn: (
            f"SELECT * FROM bigquery_query('bp', $bqq_inner${inner}$bqq_inner$)",
            True,
            "bp",
            inner,
        ),
    )

    captured = {}
    fake_table = pa.table({"country": ["CZ", "US"]})

    def _fake_run(bq, sql, *, labels=None):
        captured["sql"] = sql
        captured["labels"] = labels
        return fake_table, {"bq_job_id": "j-1", "bytes_scanned": 5, "bytes_billed": 6}

    monkeypatch.setattr(query_module, "run_bq_query_to_arrow", _fake_run)

    table = query_module.run_remote_select_to_arrow(
        conn=MagicMock(),
        user={"email": "pcernik@example.com"},
        sql="SELECT country FROM web_view",
        bq=MagicMock(),
        quota=_FakeQuota(),
    )

    # Billable job ran via the labeled client.query helper with the BQ-native
    # inner SQL and the "query" agent label — NOT via the DuckDB extension.
    assert table.num_rows == 2
    assert captured["sql"] == inner
    assert captured["labels"]["agent_name"] == "query"
    assert captured["labels"]["user_id"] == "pcernik"
    assert analytics.executed == []  # extension path not used


def test_non_rewritten_remote_select_falls_back_to_extension(monkeypatch):
    analytics = _FakeAnalytics()
    _common_patches(monkeypatch, analytics)

    # Cross-source / un-pushable query → did_rewrite False, no inner SQL.
    monkeypatch.setattr(
        query_module,
        "_bq_remote_execution_plan",
        lambda sql, conn: (sql, False, None, None),
    )

    called = {"run": False}

    def _fake_run(bq, sql, *, labels=None):
        called["run"] = True
        return pa.table({"c": [1]}), {}

    monkeypatch.setattr(query_module, "run_bq_query_to_arrow", _fake_run)

    table = query_module.run_remote_select_to_arrow(
        conn=MagicMock(),
        user={"email": "pcernik@example.com"},
        sql="SELECT * FROM web_view JOIN local_view USING (id)",
        bq=MagicMock(),
        quota=_FakeQuota(),
    )

    assert table.num_rows == 1
    assert called["run"] is False  # labeled path skipped
    assert analytics.executed  # extension path used
