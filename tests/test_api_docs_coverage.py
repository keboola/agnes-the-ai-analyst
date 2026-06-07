"""API docs coverage gate — keeps docs/api-reference.md honest.

Every public /api/* path in the OpenAPI schema must appear verbatim in
docs/api-reference.md (the "Endpoint inventory" appendix satisfies this
mechanically). A PR that adds a public endpoint without documenting it —
or explicitly exempting it below — fails CI.

Follows the repo's parity-sweep style (cf. tests/test_openapi_snapshot.py).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "api-reference.md"

# Path PREFIXES intentionally not documented in the curated guide.
# Every entry needs a one-line reason.
EXEMPT: tuple[str, ...] = (
    # populated during triage if the sweep flags internal-only surfaces;
    # keep reasons honest — "we forgot" is not a reason
)


@pytest.fixture(scope="module")
def public_api_paths() -> list[str]:
    os.environ.setdefault("TESTING", "1")
    from app.main import create_app

    app = create_app()
    return sorted(p for p in app.openapi()["paths"] if p.startswith("/api/"))


def _path_documented(path: str, doc: str) -> bool:
    """Return True iff ``path`` appears in ``doc`` as a whole token.

    The substring check (``path in doc``) silently false-passes when an
    endpoint is a prefix of an already-documented one — e.g. removing
    ``/api/health`` from the doc while ``/api/health/detailed`` remains
    would still report "documented" because ``/api/health`` is a substring
    of the surviving path. Anchor on a trailing non-path character so the
    next character must end the token (whitespace, backtick, pipe, comma,
    paren, end-of-line) — never another path segment.
    """
    pattern = re.escape(path) + r"(?![A-Za-z0-9/_\-{}])"
    return bool(re.search(pattern, doc))


def test_doc_exists():
    assert DOC_PATH.exists(), "docs/api-reference.md is missing"


def test_every_public_api_path_documented(public_api_paths):
    doc = DOC_PATH.read_text(encoding="utf-8")
    missing = [
        path
        for path in public_api_paths
        if not path.startswith(EXEMPT or ("\0",)) and not _path_documented(path, doc)
    ]
    assert not missing, (
        f"{len(missing)} public API endpoint(s) missing from docs/api-reference.md.\n"
        "Either add them to the Endpoint inventory appendix (and document non-trivial\n"
        "ones in the relevant section) or add a prefix to EXEMPT with a reason:\n"
        + "\n".join(f"  {p}" for p in missing)
    )


def test_path_documented_helper_rejects_prefix_overlap():
    """Lock the substring-match fix — see Devin Review BUG_0001 on #565.

    If we ever revert to ``path in doc``, this test fails — preventing the
    false-pass where ``/api/health`` would silently satisfy the gate because
    ``/api/health/detailed`` is in the doc.
    """
    doc = "endpoint table:\n- `/api/health/detailed` — admin diagnostics\n"
    assert _path_documented("/api/health/detailed", doc) is True
    assert _path_documented("/api/health", doc) is False, (
        "regression: /api/health passed via substring of /api/health/detailed"
    )
