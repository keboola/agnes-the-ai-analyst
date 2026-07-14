"""Cross-engine contract tests for the store_lint repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to both; the
same return shapes must come back. DuckDB is the contract authority. Mirrors
``test_authoring_suggestions_contract.py``.

Covers: run lifecycle (start -> replace -> finish -> last_run), latest_findings
replacement semantics, carry_forward re-tagging, dismissal filtering +
content-hash auto-reset, delete_for_entity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.duckdb_conn import _open_duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.store_lint import StoreLintRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return StoreLintRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.store_lint_pg import StoreLintPgRepository

    return StoreLintPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


def _finding(rule_id="SL002", severity="warn", message="msg", evidence=None, doc_url="/docs#sl002"):
    return {
        "rule_id": rule_id,
        "severity": severity,
        "message": message,
        "evidence": evidence if evidence is not None else {"line": 3},
        "doc_url": doc_url,
    }


def test_last_full_audit_run_ignores_publish_runs(repo):
    # A per-publish run must NOT count as the last full audit — otherwise
    # routine publishing perpetually resets the scheduled-audit interval.
    sched = repo.start_run("scheduler")
    repo.finish_run(sched, linted=1, skipped=0, findings=0)
    repo.start_run("publish")  # newer, but must be ignored by the guard

    full = repo.last_full_audit_run()
    assert full is not None
    assert full["id"] == sched
    assert full["trigger"] == "scheduler"

    # last_run() (any trigger) still sees the publish run — contrast.
    assert repo.last_run()["trigger"] == "publish"


def test_last_full_audit_run_none_when_only_publish_runs(repo):
    repo.start_run("publish")
    assert repo.last_full_audit_run() is None


def test_run_lifecycle_start_replace_finish_last_run(repo):
    run_id = repo.start_run("scheduler")
    assert run_id

    repo.replace_findings("ent_1", run_id, [_finding()], "hash_a")
    repo.finish_run(run_id, linted=1, skipped=0, findings=1)

    last = repo.last_run()
    assert last["id"] == run_id
    assert last["trigger"] == "scheduler"
    assert last["entities_linted"] == 1
    assert last["entities_skipped"] == 0
    assert last["findings_count"] == 1
    assert last["finished_at"] is not None

    # trigger filter
    run_id2 = repo.start_run("admin")
    repo.finish_run(run_id2, linted=0, skipped=0, findings=0)
    assert repo.last_run(trigger="admin")["id"] == run_id2
    assert repo.last_run(trigger="scheduler")["id"] == run_id
    assert repo.last_run(trigger="publish") is None


def test_replace_findings_deletes_prior_generation(repo):
    run1 = repo.start_run("scheduler")
    repo.replace_findings("ent_1", run1, [_finding(rule_id="SL002"), _finding(rule_id="SL010")], "hash_a")
    assert len(repo.latest_findings("ent_1")) == 2

    run2 = repo.start_run("scheduler")
    repo.replace_findings("ent_1", run2, [_finding(rule_id="SL011")], "hash_b")
    findings = repo.latest_findings("ent_1")
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "SL011"
    assert findings[0]["run_id"] == run2
    assert findings[0]["content_hash"] == "hash_b"
    # evidence round-trips as a dict on both engines
    assert findings[0]["evidence"] == {"line": 3}

    assert repo.last_content_hash("ent_1") == "hash_b"


def test_replace_findings_to_empty_clears_all(repo):
    run1 = repo.start_run("scheduler")
    repo.replace_findings("ent_1", run1, [_finding()], "hash_a")
    assert len(repo.latest_findings("ent_1")) == 1

    run2 = repo.start_run("scheduler")
    repo.replace_findings("ent_1", run2, [], "hash_clean")
    assert repo.latest_findings("ent_1") == []
    # entity state survives a clean lint — the unchanged-content skip must
    # see the hash even when the last lint produced zero findings.
    assert repo.last_content_hash("ent_1") == "hash_clean"


def test_last_content_hash_tracked_for_never_dirty_entity(repo):
    assert repo.last_content_hash("ent_new") is None
    run1 = repo.start_run("publish")
    repo.replace_findings("ent_new", run1, [], "hash_clean")
    assert repo.last_content_hash("ent_new") == "hash_clean"


def test_carry_forward_retags_without_content_change(repo):
    run1 = repo.start_run("scheduler")
    repo.replace_findings("ent_1", run1, [_finding(rule_id="SL002")], "hash_a")
    before = repo.latest_findings("ent_1")[0]

    run2 = repo.start_run("scheduler")
    repo.carry_forward("ent_1", run2)

    after = repo.latest_findings("ent_1")[0]
    assert after["run_id"] == run2
    assert after["id"] == before["id"]
    assert after["rule_id"] == before["rule_id"]
    assert after["content_hash"] == before["content_hash"]
    assert after["message"] == before["message"]


def test_dismiss_filters_latest_findings_until_content_changes(repo):
    run1 = repo.start_run("scheduler")
    repo.replace_findings(
        "ent_1",
        run1,
        [_finding(rule_id="SL002"), _finding(rule_id="SL010")],
        "hash_a",
    )

    repo.dismiss("ent_1", "SL002", "admin@x", "hash_a")

    assert repo.is_dismissed("ent_1", "SL002", "hash_a") is True
    assert repo.is_dismissed("ent_1", "SL010", "hash_a") is False
    # hash mismatch => not dismissed (auto-reset)
    assert repo.is_dismissed("ent_1", "SL002", "hash_b") is False

    visible = repo.latest_findings("ent_1", include_dismissed=False)
    assert {f["rule_id"] for f in visible} == {"SL010"}

    with_dismissed = repo.latest_findings("ent_1", include_dismissed=True)
    assert {f["rule_id"] for f in with_dismissed} == {"SL002", "SL010"}

    # content change on the entity invalidates the dismissal — same rule
    # reappears once findings are replaced with a new content_hash.
    run2 = repo.start_run("scheduler")
    repo.replace_findings("ent_1", run2, [_finding(rule_id="SL002")], "hash_b")
    assert repo.is_dismissed("ent_1", "SL002", "hash_b") is False
    visible_after_change = repo.latest_findings("ent_1", include_dismissed=False)
    assert {f["rule_id"] for f in visible_after_change} == {"SL002"}


def test_dismiss_upserts_on_repeat_call(repo):
    run1 = repo.start_run("scheduler")
    repo.replace_findings("ent_1", run1, [_finding(rule_id="SL002")], "hash_a")

    repo.dismiss("ent_1", "SL002", "admin@x", "hash_a")
    # second dismiss with a DIFFERENT hash must overwrite, not no-op — the
    # new hash wins and the old one no longer counts as dismissed.
    repo.dismiss("ent_1", "SL002", "admin2@y", "hash_b")

    assert repo.is_dismissed("ent_1", "SL002", "hash_b") is True
    assert repo.is_dismissed("ent_1", "SL002", "hash_a") is False


def test_all_latest_findings_spans_entities_and_filters_dismissed(repo):
    run1 = repo.start_run("scheduler")
    repo.replace_findings("ent_1", run1, [_finding(rule_id="SL002")], "hash_a")
    repo.replace_findings("ent_2", run1, [_finding(rule_id="SL010")], "hash_c")
    repo.dismiss("ent_1", "SL002", "admin@x", "hash_a")

    default_view = repo.all_latest_findings()
    assert {f["entity_id"] for f in default_view} == {"ent_2"}

    full_view = repo.all_latest_findings(include_dismissed=True)
    assert {f["entity_id"] for f in full_view} == {"ent_1", "ent_2"}


def test_delete_for_entity_purges_findings_and_dismissals(repo):
    run1 = repo.start_run("scheduler")
    repo.replace_findings("ent_1", run1, [_finding(rule_id="SL002")], "hash_a")
    repo.dismiss("ent_1", "SL002", "admin@x", "hash_a")

    repo.delete_for_entity("ent_1")

    assert repo.latest_findings("ent_1") == []
    assert repo.is_dismissed("ent_1", "SL002", "hash_a") is False
    assert repo.last_content_hash("ent_1") is None
