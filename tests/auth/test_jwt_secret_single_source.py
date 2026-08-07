"""One key-resolution path for every server-side JWT signature.

``app/auth/jwt.py::_get_secret_key`` is the fail-closed resolver: production
without ``JWT_SECRET_KEY`` refuses to boot, local dev auto-generates and
persists a per-instance key, and only ``TESTING=1`` falls back to the dev
constant committed in that module.

A caller that re-reads ``os.environ.get("JWT_SECRET_KEY", "<literal>")``
itself opts out of all of it: it signs with the committed constant whenever the
env var is absent, and — under local dev, where the resolver hands out an
auto-generated key that is never exported to the environment — it signs with a
key the verifier does not hold. Both tests below pin that: the behavioural one
for the chat runner token, the ratchet for anyone who adds the next such read.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("app", "cli", "services", "src", "connectors")

# The one module allowed to spell the fallback: it *is* the resolver.
_ALLOWED = ("app/auth/jwt.py",)

# ``os.environ.get("JWT_SECRET_KEY", "…")`` / ``environ.get(…)``, in any
# line-wrapping — the real finding (app/auth/access.py) spanned four lines.
# The default must be a NON-empty literal: ``environ.get("JWT_SECRET_KEY", "")``
# is an is-it-configured probe that fails closed (app/main.py's chat gate), not
# a signing key.
_FALLBACK_RE = re.compile(
    r"""environ(?:\.get|\.setdefault)?\(\s*["']JWT_SECRET_KEY["']\s*,\s*["'][^"']""",
)


def _scan(paths) -> list[str]:
    """Return ``file:line`` for every hardcoded-fallback read in *paths*."""
    hits: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _FALLBACK_RE.finditer(text):
            try:
                rel = path.resolve().relative_to(REPO_ROOT).as_posix()
            except ValueError:
                rel = path.as_posix()
            if rel in _ALLOWED:
                continue
            hits.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
    return hits


def _source_files():
    for d in SCAN_DIRS:
        yield from sorted((REPO_ROOT / d).rglob("*.py"))


# ── behaviour: the runner token verifies with the canonical key ──


class _StubUsers:
    def get_by_email(self, email):
        return {"id": "user-uuid-1", "email": email}


def test_mint_session_jwt_signs_with_the_canonical_secret(monkeypatch):
    """The chat runner token must verify through the same resolver the
    request path verifies with, even when the environment variable and the
    resolved key differ (local dev with an auto-generated ``.jwt_secret``)."""
    import app.auth.access as access
    import app.auth.jwt as jwtmod
    import src.repositories as repos

    monkeypatch.setenv("JWT_SECRET_KEY", "env-only-key-of-32-chars-length!!")
    monkeypatch.setattr(jwtmod, "_get_cached_secret_key", lambda: "resolved-key-of-32-chars-length!!")
    monkeypatch.setattr(repos, "users_repo", lambda: _StubUsers())

    token = access.mint_session_jwt("u@example.com", "chat-1")

    payload = jwtmod.verify_token(token)
    assert payload is not None, "runner JWT does not verify with the canonical signing key"
    assert payload["sub"] == "user-uuid-1"
    assert payload["scope"] == "chat"
    assert payload["chat_session_id"] == "chat-1"


# ── ratchet: nobody re-reads the env var with a literal fallback ──


def test_no_hardcoded_jwt_secret_fallback_outside_the_resolver():
    hits = _scan(_source_files())
    assert not hits, (
        "hardcoded JWT_SECRET_KEY fallback outside app/auth/jwt.py — route the "
        "signature through app.auth.jwt (get_signing_secret / create_access_token) "
        f"instead: {hits}"
    )


def test_ratchet_detects_a_planted_fallback(tmp_path):
    """The detector must fail on a known-bad file — a guard that matches
    nothing passes vacuously forever."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        'secret = os.environ.get(\n    "JWT_SECRET_KEY",\n    "test-jwt-secret-key-minimum-32-chars!!",\n)\n'
    )
    assert _scan([planted])
