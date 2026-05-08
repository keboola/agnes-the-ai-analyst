"""Tests for the session-pipeline framework (services/session_pipeline/).

Covers:
- Pure utility functions (parse_jsonl, compute_file_hash) and their behavior on
  edge cases (malformed lines, file changes).
- SessionProcessorStateRepository CRUD on a fresh in-memory schema.
- run_processor end-to-end with fake processors covering success, raise,
  empty-result, and file-hash-invalidation paths.
- v29 migration: existing session_extraction_state rows are copied to
  session_processor_state with processor_name='verification' and the old
  table is dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from services.session_pipeline.contract import ProcessorResult
from services.session_pipeline.lib import compute_file_hash, parse_jsonl
from services.session_pipeline.runner import run_processor
from src.repositories.session_processor_state import SessionProcessorStateRepository


def _fresh_db(tmp_path, monkeypatch) -> duckdb.DuckDBPyConnection:
    """Same idiom as tests/test_corporate_memory_v1.py — fresh schema in tmp_path."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    db_module._system_db_conn = None
    db_module._system_db_path = None
    return db_module.get_system_db()


# ---------------------------------------------------------------------------
# parse_jsonl
# ---------------------------------------------------------------------------

class TestParseJsonl:
    def test_parses_well_formed_lines(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text(
            json.dumps({"role": "user", "content": "hi"}) + "\n"
            + json.dumps({"role": "assistant", "content": "hello"}) + "\n"
        )
        turns = parse_jsonl(f)
        assert len(turns) == 2
        assert turns[0]["role"] == "user"
        assert turns[1]["content"] == "hello"

    def test_skips_malformed_lines(self, tmp_path):
        """Same behavior as pre-refactor verification_detector.parse_session —
        a single corrupt row mustn't abort processing of the rest."""
        f = tmp_path / "session.jsonl"
        f.write_text(
            json.dumps({"role": "user", "content": "ok"}) + "\n"
            + "this is not json\n"
            + json.dumps({"role": "assistant", "content": "still ok"}) + "\n"
        )
        turns = parse_jsonl(f)
        assert len(turns) == 2
        assert turns[0]["content"] == "ok"
        assert turns[1]["content"] == "still ok"

    def test_skips_blank_lines(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text(
            "\n"
            + json.dumps({"role": "user", "content": "x"}) + "\n"
            + "   \n"
        )
        turns = parse_jsonl(f)
        assert len(turns) == 1


# ---------------------------------------------------------------------------
# compute_file_hash
# ---------------------------------------------------------------------------

class TestComputeFileHash:
    def test_deterministic(self, tmp_path):
        f = tmp_path / "x.jsonl"
        f.write_text("hello world")
        assert compute_file_hash(f) == compute_file_hash(f)

    def test_changes_with_content(self, tmp_path):
        f = tmp_path / "x.jsonl"
        f.write_text("v1")
        h1 = compute_file_hash(f)
        f.write_text("v2")
        h2 = compute_file_hash(f)
        assert h1 != h2


# ---------------------------------------------------------------------------
# SessionProcessorStateRepository
# ---------------------------------------------------------------------------

class TestSessionProcessorStateRepository:
    def test_unprocessed_when_empty(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = SessionProcessorStateRepository(conn)
        assert repo.is_processed("verification", "alice/s.jsonl", "abc") is False
        conn.close()

    def test_mark_then_is_processed(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = SessionProcessorStateRepository(conn)
        repo.mark_processed("verification", "alice/s.jsonl", "alice", 3, "abc")
        assert repo.is_processed("verification", "alice/s.jsonl", "abc") is True
        conn.close()

    def test_independent_per_processor(self, tmp_path, monkeypatch):
        """Two processors track the same session independently — usage might be
        done while verification still has work."""
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = SessionProcessorStateRepository(conn)
        repo.mark_processed("usage", "alice/s.jsonl", "alice", 0, "abc")
        assert repo.is_processed("usage", "alice/s.jsonl", "abc") is True
        assert repo.is_processed("verification", "alice/s.jsonl", "abc") is False
        conn.close()

    def test_hash_mismatch_treated_as_unprocessed(self, tmp_path, monkeypatch):
        """When a session jsonl grows (live append from active Claude Code),
        the stored file_hash no longer matches → processor gets to reprocess."""
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = SessionProcessorStateRepository(conn)
        repo.mark_processed("verification", "alice/s.jsonl", "alice", 1, "old_hash")
        assert repo.is_processed("verification", "alice/s.jsonl", "new_hash") is False
        conn.close()

    def test_mark_upserts_on_re_run(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = SessionProcessorStateRepository(conn)
        repo.mark_processed("verification", "alice/s.jsonl", "alice", 1, "h1")
        repo.mark_processed("verification", "alice/s.jsonl", "alice", 5, "h2")
        row = conn.execute(
            "SELECT items_extracted, file_hash FROM session_processor_state WHERE processor_name=? AND session_file=?",
            ["verification", "alice/s.jsonl"],
        ).fetchone()
        assert row == (5, "h2")
        conn.close()

    def test_scan_unprocessed_returns_all_when_empty_state(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        (sessions / "alice").mkdir(parents=True)
        (sessions / "alice" / "s1.jsonl").write_text("{}")
        (sessions / "alice" / "s2.jsonl").write_text("{}")
        repo = SessionProcessorStateRepository(conn)
        results = repo.scan_unprocessed_for("verification", sessions)
        keys = sorted([f"{u}/{p.name}" for u, p in results])
        assert keys == ["alice/s1.jsonl", "alice/s2.jsonl"]
        conn.close()

    def test_scan_skips_non_directory_entries(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "stray.txt").write_text("not a user dir")
        (sessions / "alice").mkdir()
        (sessions / "alice" / "s.jsonl").write_text("{}")
        repo = SessionProcessorStateRepository(conn)
        results = repo.scan_unprocessed_for("verification", sessions)
        assert len(results) == 1
        assert results[0][0] == "alice"
        conn.close()


# ---------------------------------------------------------------------------
# run_processor
# ---------------------------------------------------------------------------

class _FakeProcessor:
    """Test double that records its calls and is configurable per behavior."""

    def __init__(
        self,
        name: str = "fake",
        cadence_minutes: int = 10,
        return_value: ProcessorResult | None = None,
        raise_on_session: str | None = None,
    ):
        self.name = name
        self.cadence_minutes = cadence_minutes
        self.return_value = return_value if return_value is not None else ProcessorResult(items_count=0)
        self.raise_on_session = raise_on_session
        self.calls: list[str] = []

    def process_session(self, session_path: Path, username: str, session_key: str, conn):
        self.calls.append(session_key)
        if self.raise_on_session is not None and session_key == self.raise_on_session:
            raise RuntimeError("simulated processor failure")
        return self.return_value


def _seed_session(sessions_dir: Path, username: str, name: str, content: str = "{}\n") -> Path:
    user_dir = sessions_dir / username
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / name
    path.write_text(content)
    return path


class TestRunProcessor:
    def test_processed_then_skipped_on_second_call(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "s.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=2))

        stats1 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats1["processed"] == 1
        assert stats1["items_extracted"] == 2
        assert proc.calls == ["alice/s.jsonl"]

        stats2 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats2["processed"] == 0
        assert stats2["skipped"] == 1
        assert proc.calls == ["alice/s.jsonl"]  # not invoked again
        conn.close()

    def test_raise_leaves_state_unwritten(self, tmp_path, monkeypatch):
        """A processor that raises must not be marked as processed — the runner
        retries the same session on the next tick."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "s.jsonl")

        proc = _FakeProcessor(raise_on_session="alice/s.jsonl")

        stats = run_processor(conn, proc, session_data_dir=sessions)
        assert stats["errors"] == 1
        assert stats["processed"] == 0

        # State row absent: next call sees the session again.
        repo = SessionProcessorStateRepository(conn)
        assert repo.is_processed(proc.name, "alice/s.jsonl", "anything") is False

        # Second call retries.
        proc.raise_on_session = None  # this time succeed
        stats2 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats2["processed"] == 1
        conn.close()

    def test_empty_result_marks_processed(self, tmp_path, monkeypatch):
        """0 items extracted is a valid outcome — UsageProcessor skeleton
        relies on this so its no-op runs aren't re-scanned every tick."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "bob", "s.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=0))

        stats1 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats1["processed"] == 1
        assert stats1["items_extracted"] == 0

        stats2 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats2["processed"] == 0
        assert stats2["skipped"] == 1
        conn.close()

    def test_file_hash_invalidates_state(self, tmp_path, monkeypatch):
        """When a session jsonl grows (Claude Code live-appends to an active
        session), the stored hash no longer matches → reprocessed."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        path = _seed_session(sessions, "alice", "s.jsonl", content="line1\n")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))

        stats1 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats1["processed"] == 1

        # Mutate the file → new hash → reprocessed on next call.
        path.write_text("line1\nline2\n")
        stats2 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats2["processed"] == 1
        assert proc.calls == ["alice/s.jsonl", "alice/s.jsonl"]
        conn.close()

    def test_processors_isolated(self, tmp_path, monkeypatch):
        """Two processors on the same session work independently — what one
        marked, the other still has to do."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "s.jsonl")

        proc_a = _FakeProcessor(name="a")
        proc_b = _FakeProcessor(name="b")

        run_processor(conn, proc_a, session_data_dir=sessions)
        run_processor(conn, proc_b, session_data_dir=sessions)

        assert proc_a.calls == ["alice/s.jsonl"]
        assert proc_b.calls == ["alice/s.jsonl"]
        conn.close()

    def test_no_sessions_dir_returns_clean_stats(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        proc = _FakeProcessor()
        stats = run_processor(conn, proc, session_data_dir=tmp_path / "does_not_exist")
        assert stats["scanned"] == 0
        assert stats["processed"] == 0
        assert stats["errors"] == 0
        conn.close()

    def test_non_processor_result_return_coerced(self, tmp_path, monkeypatch):
        """A processor that returns the wrong type must not poison the state
        write — the runner coerces it to an empty result and still marks the
        session processed (alternative: retry forever)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "s.jsonl")

        class _BadReturn:
            name = "bad"
            cadence_minutes = 1
            def process_session(self, *a, **kw):
                return None  # type: ignore[return-value]

        stats = run_processor(conn, _BadReturn(), session_data_dir=sessions)
        assert stats["processed"] == 1
        assert stats["items_extracted"] == 0
        conn.close()


# ---------------------------------------------------------------------------
# v29 migration — verification rows preserved, old table dropped
# ---------------------------------------------------------------------------

class TestV29Migration:
    """Exercise the v28 → v29 migration directly. Builds a v28 schema (using
    the pre-v29 idiom inline so the test doesn't depend on _SYSTEM_SCHEMA's
    current shape), seeds data, runs the v29 migrations, asserts the result.
    """

    def test_existing_rows_become_verification_processor_rows(self, tmp_path):
        conn = duckdb.connect(":memory:")
        # Recreate the pre-v29 table shape — single-key session_file PK.
        conn.execute(
            """
            CREATE TABLE session_extraction_state (
                session_file VARCHAR PRIMARY KEY,
                username VARCHAR NOT NULL,
                processed_at TIMESTAMP DEFAULT current_timestamp,
                items_extracted INTEGER DEFAULT 0,
                file_hash VARCHAR
            )
            """
        )
        conn.execute(
            "INSERT INTO session_extraction_state VALUES (?, ?, ?, ?, ?)",
            ["alice/s1.jsonl", "alice", "2026-01-01 00:00:00", 3, "abc"],
        )

        # Run v29 migration steps via the helper (which conditionally copies
        # from the legacy table when present).
        from src.db import _v30_to_v31_migrate
        _v30_to_v31_migrate(conn)

        # New table has the row tagged with processor_name='verification'.
        rows = conn.execute(
            "SELECT processor_name, session_file, username, items_extracted, file_hash "
            "FROM session_processor_state ORDER BY session_file"
        ).fetchall()
        assert rows == [("verification", "alice/s1.jsonl", "alice", 3, "abc")]

        # Old table is gone.
        existing = {
            r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        assert "session_extraction_state" not in existing
        assert "session_processor_state" in existing
        conn.close()

    def test_migration_idempotent_when_new_table_exists(self, tmp_path):
        """Fresh installs run _SYSTEM_SCHEMA (which already has session_processor_state)
        AND the migration ladder. The v29 migration must not crash if the new
        table already exists empty."""
        conn = duckdb.connect(":memory:")
        # Pre-create both tables (simulating fresh install + ladder rerun).
        conn.execute(
            """
            CREATE TABLE session_extraction_state (
                session_file VARCHAR PRIMARY KEY,
                username VARCHAR NOT NULL,
                processed_at TIMESTAMP,
                items_extracted INTEGER,
                file_hash VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE session_processor_state (
                processor_name VARCHAR NOT NULL,
                session_file VARCHAR NOT NULL,
                username VARCHAR NOT NULL,
                processed_at TIMESTAMP,
                items_extracted INTEGER,
                file_hash VARCHAR,
                PRIMARY KEY (processor_name, session_file)
            )
            """
        )

        from src.db import _v30_to_v31_migrate
        _v30_to_v31_migrate(conn)

        existing = {
            r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        assert "session_extraction_state" not in existing
        assert "session_processor_state" in existing
        conn.close()
