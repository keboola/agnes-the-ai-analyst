"""WSGI app that authenticates, loads the caller's bare repo, and hands
off to dulwich's smart-HTTP implementation.

Ported from marketplace-server/app/git_router.py with the auth path
replaced by `_auth.resolve_email_from_basic` (PAT primary, email fallback
env-gated, LOCAL_DEV_MODE bypass).
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable

from dulwich.repo import Repo
from dulwich.server import DictBackend
from dulwich.web import HTTPGitApplication

from app.api.marketplace import _auth, _git_backend as git_backend

logger = logging.getLogger(__name__)


def make_git_wsgi_app() -> Callable[[dict, Callable], Iterable[bytes]]:
    """Return a WSGI app scoped to the mount point it's installed at.

    Auth -> email -> cached bare repo -> dulwich HTTPGitApplication. The repo
    is closed deterministically after the response body drains (dulwich
    writes via the WSGI write() callable, not by yielding from the iterable).
    """
    def app(environ: dict, start_response: Callable) -> Iterable[bytes]:
        auth = environ.get("HTTP_AUTHORIZATION", "")
        email = _auth.resolve_email_from_basic(auth)
        if not email:
            start_response(
                "401 Unauthorized",
                [
                    ("Content-Type", "text/plain; charset=utf-8"),
                    ("WWW-Authenticate", 'Basic realm="agnes-marketplace"'),
                ],
            )
            return [b"authentication required\n"]

        try:
            repo_path = git_backend.ensure_repo_for_email(email)
            repo = Repo(str(repo_path))
        except FileNotFoundError:
            logger.warning("marketplace source unavailable for %r", email)
            start_response(
                "503 Service Unavailable",
                [("Content-Type", "text/plain; charset=utf-8")],
            )
            return [b"marketplace source unavailable\n"]
        except Exception:
            logger.exception("Failed to open repo for email %r", email)
            start_response(
                "500 Internal Server Error",
                [("Content-Type", "text/plain; charset=utf-8")],
            )
            return [b"internal server error\n"]

        try:
            backend = DictBackend({"/": repo})
            git_app = HTTPGitApplication(backend)
            inner = git_app(environ, start_response)
        except Exception:
            repo.close()
            logger.exception("dulwich failed for email %r", email)
            start_response(
                "500 Internal Server Error",
                [("Content-Type", "text/plain; charset=utf-8")],
            )
            return [b"internal server error\n"]

        return _CloseOnExhaust(inner, repo)

    return app


class _CloseOnExhaust:
    """Wraps a WSGI response iterable, calling repo.close() when done.

    dulwich writes response bytes through start_response's write() callable,
    so the iterable is typically empty. We still need to close the repo
    after the WSGI server finishes, and forward close() for early disconnect.
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
