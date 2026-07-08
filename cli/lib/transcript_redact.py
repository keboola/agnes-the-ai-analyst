"""Redact JWT-shaped tokens from Claude Code transcripts before upload.

Issue #753: analyst bootstrap pastes the raw PAT into a heredoc
(``cat > ~/.agnes/token <<EOF ... EOF``); Claude Code's transcript captures
that command verbatim, and `agnes push` used to upload the ``*.jsonl``
transcript (and ``CLAUDE.local.md``) byte-for-byte, landing the PAT
server-side. `cli/commands/init.py` deletes the on-disk ``~/.agnes/token``
file once consumed (#580), but the transcript copy survives on disk and in
the upload stream until now.

This module is the client-side scrub applied just before upload (see
`cli/commands/push.py`). It is deliberately narrow: Agnes PATs are HS256
JWTs minted by `app/auth/jwt.py`, so every real token has the
``eyJ<header>.<payload>.<signature>`` three-segment shape. Matching on that
shape (rather than a generic "long token" heuristic) avoids false positives
on git SHAs, plain base64 blobs, and ordinary prose — see
`tests/test_transcript_redact.py`.

Deliberately NOT done here: server-side scrubbing. Redacting on the way in
is enough to satisfy the acceptance criteria (no PAT in a server-uploaded
transcript reachable by following the setup prompt); a matching server-side
scrub would be redundant defense-in-depth and is out of scope for this fix.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

# Three dot-separated base64url segments, starting with the fixed JWT header
# prefix (`eyJ` = base64url of `{"`). The minimum segment lengths keep this
# from matching short, unrelated dotted tokens while still catching every
# real Agnes-minted JWT (header/payload/signature are all well over these
# floors in practice).
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")

_SENTINEL = "[REDACTED-JWT]"


def redact_text(s: str) -> str:
    """Replace every JWT-shaped substring in *s* with a fixed sentinel.

    Idempotent (the sentinel itself never matches the JWT pattern again) and
    safe across multi-line input — the character class excludes newlines, so
    a match never spans lines.
    """
    return _JWT_RE.sub(_SENTINEL, s)


def redact_bytes(data: bytes, encoding: str = "utf-8") -> bytes:
    """Decode, redact, and re-encode a byte string (e.g. a full file read)."""
    return redact_text(data.decode(encoding, errors="replace")).encode(encoding)


def redact_lines(lines: Iterable[str]) -> Iterator[str]:
    """Lazily redact an iterable of text lines (e.g. streaming a JSONL file)."""
    for line in lines:
        yield redact_text(line)
