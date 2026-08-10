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
