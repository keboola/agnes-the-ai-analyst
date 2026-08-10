"""
`_meta` is refreshed once per coalesced rebuild, not once per Jira event.

`update_meta` opens `extract.duckdb` for writing; the rebuild the same event enqueues
ATTACHes it; DuckDB is single-writer. A lost ATTACH is only logged, and the rebuild then
swaps in a freshly built analytics DB with no Jira views — so the tables disappear until a
later rebuild wins. That happened on a live instance the day the per-event transform first
started reaching this code.

Moving the pass into `_run_jira_refresh` makes the writer and the reader sequential inside
one job, and drops the cost from "a write-open plus a full count over every partition of
six tables, per event" to "once per coalesced rebuild".

Two ordering facts are load-bearing and pinned below: `update_meta` runs BEFORE the
rebuild (it is what creates `extract.duckdb` on a fresh install, and `rebuild_source`
returns early without one), and a failure in it must not cost us the rebuild.
"""

import ast
from pathlib import Path

import pytest

from app.worker import kinds

JIRA_TABLES = ("issues", "comments", "attachments", "changelog", "issuelinks", "remote_links")
TRANSFORM_MODULE = Path(__file__).resolve().parent.parent / "connectors" / "jira" / "incremental_transform.py"


@pytest.fixture()
def traced(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record the ordered sequence of update_meta calls and the rebuild."""
    calls: list = []

    def _fake_update_meta(output_dir, table_name):
        calls.append(("update_meta", table_name))

    class _FakeOrchestrator:
        def rebuild_source(self, name):
            calls.append(("rebuild_source", name))

    monkeypatch.setattr("connectors.jira.extract_init.update_meta", _fake_update_meta)
    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", _FakeOrchestrator)
    return calls


def test_refresh_covers_every_table_then_rebuilds(traced: list) -> None:
    kinds._run_jira_refresh({})

    assert [name for kind, name in traced if kind == "update_meta"] == list(JIRA_TABLES)
    assert traced[-1] == ("rebuild_source", "jira")


def test_meta_runs_before_the_rebuild(traced: list) -> None:
    """`update_meta` creates extract.duckdb when missing; the rebuild needs it to exist."""
    kinds._run_jira_refresh({})

    kinds_only = [kind for kind, _ in traced]
    assert kinds_only.index("rebuild_source") > max(
        i for i, kind in enumerate(kinds_only) if kind == "update_meta"
    ), "rebuild_source ran before update_meta finished creating/refreshing extract.duckdb"


def test_a_failing_meta_pass_still_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale catalog numbers must not cost us the rebuild that publishes the data."""
    rebuilt: list = []

    def _boom(output_dir, table_name):
        raise RuntimeError("extract.duckdb is locked")

    class _FakeOrchestrator:
        def rebuild_source(self, name):
            rebuilt.append(name)

    monkeypatch.setattr("connectors.jira.extract_init.update_meta", _boom)
    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", _FakeOrchestrator)

    kinds._run_jira_refresh({})

    assert rebuilt == ["jira"]


def test_the_per_event_transform_no_longer_touches_meta() -> None:
    """The whole point: no `update_meta` call may return to the per-event path.

    Read off the source, so this holds regardless of which branches a test happens to
    drive through `transform_single_issue`.
    """
    tree = ast.parse(TRANSFORM_MODULE.read_text(), filename=str(TRANSFORM_MODULE))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)) == "update_meta"
    ]

    assert not offenders, (
        f"update_meta called from {TRANSFORM_MODULE.name} at line(s) {offenders} — that is the "
        "per-event path, and it races the rebuild for extract.duckdb. It belongs in "
        "app.worker.kinds._run_jira_refresh."
    )


class TestSlaPollEnqueuesTheRefresh:
    """The SLA poller writes parquet too, so it owes the same rebuild.

    It calls `transform_single_issue` directly rather than going through
    `trigger_incremental_transform`, so it never enqueued anything. That was
    harmless while the transform refreshed `_meta` inline; once that moved to the
    job, a poller-only instance would never refresh the catalog — or, for a table
    whose first partition the poller writes, never create its extract view, since
    `update_meta` does a `CREATE OR REPLACE VIEW` as well.
    """

    @staticmethod
    def _drive(monkeypatch, results, enqueue_status="queued"):
        """Run poll_sla.run() over `results`, returning the idempotency keys enqueued."""
        from connectors.jira.scripts import poll_sla

        enqueued: list = []

        class _FakeJobs:
            def enqueue(self, kind, payload, idempotency_key=None):
                enqueued.append(idempotency_key)
                # Only the FIRST enqueue reports the caller's status; the follow-up
                # carries a distinct key and so never dedups onto anything.
                return {"status": enqueue_status if len(enqueued) == 1 else "queued"}

        keys = [f"PROJ-{i}" for i in range(1, len(results) + 1)]
        monkeypatch.setattr(
            poll_sla,
            "load_config",
            lambda: {"data_dir": Path("/srv/raw"), "base_url": "https://x", "email": "e", "api_token": "t"},
        )
        monkeypatch.setattr(poll_sla, "configured_field_ids", lambda: ["customfield_1"])
        monkeypatch.setattr(poll_sla, "find_open_issues", lambda _d: keys)
        monkeypatch.setattr(poll_sla, "update_issue_sla", lambda k, *a, **kw: results[keys.index(k)])
        monkeypatch.setattr(poll_sla.time, "sleep", lambda _s: None)
        monkeypatch.setattr("src.repositories.jobs_repo", lambda: _FakeJobs())

        stats = poll_sla.run()
        return enqueued, stats

    def test_a_run_that_wrote_something_enqueues_exactly_one_refresh(self, monkeypatch) -> None:
        enqueued, stats = self._drive(monkeypatch, ["updated", "skipped", "healed"])

        assert enqueued == ["jira-refresh"], "one coalesced rebuild per run, not per issue"
        assert stats["updated"] == 1 and stats["healed"] == 1

    def test_a_run_that_wrote_nothing_enqueues_nothing(self, monkeypatch) -> None:
        """This runs every 15 minutes; a rebuild with nothing to publish is pure cost."""
        enqueued, _ = self._drive(monkeypatch, ["skipped", "skipped"])

        assert enqueued == []

    def test_an_unreachable_job_queue_does_not_fail_the_poll(self, monkeypatch) -> None:
        """The module also runs as a standalone script; the poll's own work is already durable."""
        from connectors.jira.scripts import poll_sla

        monkeypatch.setattr(
            poll_sla,
            "load_config",
            lambda: {"data_dir": Path("/srv/raw"), "base_url": "https://x", "email": "e", "api_token": "t"},
        )
        monkeypatch.setattr(poll_sla, "configured_field_ids", lambda: ["customfield_1"])
        monkeypatch.setattr(poll_sla, "find_open_issues", lambda _d: ["PROJ-1"])
        monkeypatch.setattr(poll_sla, "update_issue_sla", lambda *a, **kw: "updated")
        monkeypatch.setattr(poll_sla.time, "sleep", lambda _s: None)

        def _boom():
            raise RuntimeError("no queue here")

        monkeypatch.setattr("src.repositories.jobs_repo", _boom)

        stats = poll_sla.run()

        assert stats["updated"] == 1

    def test_dedup_onto_a_running_refresh_gets_a_follow_up(self, monkeypatch) -> None:
        """A RUNNING job may have read the parquet before this run's writes landed.

        The webhook path states this invariant in `connectors/jira/service.py`; the
        poller owes the same guarantee, and nothing else recovers a lost write —
        the next poll only enqueues if it writes again.
        """
        enqueued, _ = self._drive(monkeypatch, ["updated"], enqueue_status="running")

        assert enqueued == ["jira-refresh", "jira-refresh-followup"]

    def test_dedup_onto_a_queued_refresh_needs_no_follow_up(self, monkeypatch) -> None:
        """A queued job has not read anything yet — one row is enough."""
        enqueued, _ = self._drive(monkeypatch, ["updated"], enqueue_status="queued")

        assert enqueued == ["jira-refresh"]
