#!/usr/bin/env python3
"""Seed the triple-surface grandfather baseline (one-time, then committed).

Writes every live /api/* + /documentation/* path (minus the existing _COHORT)
to tests/api_triple_surface_grandfathered.txt, sorted. Re-runnable for audits;
never runs in CI.

Run from the repo root:  .venv/bin/python -m scripts.seed_triple_surface_baseline
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

from app.main import create_app  # noqa: E402
from tests.test_documentation_api_triple_surface import _COHORT  # noqa: E402

_OUT = _ROOT / "tests" / "api_triple_surface_grandfathered.txt"


def main() -> None:
    live = {p for p in create_app().openapi()["paths"] if p.startswith(("/api/", "/documentation/"))}
    baseline = sorted(live - set(_COHORT))
    header = (
        "# Grandfathered endpoints — existed when the triple-surface ratchet\n"
        "# (test_documentation_api_triple_surface.py) landed. Not yet required to\n"
        "# be CLI+MCP-reachable. Shrinks only: cover one -> move to _COHORT and\n"
        "# delete its line. Regenerate-for-audit:\n"
        "#   .venv/bin/python -m scripts.seed_triple_surface_baseline\n"
    )
    _OUT.write_text(header + "\n".join(baseline) + "\n", encoding="utf-8")
    print(f"wrote {len(baseline)} grandfathered paths to {_OUT}")


if __name__ == "__main__":
    main()
