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
