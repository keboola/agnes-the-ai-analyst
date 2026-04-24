"""WSGI app serving the per-user bare repo over git smart-HTTP.

Mounted at `/marketplace.git` via `a2wsgi.WSGIMiddleware`. Claude Code
registers the URL:

    /plugin marketplace add https://x:<PAT>@host/marketplace.git/

git CLI does not speak Bearer tokens — it only sends HTTP Basic. By
convention (same as GitHub PATs) the username is ignored and the password
field carries the bearer token. We extract it, validate via the shared
`resolve_token_to_user`, then hand the request off to dulwich's smart-HTTP
handler scoped to the user's filtered bare repo.

Repo lifetime: dulwich writes response data via the WSGI `write()` callable,
so the returned iterable is typically empty. We wrap it in `_CloseOnExhaust`
to close the Repo handle deterministically once the body has been flushed.
"""

from __future__ import annotations

import base64
import logging
from typing import Callable, Iterable, Optional

from dulwich.repo import Repo
from dulwich.server import DictBackend
from dulwich.web import HTTPGitApplication

from app.auth.pat_resolver import resolve_token_to_user
from app.marketplace_server import git_backend
from src.db import get_system_db

logger = logging.getLogger(__name__)


def token_from_basic_auth(auth_header: Optional[str]) -> Optional[str]:
    """Extract the password (= PAT in our scheme) from an HTTP Basic header.

    Username is discarded; git CLI typically sends `x`, `x-access-token`,
    `git`, etc. Returns None for missing / malformed / non-Basic headers.
    """
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(parts[1], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    _, _, password = decoded.partition(":")
    return password or None


def _unauthorized(start_response: Callable) -> Iterable[bytes]:
    start_response(
        "401 Unauthorized",
        [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("WWW-Authenticate", 'Basic realm="agnes-marketplace"'),
        ],
    )
    return [b"authentication required\n"]


def _server_error(start_response: Callable) -> Iterable[bytes]:
    start_response(
        "500 Internal Server Error",
        [("Content-Type", "text/plain; charset=utf-8")],
    )
    return [b"internal server error\n"]


def make_git_wsgi_app() -> Callable:
    """Construct the per-request WSGI handler. The returned callable is what
    `a2wsgi.WSGIMiddleware` invokes for every mounted request."""

    def app(environ: dict, start_response: Callable) -> Iterable[bytes]:
        token = token_from_basic_auth(environ.get("HTTP_AUTHORIZATION", ""))

        conn = None
        try:
            conn = get_system_db()
        except Exception:
            logger.exception("get_system_db() failed")
            return _server_error(start_response)

        try:
            # Git channel doesn't need the reason — just auth yes/no.
            if token:
                user, _reason = resolve_token_to_user(conn, token)
            else:
                user = None
            if not user:
                return _unauthorized(start_response)

            try:
                repo_path = git_backend.ensure_repo_for_user(conn, user)
                # Use string key "/" — url_prefix() returns a str and
                # DictBackend resolves str keys directly.
                repo = Repo(str(repo_path))
            except Exception:
                logger.exception(
                    "Failed to open repo for user %r", user.get("email") or user.get("id")
                )
                return _server_error(start_response)

            try:
                backend = DictBackend({"/": repo})
                git_app = HTTPGitApplication(backend)
                inner = git_app(environ, start_response)
            except Exception:
                repo.close()
                logger.exception("dulwich failed for user %r", user.get("id"))
                return _server_error(start_response)

            return _CloseOnExhaust(inner, repo)
        finally:
            # The DB cursor can be closed early — ensure_repo_for_user and
            # resolve_token_to_user are done with it by now.
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    return app


class _CloseOnExhaust:
    """Wrap a WSGI response iterable, closing the Repo when the body is done.

    dulwich drives output through the WSGI `write()` callable, so the
    iterable itself is usually empty. We still forward `close()` in case the
    WSGI server signals early termination (client disconnect).
    """

    def __init__(self, inner: Iterable[bytes], repo: Repo) -> None:
        self._inner = inner
        self._repo = repo

    def __iter__(self):
        try:
            yield from self._inner
        finally:
            self._repo.close()

    def close(self) -> None:
        try:
            inner_close = getattr(self._inner, "close", None)
            if inner_close is not None:
                inner_close()
        finally:
            self._repo.close()
