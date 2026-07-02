"""Tests for the Windows deferred self-update helper (cli/commands/_win_deferred_update).

The helper runs OUTSIDE the agnes tool venv and does the swap after the agnes
process exits. These tests exercise its pure logic (PID-wait / uv-install /
verify / rollback / status) with subprocess mocked, so they run on any OS."""

import json

from cli.commands import _win_deferred_update as h


def test_run_success_writes_status_and_lkg(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "_wait_for_exit", lambda pid, **k: None)
    monkeypatch.setattr(h, "_uv_install", lambda wheel, **k: 0)
    monkeypatch.setattr(h, "_installed_version_ok", lambda v: True)

    wheel = tmp_path / "0.72.2.whl"
    wheel.write_bytes(b"new-wheel-bytes")
    cfg = tmp_path / "cfg"
    cfg.mkdir()

    rc = h.run(1234, str(wheel), "0.72.2", str(cfg), None)
    assert rc == 0

    status = json.loads((cfg / "upgrade_status.json").read_text())
    assert status["last_outcome"] == "success"
    assert status["consecutive_failures"] == 0

    lkg = json.loads((cfg / "last_known_good.json").read_text())
    assert lkg["version"] == "0.72.2"
    assert lkg["wheel_filename"] == "0.72.2.whl"
    assert lkg["sha256"]


def test_run_install_fails_records_failure_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "_wait_for_exit", lambda pid, **k: None)
    monkeypatch.setattr(h, "_uv_install", lambda wheel, **k: 2)  # never succeeds
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "upgrade_status.json").write_text('{"consecutive_failures": 1}')

    rc = h.run(1, str(tmp_path / "x.whl"), "0.72.2", str(cfg), None)
    assert rc == 2

    status = json.loads((cfg / "upgrade_status.json").read_text())
    assert status["last_outcome"] == "failure"
    assert status["consecutive_failures"] == 2       # incremented from prior 1
    assert "rc=2" in status["last_failure_reason"]


def test_run_verify_fails_rolls_back(monkeypatch, tmp_path):
    installed = []
    monkeypatch.setattr(h, "_wait_for_exit", lambda pid, **k: None)
    monkeypatch.setattr(h, "_uv_install", lambda wheel, **k: installed.append(wheel) or 0)
    monkeypatch.setattr(h, "_installed_version_ok", lambda v: False)  # smoke fails

    staged = tmp_path / "0.72.2.whl"
    staged.write_bytes(b"new")
    rollback = tmp_path / "0.72.1.whl"
    rollback.write_bytes(b"old")
    cfg = tmp_path / "cfg"
    cfg.mkdir()

    rc = h.run(1, str(staged), "0.72.2", str(cfg), str(rollback))
    assert rc == 1
    assert installed == [str(staged), str(rollback)]  # installed new, then rolled back

    status = json.loads((cfg / "upgrade_status.json").read_text())
    assert status["last_outcome"] == "failure"


def test_main_parses_args_and_empty_rollback_is_none(monkeypatch):
    seen = {}

    def _cap(*a):
        seen["args"] = a
        return 0

    monkeypatch.setattr(h, "run", _cap)
    rc = h.main(["1234", "w.whl", "0.72.2", "/cfg", ""])
    assert rc == 0
    assert seen["args"] == (1234, "w.whl", "0.72.2", "/cfg", None)


def test_main_usage_error_on_missing_args():
    assert h.main(["1234", "w.whl"]) == 64


def test_venv_free_true_when_absent_or_openable(tmp_path):
    # No path / missing file → "attempt anyway" (True). A present, not-running
    # file is openable for write → free (True). (The locked-running-exe case is
    # Windows-runtime-only and can't be simulated portably.)
    assert h._venv_free(None) is True
    assert h._venv_free(str(tmp_path / "missing.exe")) is True
    py = tmp_path / "python.exe"
    py.write_bytes(b"stub")
    assert h._venv_free(str(py)) is True


def test_run_clears_updating_sentinel_on_success(monkeypatch, tmp_path):
    # The status-bar "step aside" sentinel must not linger once the swap is done.
    monkeypatch.setattr(h, "_wait_for_exit", lambda pid, **k: None)
    monkeypatch.setattr(h, "_uv_install", lambda wheel, **k: 0)
    monkeypatch.setattr(h, "_installed_version_ok", lambda v: True)

    wheel = tmp_path / "0.72.3.whl"
    wheel.write_bytes(b"w")
    cfg = tmp_path / "cfg"
    cfg.mkdir()

    assert h.run(1, str(wheel), "0.72.3", str(cfg), None) == 0
    assert not (cfg / "deferred-update.active").exists()


def test_run_clears_updating_sentinel_on_install_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "_wait_for_exit", lambda pid, **k: None)
    monkeypatch.setattr(h, "_uv_install", lambda wheel, **k: 2)
    cfg = tmp_path / "cfg"
    cfg.mkdir()

    assert h.run(1, str(tmp_path / "x.whl"), "0.72.3", str(cfg), None) == 2
    assert not (cfg / "deferred-update.active").exists()


def test_looks_like_lock_only_matches_real_locks():
    # Real Windows file-lock errors are retried; anything else (a bad wheel
    # filename, uv missing) must NOT be treated as a lock — otherwise it burns
    # the whole retry budget mislabeled as "venv locked", which is exactly what
    # hid the staged-wheel-filename bug.
    assert h._looks_like_lock("failed to remove directory ...Scripts: Access is denied. (os error 5)")
    assert h._looks_like_lock("cannot access the file because it is being used by another process")
    assert not h._looks_like_lock('The wheel filename "0.72.4.whl" is invalid: Must have a version')
    assert not h._looks_like_lock("")
