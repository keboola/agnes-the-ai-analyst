"""Boot-time install of operator-provided stdio-MCP wheels.

stdio-transport MCP sources spawn ``command`` as a subprocess inside the
app container, but anything an operator installs by hand (``docker exec
pip install …``) is wiped on every container recreate — and recreates are
routine now that auto-upgrade tracks releases. The bootstrap installs any
wheels dropped into ``${DATA_DIR}/mcp/wheels/`` (the persistent data
volume) at startup: fail-soft per wheel, idempotent via a content-hash
marker, and ``~/.local/bin`` is put on PATH so console scripts resolve
when the stdio client spawns them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from connectors.mcp.wheel_bootstrap import (
    ensure_user_bin_on_path,
    install_operator_wheels,
)


def _fake_run_factory(calls, rc=0):
    class _R:
        def __init__(self):
            self.returncode = rc
            self.stdout = ""
            self.stderr = "boom" if rc else ""

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    return _fake_run


def test_missing_dir_is_noop(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("connectors.mcp.wheel_bootstrap.subprocess.run", _fake_run_factory(calls))
    assert install_operator_wheels(tmp_path) == []
    assert calls == []


def test_installs_new_wheel_and_writes_marker(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("connectors.mcp.wheel_bootstrap.subprocess.run", _fake_run_factory(calls))
    wheels = tmp_path / "mcp" / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "some_mcp-1.0-py3-none-any.whl").write_bytes(b"fake-wheel-bytes")

    installed = install_operator_wheels(tmp_path)

    assert installed == ["some_mcp-1.0-py3-none-any.whl"]
    assert len(calls) == 1
    assert "--user" in calls[0] and "--no-deps" in calls[0]
    marker = json.loads((wheels / ".installed.json").read_text())
    assert "some_mcp-1.0-py3-none-any.whl" in marker


def test_skips_already_installed_wheel(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("connectors.mcp.wheel_bootstrap.subprocess.run", _fake_run_factory(calls))
    monkeypatch.setattr("connectors.mcp.wheel_bootstrap._distribution_present", lambda _: True)
    wheels = tmp_path / "mcp" / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "a-1.whl").write_bytes(b"AAA")
    install_operator_wheels(tmp_path)
    calls.clear()

    assert install_operator_wheels(tmp_path) == []  # same content → skip
    assert calls == []

    # Content change → reinstall (hash mismatch).
    (wheels / "a-1.whl").write_bytes(b"BBB")
    assert install_operator_wheels(tmp_path) == ["a-1.whl"]
    assert len(calls) == 1


def test_reinstalls_when_marker_matches_but_distribution_is_gone(tmp_path, monkeypatch):
    """Container recreate: the marker (persistent /data volume) survives, the
    ``pip install --user`` target (ephemeral container FS) does not. A
    hash-matching marker alone must NOT skip the install — otherwise every
    recreate after the first successful boot leaves the stdio command
    missing and the source fails with ``[Errno 2] No such file or directory``.
    """
    calls = []
    monkeypatch.setattr("connectors.mcp.wheel_bootstrap.subprocess.run", _fake_run_factory(calls))
    monkeypatch.setattr("connectors.mcp.wheel_bootstrap._distribution_present", lambda _: False)
    wheels = tmp_path / "mcp" / "wheels"
    wheels.mkdir(parents=True)
    whl = "some_mcp-1.0-py3-none-any.whl"
    (wheels / whl).write_bytes(b"fake-wheel-bytes")
    install_operator_wheels(tmp_path)
    assert len(calls) == 1
    calls.clear()

    # Same marker, same wheel content, but the distribution vanished with
    # the old container filesystem → must reinstall, not skip.
    assert install_operator_wheels(tmp_path) == [whl]
    assert len(calls) == 1


def test_distribution_present_checks_real_metadata():
    from connectors.mcp.wheel_bootstrap import _distribution_present

    # pytest is installed in the test venv; wheel filenames carry the dist
    # name as the first dash-separated segment.
    assert _distribution_present("pytest-8.0.0-py3-none-any.whl") is True
    # Underscore escaping in wheel filenames must resolve to the dashed
    # distribution name (PEP 427 escaping).
    assert _distribution_present("typing_extensions-4.0-py3-none-any.whl") is True
    assert _distribution_present("definitely_not_installed_xyz-1.0-py3-none-any.whl") is False


def test_failing_wheel_is_fail_soft_and_not_marked(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("connectors.mcp.wheel_bootstrap.subprocess.run", _fake_run_factory(calls, rc=1))
    wheels = tmp_path / "mcp" / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "bad-1.whl").write_bytes(b"X")
    (wheels / "good-2.whl").write_bytes(b"Y")

    installed = install_operator_wheels(tmp_path)

    assert installed == []  # nothing succeeded
    assert len(calls) == 2  # but both were attempted
    marker = json.loads((wheels / ".installed.json").read_text())
    assert marker == {}  # failures not marked → retried next boot


def test_ensure_user_bin_on_path_idempotent(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    ensure_user_bin_on_path()
    p1 = os.environ["PATH"]
    assert str(Path.home() / ".local" / "bin") in p1.split(os.pathsep)
    ensure_user_bin_on_path()
    assert os.environ["PATH"] == p1  # no duplicate prepend
