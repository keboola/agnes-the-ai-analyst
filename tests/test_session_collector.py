"""Tests for session_collector.collector."""

from pathlib import Path

import pytest

from services.session_collector.collector import copy_session_file, find_session_files


class TestCopySessionFile:
    def test_skips_if_target_exists(self, tmp_path):
        """Returns False and does not overwrite if target already exists."""
        source = tmp_path / "session.jsonl"
        source.write_text('{"event": "start"}')
        target = tmp_path / "dest" / "session.jsonl"
        target.parent.mkdir(parents=True)
        target.write_text("existing content")

        result = copy_session_file(source, target)
        assert result is False
        # Target content should not be overwritten
        assert target.read_text() == "existing content"

    def test_copies_new_file(self, tmp_path):
        """Returns True and creates the target when it does not exist."""
        source = tmp_path / "session.jsonl"
        source.write_text('{"event": "start"}')
        target = tmp_path / "dest" / "session.jsonl"

        result = copy_session_file(source, target)
        assert result is True
        assert target.exists()
        assert target.read_text() == '{"event": "start"}'

    def test_dry_run_returns_true_without_copying(self, tmp_path):
        """In dry_run mode, returns True but does not create the file."""
        source = tmp_path / "session.jsonl"
        source.write_text('{"event": "start"}')
        target = tmp_path / "dest" / "session.jsonl"

        result = copy_session_file(source, target, dry_run=True)
        assert result is True
        assert not target.exists()

    def test_creates_parent_directory(self, tmp_path):
        """Parent directories are created automatically."""
        source = tmp_path / "session.jsonl"
        source.write_text("data")
        target = tmp_path / "a" / "b" / "c" / "session.jsonl"

        copy_session_file(source, target)
        assert target.exists()

    def test_dry_run_skips_existing_target(self, tmp_path):
        """dry_run still returns False if target already exists."""
        source = tmp_path / "session.jsonl"
        source.write_text("data")
        target = tmp_path / "session.jsonl"
        target.write_text("old")

        result = copy_session_file(source, target, dry_run=True)
        assert result is False


class TestFindSessionFiles:
    def test_finds_jsonl_files(self, tmp_path):
        """find_session_files yields .jsonl files from user/sessions/."""
        user_home = tmp_path / "alice"
        sessions_dir = user_home / "user" / "sessions"
        sessions_dir.mkdir(parents=True)
        f1 = sessions_dir / "session1.jsonl"
        f2 = sessions_dir / "session2.jsonl"
        f1.write_text("{}")
        f2.write_text("{}")

        found = list(find_session_files(user_home))
        assert len(found) == 2
        assert all(f.suffix == ".jsonl" for f in found)

    def test_ignores_non_jsonl_files(self, tmp_path):
        """Non-.jsonl files are not returned."""
        user_home = tmp_path / "bob"
        sessions_dir = user_home / "user" / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "notes.txt").write_text("ignore me")
        (sessions_dir / "session.jsonl").write_text("{}")

        found = list(find_session_files(user_home))
        assert len(found) == 1
        assert found[0].name == "session.jsonl"

    def test_returns_empty_when_no_sessions_dir(self, tmp_path):
        """Returns empty iterator when user/sessions/ doesn't exist."""
        user_home = tmp_path / "carol"
        user_home.mkdir()

        found = list(find_session_files(user_home))
        assert found == []


class TestRunHelper:
    """Argv-free run() entry point — regression for #179 review (SystemExit bug)."""

    def test_run_does_not_call_argparse(self, monkeypatch, tmp_path):
        """run() must not parse sys.argv — uvicorn's argv would SystemExit(2) the worker.

        Regression: app/api/admin.py:run_session_collector previously called
        collector.main() which did argparse.parse_args() on uvicorn's argv.
        """
        from services.session_collector import collector

        monkeypatch.setattr(
            "sys.argv",
            ["app.main:app", "--host", "0.0.0.0", "--port", "8000",
             "--proxy-headers", "--forwarded-allow-ips=*"],
        )
        monkeypatch.setattr(collector, "TARGET_BASE", tmp_path / "user_sessions")
        monkeypatch.setattr(collector, "find_user_home_dirs", lambda: iter([]))

        rc, stats = collector.run(dry_run=True, verbose=False)
        assert rc == 0
        assert stats == {"users_processed": 0, "files_copied": 0, "files_skipped": 0}

    def test_run_returns_stats_tuple(self, monkeypatch, tmp_path):
        """run() returns (exit_code, stats_dict) so the admin endpoint can audit."""
        from services.session_collector import collector

        monkeypatch.setattr(collector, "TARGET_BASE", tmp_path / "user_sessions")
        monkeypatch.setattr(collector, "find_user_home_dirs", lambda: iter([]))

        rc, stats = collector.run()
        assert rc == 0
        assert set(stats.keys()) == {"users_processed", "files_copied", "files_skipped"}

    def test_main_still_delegates_to_run(self, monkeypatch, tmp_path):
        """The CLI main() must continue to work — argparse + delegate."""
        from services.session_collector import collector

        monkeypatch.setattr("sys.argv", ["session_collector", "--dry-run"])
        monkeypatch.setattr(collector, "TARGET_BASE", tmp_path / "user_sessions")
        monkeypatch.setattr(collector, "find_user_home_dirs", lambda: iter([]))

        rc = collector.main()
        assert rc == 0


class TestRunSkipEnvVar:
    """AGNES_SKIP_LEGACY_COLLECTOR=1 short-circuits the run before any FS or
    grp lookups. Used in the Docker layout where /home/*/user/sessions/ is
    empty by design — keeps logs quiet without auto-detect logic that would
    mask real bare-VM mis-deploys.
    """

    def test_collector_run_skips_when_env_set(self, monkeypatch, tmp_path):
        """AGNES_SKIP_LEGACY_COLLECTOR=1 → return early with skipped=True."""
        from services.session_collector import collector

        monkeypatch.setenv("AGNES_SKIP_LEGACY_COLLECTOR", "1")
        # Point TARGET_BASE at tmp_path so even if the skip didn't fire we
        # wouldn't touch /data — but the assertion below is that mkdir
        # was NOT called on it.
        target = tmp_path / "user_sessions"
        monkeypatch.setattr(collector, "TARGET_BASE", target)

        # If the skip didn't fire, find_user_home_dirs would be called.
        called = []

        def _spy():
            called.append(True)
            return iter([])

        monkeypatch.setattr(collector, "find_user_home_dirs", _spy)

        rc, stats = collector.run()
        assert rc == 0
        assert stats.get("skipped") is True
        assert stats["files_copied"] == 0
        assert stats["users_processed"] == 0
        assert stats["files_skipped"] == 0
        # Skip path must NOT touch the target directory or call into the
        # /home scanner — those are exactly the operations we're avoiding.
        assert not target.exists(), "TARGET_BASE.mkdir should not have run"
        assert called == [], "find_user_home_dirs should not have been called"

    @pytest.mark.parametrize("val", ["1", "true", "TRUE"])
    def test_collector_run_skips_for_truthy_values(self, monkeypatch, tmp_path, val):
        """The accepted truthy spellings are 1 / true / TRUE. Anything else
        (including '0', 'false', 'yes') falls through to the normal pass."""
        from services.session_collector import collector

        monkeypatch.setenv("AGNES_SKIP_LEGACY_COLLECTOR", val)
        monkeypatch.setattr(collector, "TARGET_BASE", tmp_path / "user_sessions")
        monkeypatch.setattr(collector, "find_user_home_dirs", lambda: iter([]))

        rc, stats = collector.run()
        assert rc == 0
        assert stats.get("skipped") is True

    def test_collector_run_full_pass_when_env_unset(self, monkeypatch, tmp_path):
        """No env var → existing scan path runs (returns stats without 'skipped')."""
        from services.session_collector import collector

        monkeypatch.delenv("AGNES_SKIP_LEGACY_COLLECTOR", raising=False)
        target = tmp_path / "user_sessions"
        monkeypatch.setattr(collector, "TARGET_BASE", target)
        monkeypatch.setattr(collector, "find_user_home_dirs", lambda: iter([]))

        rc, stats = collector.run()
        assert rc == 0
        # Bare-VM path: we ran, even if no users were scanned.
        assert "skipped" not in stats
        # mkdir should have happened.
        assert target.exists()

    def test_collector_run_full_pass_for_falsy_values(self, monkeypatch, tmp_path):
        """AGNES_SKIP_LEGACY_COLLECTOR='0' should NOT skip — only the explicit
        truthy spellings short-circuit."""
        from services.session_collector import collector

        monkeypatch.setenv("AGNES_SKIP_LEGACY_COLLECTOR", "0")
        monkeypatch.setattr(collector, "TARGET_BASE", tmp_path / "user_sessions")
        monkeypatch.setattr(collector, "find_user_home_dirs", lambda: iter([]))

        rc, stats = collector.run()
        assert rc == 0
        assert "skipped" not in stats
