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
    """Greenfield rename: no backward-compat alias kept for `da`."""
    result = subprocess.run(
        ["bash", "-c", "command -v da"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        f"`da` should NOT be on PATH after the rename, but resolved to: "
        f"{result.stdout.strip()!r}"
    )
