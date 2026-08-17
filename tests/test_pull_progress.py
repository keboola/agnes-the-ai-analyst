"""Tests for `agnes pull` progress UX (Change 3).

The Rich progress bar handles the TTY case fine, but Claude Code's
SessionStart context — and any hook running `agnes pull` non-interactively —
has stderr connected to a pipe, not a TTY. In that case Rich either
suppresses output entirely or emits raw ANSI noise into the consumer's
log. Goal: when the caller asks for progress and stderr is not a TTY,
emit a plain-text per-10%-or-30s update so the operator gets *some*
signal instead of multi-minute silence.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "_cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg))


@pytest.fixture
def fake_pull_io(monkeypatch):
    """Stub the manifest + memory + download endpoints so run_pull can
    execute end-to-end with a fake parquet write per table."""
    canned_manifest = {
        "tables": {
            "tbl_big": {"hash": "h1", "rows": 0, "size_bytes": 1_000_000},
        },
    }
    canned_memory = {"mandatory": [], "approved": []}

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/sync/manifest":
            resp.json.return_value = canned_manifest
        elif path == "/api/memory/bundle":
            resp.json.return_value = canned_memory
        resp.raise_for_status = lambda: None
        return resp

    def _stream_download(path, target_path, progress_callback=None):
        # Simulate a chunked download: emit progress in 4 increments
        # totaling the announced size.
        total = 1_000_000
        slices = [total // 4] * 3 + [total - 3 * (total // 4)]
        Path(target_path).write_bytes(b"PAR1" + b"\x00" * 1000 + b"PAR1")
        if progress_callback:
            for s in slices:
                progress_callback(s)
        return total

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "h1", raising=False)


def test_textual_progress_when_stderr_is_not_tty(
    tmp_path,
    fake_pull_io,
    monkeypatch,
    capsys,
):
    """Non-TTY stderr → emit a plain-text progress line per file."""
    # Force the non-TTY branch even if pytest's fake stderr is a tty.
    monkeypatch.setattr("sys.stderr.isatty", lambda: False, raising=False)

    from cli.lib.pull import run_pull

    result = run_pull(
        server_url="http://x",
        token="t",
        workspace=tmp_path,
        show_progress=True,
    )
    captured = capsys.readouterr()
    # Some indication of the file + bytes ran; we don't pin exact format.
    assert "tbl_big" in captured.err
    assert result.tables_updated == 1
    # No raw ANSI escape sequences in the textual fallback.
    assert "\x1b[" not in captured.err.split("tbl_big")[0]


def test_no_progress_output_when_show_progress_is_false(
    tmp_path,
    fake_pull_io,
    monkeypatch,
    capsys,
):
    """`show_progress=False` (the SessionStart hook path) emits no
    progress text on stderr in either TTY or non-TTY mode."""
    monkeypatch.setattr("sys.stderr.isatty", lambda: False, raising=False)

    from cli.lib.pull import run_pull

    run_pull(
        server_url="http://x",
        token="t",
        workspace=tmp_path,
        show_progress=False,
    )
    captured = capsys.readouterr()
    assert "tbl_big" not in captured.err


def test_textual_progress_emits_at_completion(
    tmp_path,
    fake_pull_io,
    monkeypatch,
    capsys,
):
    """At least one final completion line gets emitted per file even if
    the throttle window doesn't trigger mid-file."""
    monkeypatch.setattr("sys.stderr.isatty", lambda: False, raising=False)
    from cli.lib.pull import run_pull

    run_pull(
        server_url="http://x",
        token="t",
        workspace=tmp_path,
        show_progress=True,
    )
    captured = capsys.readouterr()
    # Final line marks the file as done — either "100%" or a "✓ tbl_big" /
    # "tbl_big done" indicator. We accept any final-completion form.
    assert "100%" in captured.err or "done" in captured.err.lower() or "complete" in captured.err.lower()


class TestProgressIntervalKnobs:
    """Issue #203: cadence is configurable via env vars so non-TTY
    consumers (CI runners, sub-agent watchdogs) can tighten the floor
    when the default is too quiet for their dead-process detector."""

    def _stream(self):
        return io.StringIO()

    def test_default_seconds_floor_is_5s(self, monkeypatch):
        """Default cadence is 5 s (was 30 s pre-#203)."""
        monkeypatch.delenv("AGNES_PULL_PROGRESS_INTERVAL_SECONDS", raising=False)
        from cli.lib.pull import _read_progress_interval_seconds

        assert _read_progress_interval_seconds() == 5.0

    def test_default_bytes_floor_is_1mib(self, monkeypatch):
        """Default cadence is 1 MiB; complements the time-based floor."""
        monkeypatch.delenv("AGNES_PULL_PROGRESS_INTERVAL_BYTES", raising=False)
        from cli.lib.pull import _read_progress_interval_bytes

        assert _read_progress_interval_bytes() == 1024 * 1024

    def test_seconds_env_override(self, monkeypatch):
        monkeypatch.setenv("AGNES_PULL_PROGRESS_INTERVAL_SECONDS", "0.5")
        from cli.lib.pull import _read_progress_interval_seconds

        assert _read_progress_interval_seconds() == 0.5

    def test_bytes_env_override(self, monkeypatch):
        monkeypatch.setenv("AGNES_PULL_PROGRESS_INTERVAL_BYTES", "131072")
        from cli.lib.pull import _read_progress_interval_bytes

        assert _read_progress_interval_bytes() == 131072

    def test_invalid_envs_fall_back_to_default(self, monkeypatch):
        """Garbage input doesn't break the pull — fall back to defaults."""
        monkeypatch.setenv("AGNES_PULL_PROGRESS_INTERVAL_SECONDS", "nope")
        monkeypatch.setenv("AGNES_PULL_PROGRESS_INTERVAL_BYTES", "-1")
        from cli.lib.pull import (
            _read_progress_interval_bytes,
            _read_progress_interval_seconds,
        )

        assert _read_progress_interval_seconds() == 5.0
        assert _read_progress_interval_bytes() == 1024 * 1024

    def test_byte_floor_emits_more_often_than_pct_threshold(self, monkeypatch):
        """A 100 MB file with 1 MiB byte cadence should emit far more
        than 10 progress lines (the 10%-of-total cadence alone would).
        This was the operator complaint in #203: on multi-GB parquets
        the 30 s / 10 % policy produced one line every ~3 minutes."""
        monkeypatch.setenv("AGNES_PULL_PROGRESS_INTERVAL_SECONDS", "9999")
        monkeypatch.setenv("AGNES_PULL_PROGRESS_INTERVAL_BYTES", "1048576")
        from cli.lib.pull import _TextualProgress

        sink = self._stream()
        total = 100 * 1024 * 1024  # 100 MiB
        prog = _TextualProgress(stream=sink, total_files=1, file_sizes={"tbl": total})
        chunk = 64 * 1024  # 64 KiB chunks → 1600 advances
        for _ in range(total // chunk):
            prog.advance("tbl", chunk)
        prog.finish()
        emitted = sink.getvalue().count("\n")
        assert emitted >= 50, f"only {emitted} lines emitted; cadence too coarse"


# ---------------------------------------------------------------------------
# #1308 — step 8 (v49 typed stack sync) progress + logging
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_stack_sync_io(monkeypatch):
    """Stub manifest + memory endpoints with a v49 `direct_tables` entry
    (and an empty legacy `tables` dict, so step 4 issues zero downloads and
    every `stream_download` call observed in these tests came from step 8's
    `_fetcher`) plus a `stream_download` fake that records every call's
    `progress_callback` and writes deterministic bytes."""
    canned_manifest = {
        "tables": {},
        "direct_tables": [
            {
                "id": "tbl_stack",
                "name": "ops",
                "hash": "",
                "md5": "",
                "size_bytes": 2_000_000,
                "rows": 0,
                "query_mode": "local",
                "server_only": False,
                "parquet_url": "/api/data/tbl_stack/download",
            }
        ],
        "data_packages": [],
        "memory_domains": [],
    }
    canned_memory = {"mandatory": [], "approved": []}
    calls: list[dict] = []

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/sync/manifest":
            resp.json.return_value = canned_manifest
        elif path == "/api/memory/bundle":
            resp.json.return_value = canned_memory
        resp.raise_for_status = lambda: None
        return resp

    def _stream_download(path, target_path, progress_callback=None):
        calls.append({"path": path, "progress_callback": progress_callback})
        Path(target_path).write_bytes(b"PAR1" + b"\x00" * 100 + b"PAR1")
        if progress_callback:
            progress_callback(108)
        return 108

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    return calls


class TestStackSyncProgress:
    """Issue #1308: step 8's injected fetcher used to call `stream_download`
    with no `progress_callback` and no log line, so a slow multi-GB
    `data_packages`/`direct_tables` fetch looked identical to a hang."""

    def test_progress_callback_is_wired(self, tmp_path, fake_stack_sync_io):
        from cli.lib.pull import run_pull

        result = run_pull(
            server_url="http://x",
            token="t",
            workspace=tmp_path,
            show_progress=True,
        )
        assert not result.errors, result.errors
        assert len(fake_stack_sync_io) == 1
        assert fake_stack_sync_io[0]["progress_callback"] is not None, (
            "stream_download must receive a real progress_callback, not the "
            "hard-coded None from the pre-#1308 `_fetcher`"
        )

    def test_per_table_line_emitted_when_show_progress(self, tmp_path, fake_stack_sync_io, capsys):
        from cli.lib.pull import run_pull

        run_pull(
            server_url="http://x",
            token="t",
            workspace=tmp_path,
            show_progress=True,
        )
        captured = capsys.readouterr()
        assert "tbl_stack" in captured.err
        # Both a before- and an after-line — table id + human-readable size.
        assert "fetching" in captured.err
        assert "done" in captured.err
        assert "MB" in captured.err or "KB" in captured.err or "B" in captured.err

    def test_no_stack_sync_progress_output_when_show_progress_false(
        self,
        tmp_path,
        fake_stack_sync_io,
        capsys,
    ):
        """Mirrors step 4's quiet-mode contract: `show_progress=False` (the
        `--quiet` / SessionStart hook path) must stay silent, and must not
        pass a progress_callback down to `stream_download` either."""
        from cli.lib.pull import run_pull

        run_pull(
            server_url="http://x",
            token="t",
            workspace=tmp_path,
            show_progress=False,
        )
        captured = capsys.readouterr()
        assert "tbl_stack" not in captured.err
        assert len(fake_stack_sync_io) == 1
        assert fake_stack_sync_io[0]["progress_callback"] is None
