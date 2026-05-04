"""Packaging regression tests — guard against silent prod-vs-dev dep drift.

`anthropic` and `openai` are imported by `connectors/llm/anthropic_provider.py`
and `connectors/llm/openai_compat.py`. Those modules run in production from
`services/corporate_memory` and `services/verification_detector`. If they
slip back into `[project.optional-dependencies].dev` the Dockerfile (which
only installs core deps) will boot-loop on `ModuleNotFoundError`. See #176.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _read_pyproject() -> dict:
    """Load pyproject.toml from the repo root."""
    try:
        import tomllib  # py3.11+
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore

    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def test_anthropic_is_a_core_dependency():
    """anthropic must live in [project].dependencies, not [dev].

    Production code (connectors/llm/anthropic_provider.py) imports the SDK
    unconditionally. Demoting it to dev resurrects the #176 boot loop.
    """
    cfg = _read_pyproject()
    core = cfg["project"]["dependencies"]
    assert any(dep.startswith("anthropic") for dep in core), (
        "anthropic must be in [project].dependencies — see #176"
    )


def test_openai_is_a_core_dependency():
    """openai must live in [project].dependencies, not [dev]."""
    cfg = _read_pyproject()
    core = cfg["project"]["dependencies"]
    assert any(dep.startswith("openai") for dep in core), (
        "openai must be in [project].dependencies — see #176"
    )


def test_anthropic_not_in_optional_dev_extras():
    """Belt-and-suspenders: dev extras should not double-list anthropic."""
    cfg = _read_pyproject()
    dev = cfg["project"].get("optional-dependencies", {}).get("dev", [])
    assert not any(dep.startswith("anthropic") for dep in dev), (
        "anthropic should not be duplicated in [dev] — keep it core-only"
    )


def test_openai_not_in_optional_dev_extras():
    """Belt-and-suspenders: dev extras should not double-list openai."""
    cfg = _read_pyproject()
    dev = cfg["project"].get("optional-dependencies", {}).get("dev", [])
    assert not any(dep.startswith("openai") for dep in dev), (
        "openai should not be duplicated in [dev] — keep it core-only"
    )


def test_llm_provider_modules_import_cleanly():
    """A fresh interpreter with only core deps installed must import the
    LLM provider modules without ImportError. This is the actual behavior
    that breaks the scheduler container when anthropic/openai are dev-only.
    """
    # Just importing here proves the deps resolve in the active env. The
    # pyproject.toml assertions above keep the contract going forward.
    import importlib

    for mod in (
        "connectors.llm.anthropic_provider",
        "connectors.llm.openai_compat",
        "connectors.llm.factory",
    ):
        importlib.import_module(mod)
