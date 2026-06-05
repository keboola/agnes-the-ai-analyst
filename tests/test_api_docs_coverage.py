"""API docs coverage gate — keeps docs/api-reference.md honest.

Every public /api/* path in the OpenAPI schema must appear verbatim in
docs/api-reference.md (the "Endpoint inventory" appendix satisfies this
mechanically). A PR that adds a public endpoint without documenting it —
or explicitly exempting it below — fails CI.

Follows the repo's parity-sweep style (cf. tests/test_openapi_snapshot.py).
"""

from __future__ import annotations

import os
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


def test_doc_exists():
    assert DOC_PATH.exists(), "docs/api-reference.md is missing"


def test_every_public_api_path_documented(public_api_paths):
    doc = DOC_PATH.read_text(encoding="utf-8")
    missing = [
        path
        for path in public_api_paths
        if not path.startswith(EXEMPT or ("\0",)) and path not in doc
    ]
    assert not missing, (
        f"{len(missing)} public API endpoint(s) missing from docs/api-reference.md.\n"
        "Either add them to the Endpoint inventory appendix (and document non-trivial\n"
        "ones in the relevant section) or add a prefix to EXEMPT with a reason:\n"
        + "\n".join(f"  {p}" for p in missing)
    )
