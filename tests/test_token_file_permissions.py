"""Security regression: plaintext token files are written with mode 0o600.

Covers #580 Finding 2 — Agnes's own PAT must not sit in a world-readable
plaintext file at the ambient umask. Two write paths:

  1. ``cli.config.save_token`` → ``~/.config/agnes/token.json``
  2. the generated Cowork ``setup.py`` → ``token.json`` + ``.agnes-creds.json``

Both POSIX-only; skipped where the platform / filesystem doesn't honor
fchmod (Windows), mirroring the best-effort contract in the writers.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import zipfile

import pytest

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file-mode semantics only"
)


@_POSIX_ONLY
def test_save_token_writes_0600(tmp_path, monkeypatch):
    """save_token() must persist token.json with owner-only perms."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import config

    config.save_token("eyJ-fake-pat", email="analyst@example.com")

    token_file = tmp_path / "token.json"
    assert token_file.exists()
    # Contents still readable + correct (no functional regression).
    data = json.loads(token_file.read_text(encoding="utf-8"))
    assert data["access_token"] == "eyJ-fake-pat"
    assert data["email"] == "analyst@example.com"
    # Mode is owner read/write only — no group/other bits.
    mode = stat.S_IMODE(token_file.stat().st_mode)
    if not _fchmod_supported(tmp_path):
        pytest.skip("filesystem does not honor fchmod")
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


@_POSIX_ONLY
def test_save_token_overwrite_keeps_0600(tmp_path, monkeypatch):
    """Re-saving (atomic replace) must not regress the mode to the umask."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import config

    config.save_token("first", email="")
    config.save_token("second", email="")

    token_file = tmp_path / "token.json"
    assert json.loads(token_file.read_text())["access_token"] == "second"
    if not _fchmod_supported(tmp_path):
        pytest.skip("filesystem does not honor fchmod")
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def _fchmod_supported(tmp_path) -> bool:
    """Probe whether fchmod actually sticks on this tmp filesystem."""
    probe = tmp_path / ".fchmod-probe"
    try:
        fd = os.open(str(probe), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        ok = stat.S_IMODE(probe.stat().st_mode) == 0o600
    except (AttributeError, NotImplementedError, OSError):
        ok = False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return ok


@_POSIX_ONLY
def test_cowork_setup_py_writes_creds_0600(seeded_app, tmp_path, monkeypatch):
    """The generated Cowork setup.py must write token.json and
    .agnes-creds.json with mode 0o600 when it persists the PAT.

    We run the real generated script end-to-end against a fake bundle in an
    isolated HOME so the offline (pre-baked-token) path executes and lands
    the credential files; then assert their modes.
    """
    if not _fchmod_supported(tmp_path):
        pytest.skip("filesystem does not honor fchmod")

    c = seeded_app["client"]
    resp = c.post(
        "/api/user/cowork-bundle",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    folder = zf.namelist()[0].split("/")[0]
    setup_src = zf.read(f"{folder}/setup.py").decode()

    # Lay out a minimal bundle dir the script expects: setup.py is run from
    # a folder containing agnes-bundle.json. We point HOME at an isolated
    # tmp dir so ~/.config/agnes/ writes are sandboxed.
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "setup.py").write_text(setup_src, encoding="utf-8")
    bundle = json.loads(zf.read(f"{folder}/agnes-bundle.json"))
    # Drop the setup_token so the script skips the network exchange and uses
    # the pre-baked access_token (offline path) — keeps the test hermetic.
    bundle.pop("setup_token", None)
    (project / "agnes-bundle.json").write_text(json.dumps(bundle), encoding="utf-8")

    env = dict(os.environ)
    env["HOME"] = str(home)
    # Force the verifying-but-offline path; the exchange URL is unreachable
    # in-test, and with setup_token removed it's skipped entirely.
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(project / "setup.py")],
        cwd=str(project),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # The script's network steps (CLAUDE.md fetch, agnes pull) are
    # best-effort and may print warnings, but credential write (step 2)
    # is pure file I/O and must have happened. Don't assert returncode==0
    # because MCP-registration / pull legs can fail in the sandbox.
    token_json = home / ".config" / "agnes" / "token.json"
    creds_json = project / ".agnes-creds.json"
    assert token_json.exists(), f"token.json not written; stderr:\n{proc.stderr}"
    assert creds_json.exists(), f".agnes-creds.json not written; stderr:\n{proc.stderr}"

    assert json.loads(token_json.read_text())["access_token"] == bundle["access_token"]
    assert stat.S_IMODE(token_json.stat().st_mode) == 0o600, (
        f"token.json mode {oct(stat.S_IMODE(token_json.stat().st_mode))} != 0o600"
    )
    assert stat.S_IMODE(creds_json.stat().st_mode) == 0o600, (
        f".agnes-creds.json mode {oct(stat.S_IMODE(creds_json.stat().st_mode))} != 0o600"
    )
