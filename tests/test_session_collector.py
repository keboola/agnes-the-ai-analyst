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
