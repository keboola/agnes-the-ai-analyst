from __future__ import annotations

import base64
from io import BytesIO
from typing import Callable

from app.auth.jwt import create_access_token


def _basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


class _Capture:
    def __init__(self) -> None:
        self.status: str | None = None
        self.headers: list[tuple[str, str]] = []
        self._written: list[bytes] = []

    def __call__(self, status: str, headers, exc_info=None) -> Callable[[bytes], None]:
        self.status = status
        self.headers = headers
        return self._written.append

    @property
    def body(self) -> bytes:
        return b"".join(self._written)


def _base_environ(path: str, method: str = "GET",
                  auth: str | None = None,
                  query: str = "") -> dict:
    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8000",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.input": BytesIO(b""),
        "wsgi.errors": BytesIO(),
        "wsgi.url_scheme": "http",
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if auth is not None:
        env["HTTP_AUTHORIZATION"] = auth
    return env


def _consume(resp) -> None:
    try:
        for _ in resp:
            pass
    finally:
        close = getattr(resp, "close", None)
        if close is not None:
            close()


def test_git_wsgi_401_without_auth(configured):
    from app.api.marketplace.git import make_git_wsgi_app
    app = make_git_wsgi_app()
    cap = _Capture()
    _consume(app(_base_environ("/info/refs", query="service=git-upload-pack"), cap))
    assert cap.status and cap.status.startswith("401")
    header_names = {h[0].lower() for h in cap.headers}
    assert "www-authenticate" in header_names


def test_git_wsgi_401_with_unknown_email_fallback_disabled(configured):
    from app.api.marketplace.git import make_git_wsgi_app
    app = make_git_wsgi_app()
    cap = _Capture()
    _consume(app(
        _base_environ("/info/refs", query="service=git-upload-pack",
                      auth=_basic("x", "stranger@test")),
        cap,
    ))
    assert cap.status and cap.status.startswith("401")


def test_git_wsgi_200_info_refs_with_email_fallback(configured, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    from app.api.marketplace.git import make_git_wsgi_app
    app = make_git_wsgi_app()
    cap = _Capture()
    _consume(app(
        _base_environ("/info/refs", query="service=git-upload-pack",
                      auth=_basic("x", "admin@test")),
        cap,
    ))
    assert cap.status and cap.status.startswith("200"), cap.status
    body = cap.body
    assert b"# service=git-upload-pack" in body, f"body was: {body[:200]!r}"
    assert b"refs/heads/main" in body


def test_git_wsgi_200_info_refs_with_session_jwt(seeded_admin):
    """Session JWT for a seeded user resolves through the DB-validating
    auth path (no PAT DB row required)."""
    token = create_access_token(
        user_id=seeded_admin["id"], email=seeded_admin["email"], role="admin",
    )
    from app.api.marketplace.git import make_git_wsgi_app
    app = make_git_wsgi_app()
    cap = _Capture()
    _consume(app(
        _base_environ("/info/refs", query="service=git-upload-pack",
                      auth=_basic("x", token)),
        cap,
    ))
    assert cap.status and cap.status.startswith("200"), cap.status
    assert b"refs/heads/main" in cap.body


def test_git_wsgi_401_pat_unknown_in_db(seeded_admin):
    """A PAT-typed JWT with no DB row must be rejected (revocation gap fix)."""
    token = create_access_token(
        user_id=seeded_admin["id"], email=seeded_admin["email"], role="admin",
        typ="pat", token_id="pat-never-issued", omit_exp=True,
    )
    from app.api.marketplace.git import make_git_wsgi_app
    app = make_git_wsgi_app()
    cap = _Capture()
    _consume(app(
        _base_environ("/info/refs", query="service=git-upload-pack",
                      auth=_basic("x", token)),
        cap,
    ))
    assert cap.status and cap.status.startswith("401"), cap.status


def test_git_wsgi_local_dev_mode_bypass(configured, monkeypatch):
    """LOCAL_DEV_MODE + no creds -> serves repo under dev email.

    dev@localhost isn't in the fixture's user_groups.json, so it falls
    back to the default group (which maps to no plugins in the fixture).
    The request still succeeds - default-group users get the empty-filter repo.
    """
    monkeypatch.setenv("LOCAL_DEV_MODE", "1")
    from app.api.marketplace.git import make_git_wsgi_app
    app = make_git_wsgi_app()
    cap = _Capture()
    _consume(app(
        _base_environ("/info/refs", query="service=git-upload-pack"),
        cap,
    ))
    assert cap.status and cap.status.startswith("200"), cap.status
