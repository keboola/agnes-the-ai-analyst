"""Windows User-scope CA env-var cleanup at the top of `agnes init`.

Past Agnes installs that wrote `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` /
`GIT_SSL_CAINFO` to Windows User-scope env (via the setup-prompt trust
block or an older bootstrap helper) left those pointers behind when the
target file got cleaned up. Subsequent boots → every TLS handshake on
the host fails with UnknownIssuer / FileNotFoundError before Agnes
itself runs. The 2026-05-11 Windows test user fixed the wedge manually;
`agnes init` now does it for them.

The cleanup is best-effort. Tests pin:
  - Current-process env vars pointing at non-existent paths get removed.
  - Real paths are preserved (no false positives).
  - PowerShell invocation failures don't abort the helper.
  - On non-Windows, the PowerShell branch is skipped.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def test_cleanup_removes_stale_process_env_pointing_at_missing_file(monkeypatch, tmp_path):
    """Each of the three known-bad vars gets removed when its value
    points at a path that doesn't exist on disk."""
    from cli.commands.init import _cleanup_stale_ca_env_vars

    bogus = str(tmp_path / "does-not-exist.pem")
    monkeypatch.setenv("SSL_CERT_FILE", bogus)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", bogus)
    monkeypatch.setenv("GIT_SSL_CAINFO", bogus)

    # Force the Windows branch off so we don't try to shell out in test env.
    with patch("cli.commands.init._is_windows_host", return_value=False):
        _cleanup_stale_ca_env_vars()

    assert "SSL_CERT_FILE" not in os.environ
    assert "REQUESTS_CA_BUNDLE" not in os.environ
    assert "GIT_SSL_CAINFO" not in os.environ


def test_cleanup_preserves_env_pointing_at_real_file(monkeypatch, tmp_path):
    """Operator-configured paths that DO exist must not be touched —
    the cleanup must only remove dangling pointers."""
    from cli.commands.init import _cleanup_stale_ca_env_vars

    real = tmp_path / "ca.pem"
    real.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("SSL_CERT_FILE", str(real))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(real))

    with patch("cli.commands.init._is_windows_host", return_value=False):
        _cleanup_stale_ca_env_vars()

    assert os.environ["SSL_CERT_FILE"] == str(real)
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(real)


def test_cleanup_skips_powershell_on_non_windows(monkeypatch, tmp_path):
    """The User-scope cleanup leg is a no-op outside Windows. Tests run
    on macOS / Linux — confirm we never spawn PowerShell there."""
    from cli.commands.init import _cleanup_stale_ca_env_vars

    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))

    with patch("cli.commands.init._is_windows_host", return_value=False), \
         patch("cli.commands.init.subprocess.run") as mock_run:
        _cleanup_stale_ca_env_vars()

    assert mock_run.call_count == 0


def test_cleanup_invokes_powershell_on_windows(monkeypatch, tmp_path):
    """On Windows, the cleanup shells out to PowerShell with a script
    that GetEnvironmentVariable's each var at User scope and clears
    those pointing at non-existent paths."""
    from cli.commands.init import _cleanup_stale_ca_env_vars

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("GIT_SSL_CAINFO", raising=False)

    fake_result = MagicMock()
    fake_result.stdout = "agnes init: cleared stale User-scope SSL_CERT_FILE=C:\\stale\\ca.pem (file does not exist)\n"
    fake_result.returncode = 0

    with patch("cli.commands.init._is_windows_host", return_value=True), \
         patch("cli.commands.init.subprocess.run", return_value=fake_result) as mock_run:
        _cleanup_stale_ca_env_vars()

    assert mock_run.call_count == 1
    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[0] == "powershell.exe"
    assert "-NoProfile" in cmd
    # Script body must reference all three vars + check Test-Path before deletion.
    script = cmd[-1]
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO"):
        assert var in script, f"PowerShell script must check {var}"
    assert "Test-Path" in script
    assert "SetEnvironmentVariable" in script


def test_cleanup_swallows_powershell_failures(monkeypatch):
    """PowerShell missing / blocked by execution policy must not abort
    init — the cleanup is best-effort. Verifies the FileNotFoundError /
    OSError handler silently absorbs the exception."""
    from cli.commands.init import _cleanup_stale_ca_env_vars

    with patch("cli.commands.init._is_windows_host", return_value=True), \
         patch("cli.commands.init.subprocess.run", side_effect=FileNotFoundError("powershell.exe not on PATH")):
        # Must not raise.
        _cleanup_stale_ca_env_vars()


def test_is_windows_host_reflects_sys_platform(monkeypatch):
    """Helper toggles on `sys.platform == 'win32'`. Covers native Python
    on Windows + Git Bash launchers (still 'win32' under the hood)."""
    from cli.commands.init import _is_windows_host

    monkeypatch.setattr(sys, "platform", "win32")
    assert _is_windows_host() is True

    for plat in ("darwin", "linux", "cygwin"):
        monkeypatch.setattr(sys, "platform", plat)
        assert _is_windows_host() is False
