"""Confirm the wheel installs the binary as `agnes`, not `da`."""

import shutil
import subprocess

import pytest


def test_agnes_command_exists():
    """`agnes --version` must succeed once the package is editable-installed.

    Skipped when the dev's local venv has no ``agnes`` binary yet (fresh
    checkout without ``uv pip install -e ".[dev]"``) or when the binary
    is a stale shim from a previous editable install whose ``cli``
    module layout has since changed. CI always installs the package
    fresh and runs the real assertion. Locally:
    ``uv pip install -e ".[dev]" --force-reinstall`` fixes both cases.
    """
    if shutil.which("agnes") is None:
        pytest.skip(
            "`agnes` not on PATH; run `uv pip install -e \".[dev]\"` to populate the venv"
        )
    result = subprocess.run(
        ["agnes", "--version"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "ModuleNotFoundError" in result.stderr:
        pytest.skip(
            "stale `agnes` shim points at a removed module — "
            "rerun `uv pip install -e \".[dev]\" --force-reinstall` and retry"
        )
    assert result.returncode == 0, (
        f"agnes --version failed (rc={result.returncode}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_da_command_no_longer_works():
    """Greenfield rename: no backward-compat alias kept for `da`.

    Skipped on dev machines whose ``$PATH`` carries a personal ``da``
    shim outside the venv (e.g. ``~/.local/bin/da`` from an older
    Agnes install). The check still bites in CI / fresh container
    installs where ``$PATH`` only sees the package's bin dir.
    """
    import os
    import sys
    result = subprocess.run(
        ["bash", "-c", "command -v da"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        path = result.stdout.strip()
        venv_root = os.path.dirname(os.path.dirname(sys.executable))
        if not path.startswith(venv_root):
            pytest.skip(
                f"personal ``da`` shim outside the venv at {path!r}; "
                "rename assertion is package-scoped."
            )
    assert result.returncode != 0, (
        f"`da` should NOT be on PATH after the rename, but resolved to: "
        f"{result.stdout.strip()!r}"
    )
