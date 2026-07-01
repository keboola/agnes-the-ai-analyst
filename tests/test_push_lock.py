"""Direct behavior coverage for the single-instance lock primitive behind
`agnes update` / `agnes push` (`cli/lib/push_lock.py`). The update/push tests
stub this wrapper, so its real filelock behavior was otherwise unpinned."""

from pathlib import Path

from cli.lib.push_lock import acquire_path_or_skip


def test_acquire_yields_held_lock_and_creates_parent(tmp_path):
    """Successful acquire yields a held lock and creates the parent dir."""
    lock_file = tmp_path / "sub" / "update.lock"
    with acquire_path_or_skip(lock_file) as lock:
        assert lock is not None
        assert lock.is_locked
        assert lock_file.parent.is_dir()  # parent mkdir happened


def test_second_acquire_yields_none_when_held(tmp_path):
    """A concurrent second acquire on the same path yields None (non-blocking
    timeout=0) — exactly one holder runs, the rest no-op."""
    lock_file = tmp_path / "update.lock"
    with acquire_path_or_skip(lock_file) as first:
        assert first is not None
        with acquire_path_or_skip(lock_file) as second:
            assert second is None


def test_acquire_reusable_after_release(tmp_path):
    """Once released, the next acquire succeeds — no stale-lock tracking."""
    lock_file = tmp_path / "update.lock"
    with acquire_path_or_skip(lock_file) as first:
        assert first is not None
    with acquire_path_or_skip(lock_file) as again:
        assert again is not None


def test_mkdir_failure_yields_none(tmp_path, monkeypatch):
    """If the parent dir can't be created (read-only fs, etc.) the wrapper
    swallows the OSError and yields None rather than raising."""
    def _boom(self, *a, **k):
        raise OSError("read-only fs")

    monkeypatch.setattr(Path, "mkdir", _boom)
    with acquire_path_or_skip(tmp_path / "x" / "update.lock") as lock:
        assert lock is None
