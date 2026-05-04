"""Confirm the wheel installs the binary as `agnes`, not `da`."""

import subprocess


def test_agnes_command_exists():
    """`agnes --version` must succeed once the package is editable-installed."""
    result = subprocess.run(
        ["agnes", "--version"],
        capture_output=True,
        text=True,
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
