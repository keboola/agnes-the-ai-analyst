"""Self-test for the real_llm marker gate registered in conftest.py.

Without AGNES_E2E_ANTHROPIC the `pytest_collection_modifyitems` hook
adds a skip marker to every test tagged `real_llm`. This file ships
one such test so the gate is exercised on every run — if the hook
breaks (typo, wrong env var name, marker no longer recognized), the
test below will surface as an unexpected FAIL and CI will catch it.

When AGNES_E2E_ANTHROPIC is set, the test runs and trivially passes —
that's the opt-in path.
"""

from __future__ import annotations

import os

import pytest


@pytest.mark.real_llm
def test_real_llm_gate_is_active() -> None:
    """If you see this test FAIL without AGNES_E2E_ANTHROPIC, the gate is broken."""
    assert os.environ.get("AGNES_E2E_ANTHROPIC"), (
        "real_llm marker gate failed — this test should have been skipped "
        "by the pytest_collection_modifyitems hook in tests/e2e/conftest.py"
    )
