"""Tests for #9 — CLI artifact + install script endpoints."""

import os
from pathlib import Path
import tempfile


def test_cli_install_script_bakes_server_url(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, base_url="https://agnes.example.com")
    resp = client.get("/cli/install.sh", headers={"host": "agnes.example.com"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/")
    body = resp.text
    assert "https://agnes.example.com" in body or "agnes.example.com" in body
    assert "pip install" in body or "uv tool install" in body


def test_cli_install_script_merges_config_not_clobber(monkeypatch):
    """The generated installer must MERGE config.yaml (preserve workspace_root
    and other keys on a re-run), never truncate it with `cat >`."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, base_url="https://agnes.example.com")
    body = client.get("/cli/install.sh", headers={"host": "agnes.example.com"}).text
    # No truncating write to config.yaml.
    assert 'cat > "$CFG_DIR/config.yaml"' not in body
    # Uses a merge: strip any existing server line, then append the new one.
    assert "grep -v '^server:'" in body
    assert 'printf \'server: %s\\n\' "$SERVER"' in body


def test_cli_download_returns_wheel_or_404(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/cli/download")
    # Either serve the wheel or return a clear 404 telling where to find it.
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        assert resp.headers["content-disposition"].startswith("attachment")


def test_cli_download_serves_wheel_when_present(monkeypatch, tmp_path):
    """Put a fake wheel and confirm the endpoint serves it."""
    wheel = tmp_path / "agnes_fake-1.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04fake-wheel-bytes")
    monkeypatch.setenv("AGNES_CLI_DIST_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/cli/download")
    assert resp.status_code == 200
    assert resp.content.startswith(b"PK")


def test_cli_wheel_versioned_serves_current_wheel(monkeypatch, tmp_path):
    """`/cli/wheel/{filename}` serves the current wheel and matches `/cli/download` bytes."""
    wheel = tmp_path / "agnes_fake-1.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04fake-wheel-bytes-agnes")
    monkeypatch.setenv("AGNES_CLI_DIST_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)

    resp = client.get("/cli/wheel/agnes_fake-1.0-py3-none-any.whl")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.content == wheel.read_bytes()

    resp_download = client.get("/cli/download")
    assert resp_download.status_code == 200
    assert resp.content == resp_download.content


def test_cli_wheel_versioned_rejects_other_filenames(monkeypatch, tmp_path):
    """Arbitrary `wheel_name` values must 404 — no filesystem lookup from user input."""
    wheel = tmp_path / "agnes_fake-1.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04")
    monkeypatch.setenv("AGNES_CLI_DIST_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)

    resp_wrong = client.get("/cli/wheel/other-2.0-py3-none-any.whl")
    assert resp_wrong.status_code == 404


def test_cli_agnes_whl_alias_is_gone(monkeypatch, tmp_path):
    """The bareword alias was removed — it never worked with `uv tool install`
    (uv validates the filename before fetching) and only confused users. The
    only CLI wheel URL is now `/cli/wheel/{filename}`."""
    wheel = tmp_path / "agnes_fake-1.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04")
    monkeypatch.setenv("AGNES_CLI_DIST_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/cli/agnes.whl", follow_redirects=False)
    assert resp.status_code == 404


def test_install_page_renders_with_server_url(tmp_path, monkeypatch):
    """Parallel-test isolation: GET /install routes through the shared
    system.duckdb. Without per-worker DATA_DIR isolation, two xdist
    workers exercising this test (or any DB-touching test) at the same
    time hit `Could not set lock on file …system.duckdb` because
    conftest's default DATA_DIR is shared across the worker pool."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/setup", headers={"host": "agnes.test", "Accept": "text/html"})
    assert resp.status_code == 200
    assert "agnes.test" in resp.text
    assert "agnes auth whoami" in resp.text


def test_safe_url_re_accepts_reverse_proxy_path_prefix():
    """Reverse-proxy deployments have request.base_url with a path segment
    (e.g. https://host/agnes/). The regex must accept that; the install.sh
    endpoint previously rejected it with 400."""
    from app.api.cli_artifacts import _SAFE_URL_RE
    # Path prefix (Agnes behind a reverse proxy with location /agnes/)
    assert _SAFE_URL_RE.match("https://agnes.example.com/agnes")
    assert _SAFE_URL_RE.match("https://agnes.example.com/agnes/")
    # Underscores in Docker Compose hostnames
    assert _SAFE_URL_RE.match("http://agnes_web:8000")
    # IPv6 literal
    assert _SAFE_URL_RE.match("http://[::1]:8000")
    # Still rejects obvious bad shapes
    assert not _SAFE_URL_RE.match("https://agnes.example.com/agnes;rm -rf /")
    assert not _SAFE_URL_RE.match("ftp://agnes.example.com/")
    assert not _SAFE_URL_RE.match("https://agnes.example.com/?x=$(id)")


def test_safe_url_re_rejects_trailing_newline_bypass():
    """Python's `$` matches immediately before a trailing `\\n`, so a naïve
    allowlist with `^...$` would accept "good.example.com\\n$(rm -rf /)"
    and allow shell-injection in the generated install.sh. Anchoring with
    `\\Z` closes that bypass. Covers both allowlists."""
    from app.api.cli_artifacts import _SAFE_URL_RE, _SAFE_VERSION_RE

    # Trailing newline after an otherwise-valid URL must be rejected.
    assert not _SAFE_URL_RE.match("https://good.example.com\n")
    assert not _SAFE_URL_RE.match("https://good.example.com\n$(rm -rf /)")
    assert not _SAFE_URL_RE.match("http://host:8000\nevil")
    # Sanity: the clean form still matches.
    assert _SAFE_URL_RE.match("https://good.example.com")

    # Version allowlist — same class of bypass.
    assert not _SAFE_VERSION_RE.match("1.2.3\n")
    assert not _SAFE_VERSION_RE.match("1.2.3\nrm")
    assert _SAFE_VERSION_RE.match("1.2.3")


def test_cli_install_sh_accepts_base_url_with_path_prefix(monkeypatch):
    """Reverse-proxy deployments (Caddy/Nginx routing /agnes/* to Agnes)
    surface a request.base_url like 'https://host/agnes/'. The handler
    previously 400'd on that. We call the handler directly with a stub
    request so we don't need a mounted ASGI proxy in tests."""
    import asyncio
    from types import SimpleNamespace
    from starlette.datastructures import URL
    from app.api.cli_artifacts import cli_install_script

    # Minimal Request stub — cli_install_script only needs .base_url.
    stub = SimpleNamespace(base_url=URL("https://agnes.example.com/agnes/"))
    result = asyncio.run(cli_install_script(stub))  # returns the script body
    assert isinstance(result, str)
    assert "https://agnes.example.com/agnes" in result


def _render_install_script():
    import asyncio
    from app.api.cli_artifacts import cli_install_script

    class _Req:
        base_url = "https://agnes.example.com/"

    return asyncio.run(cli_install_script(_Req()))


def _write_fakebin(fakebin: Path):
    fakebin.mkdir()
    # -OJ writes the remote-named wheel into cwd; fake it as a no-op touch.
    (fakebin / "curl").write_text(
        "#!/usr/bin/env bash\ntouch agnes_fake-1.0-py3-none-any.whl\nexit 0\n"
    )
    (fakebin / "uv").write_text("#!/usr/bin/env bash\nexit 0\n")
    for name in ("curl", "uv"):
        (fakebin / name).chmod(0o755)


def test_install_script_execution_preserves_workspace_root(tmp_path):
    """Run the generated installer against a seeded config; the merge must keep
    workspace_root and swap only the server line (executable behavior, not just
    static strings)."""
    import sys
    import subprocess

    if sys.platform.startswith("win"):
        import pytest
        pytest.skip("bash installer test is POSIX-only")

    script_path = tmp_path / "install.sh"
    script_path.write_text(_render_install_script())
    fakebin = tmp_path / "fakebin"
    _write_fakebin(fakebin)

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("server: http://old\nworkspace_root: /home/me/ws\n")

    env = {**os.environ,
           "PATH": f"{fakebin}:{os.environ['PATH']}",
           "AGNES_CONFIG_DIR": str(cfg_dir)}
    r = subprocess.run(["bash", str(script_path)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    merged = (cfg_dir / "config.yaml").read_text()
    assert "workspace_root: /home/me/ws" in merged
    assert "server: https://agnes.example.com" in merged
    assert "server: http://old" not in merged


def test_install_script_aborts_on_unreadable_config(tmp_path):
    """A real read error on the existing config must abort WITHOUT clobbering it
    (never reduce config.yaml to a single server: line)."""
    import sys
    import os as _os
    import subprocess

    if sys.platform.startswith("win"):
        import pytest
        pytest.skip("bash installer test is POSIX-only")
    if hasattr(_os, "geteuid") and _os.geteuid() == 0:
        import pytest
        pytest.skip("root bypasses file permissions")

    script_path = tmp_path / "install.sh"
    script_path.write_text(_render_install_script())
    fakebin = tmp_path / "fakebin"
    _write_fakebin(fakebin)

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg = cfg_dir / "config.yaml"
    original = "server: http://old\nworkspace_root: /home/me/ws\n"
    cfg.write_text(original)
    cfg.chmod(0o000)  # unreadable → grep exit > 1

    env = {**os.environ,
           "PATH": f"{fakebin}:{os.environ['PATH']}",
           "AGNES_CONFIG_DIR": str(cfg_dir)}
    r = subprocess.run(["bash", str(script_path)], env=env, capture_output=True, text=True)
    cfg.chmod(0o644)  # restore so we can read it back
    assert r.returncode != 0
    assert cfg.read_text() == original  # untouched, not clobbered
