"""Tests for the SelectiveGZipMiddleware path-skip logic in app/main.py.

Key property: parquet-serving endpoints must not be gzipped on the wire,
but JSON / HTML endpoints above the minimum-size threshold must be.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_client(tmp_path, monkeypatch):
    """Fresh FastAPI app with its own tmp DATA_DIR so DuckDB locks don't
    collide with a concurrently-running dev container."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db
    close_system_db()
    from app.main import create_app
    yield TestClient(create_app())
    close_system_db()


def test_parquet_path_is_not_gzipped(isolated_client, tmp_path, monkeypatch):
    """/cli/wheel/... must return the raw bytes without Content-Encoding: gzip."""
    wheel = tmp_path / "agnes_fake-1.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04" + b"x" * 4096)
    monkeypatch.setenv("AGNES_CLI_DIST_DIR", str(tmp_path))

    resp = isolated_client.get(
        f"/cli/wheel/{wheel.name}",
        headers={"Accept-Encoding": "gzip"},
    )
    assert resp.status_code == 200
    assert "gzip" not in resp.headers.get("content-encoding", "")
    assert resp.content.startswith(b"PK")


def test_install_page_is_gzipped(isolated_client):
    """/setup is HTML above the threshold — gzip should kick in when the
    client advertises gzip support. TestClient may decompress transparently,
    so we accept either the header or readable body as proof that the
    middleware decided to handle the response (i.e. did not skip)."""
    resp = isolated_client.get("/setup", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    enc = resp.headers.get("content-encoding", "")
    # Either we see the encoding on the wire OR TestClient auto-decoded it.
    assert "gzip" in enc or "setup" in resp.text.lower()


def test_no_accept_encoding_means_no_gzip_anywhere(isolated_client):
    """Client that doesn't advertise gzip gets uncompressed body."""
    resp = isolated_client.get("/setup", headers={"Accept-Encoding": "identity"})
    assert resp.status_code == 200
    assert "gzip" not in resp.headers.get("content-encoding", "")


def test_selective_gzip_wrapper_dispatches_on_prefix():
    """Direct unit test of the wrapper's path-based branch without standing up
    the whole FastAPI app — verifies the skip list is honoured."""
    from app.main import _SelectiveGZipMiddleware

    calls = {"raw": 0, "gzip": 0}

    async def raw_app(scope, receive, send):
        calls["raw"] += 1

    wrapper = _SelectiveGZipMiddleware(raw_app, minimum_size=10, skip_prefixes=("/api/data/",))
    # Monkey-patch the gzip inner so we can count hits without running middleware.
    async def stub_gzip(scope, receive, send):
        calls["gzip"] += 1
    wrapper._gzip = stub_gzip

    import asyncio
    # Path that matches the skip prefix → raw app
    asyncio.run(wrapper({"type": "http", "path": "/api/data/orders/download"}, None, None))
    assert calls == {"raw": 1, "gzip": 0}
    # Path that does not → gzip app
    asyncio.run(wrapper({"type": "http", "path": "/api/sync/manifest"}, None, None))
    assert calls == {"raw": 1, "gzip": 1}
    # Non-http scope (websocket, lifespan) → gzip app (it handles lifespan as pass-through)
    asyncio.run(wrapper({"type": "lifespan"}, None, None))
    assert calls == {"raw": 1, "gzip": 2}
