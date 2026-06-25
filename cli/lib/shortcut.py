"""Cross-platform one-word launcher shortcut installer.

``install_launcher_shortcut`` is called by ``agnes init`` to write a shell
function (bash/zsh on POSIX, PowerShell on Windows) that jumps into the
workspace and launches Claude with ``--permission-mode auto``.

Design decisions
----------------
- Vendor-agnostic: the shortcut word is derived from the workspace folder
  name (``workspace.name.lower()``), never hard-coded.
- IWT convention: when ``<workspace>/bin/<word>`` (POSIX) or
  ``<workspace>/bin/<word>.cmd`` / ``<word>.ps1`` (Windows) exists and is
  executable, the shortcut routes through it (adds ``--permission-mode auto``
  on top) so the operator's welcome skill fires correctly.  When absent, it
  falls back to ``cd <workspace> && claude --permission-mode auto``.
- Idempotent: a per-workspace guard marker
  (``# >>> agnes launcher: <word> <<<``) is checked by reading the rc file —
  not via ``grep`` (unavailable on Windows).  The marker embeds the launcher
  word so several workspaces on one machine each get their own block and a
  re-run of ``agnes init`` in any of them never duplicates.
- Best-effort: all errors are caught and reported via ``typer.echo(err=True)``
  so a write failure never aborts ``agnes init``.
- Reversible: deleting the marked block from the rc file removes the shortcut.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer


# The open/close sentinels embed the workspace word so each workspace gets its
# own idempotent block.
def _markers(word: str) -> tuple[str, str]:
    """Return the (open, close) sentinel comments for ``word``'s block."""
    return (
        f"# >>> agnes launcher: {word} <<<",
        f"# <<< agnes launcher: {word} >>>",
    )


def _rc_file(home: Path) -> Path:
    """Return the shell rc file to append to on POSIX.

    Preference order:
    1. SHELL env contains 'zsh' → ~/.zshrc
    2. macOS (darwin) → ~/.zshrc  (zsh is the default since Catalina)
    3. otherwise → ~/.bashrc
    """
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell or sys.platform == "darwin":
        return home / ".zshrc"
    return home / ".bashrc"


def _posix_block(word: str, workspace: Path) -> str:
    """Shell function block for bash/zsh."""
    open_marker, close_marker = _markers(word)
    # Check for a conventional IWT launcher: <workspace>/bin/<word>
    launcher = workspace / "bin" / word
    if launcher.exists() and os.access(launcher, os.X_OK):
        launch_cmd = f'"{launcher}" --permission-mode auto "$@"'
    else:
        launch_cmd = f'cd "{workspace}" && claude --permission-mode auto "$@"'

    return f"\n{open_marker}\nfunction {word} {{\n  {launch_cmd}\n}}\n{close_marker}\n"


def _windows_block(word: str, workspace: Path) -> str:
    """PowerShell function block for Windows."""
    open_marker, close_marker = _markers(word)
    # Check for a conventional IWT launcher: <workspace>/bin/<word>.cmd or .ps1
    bin_dir = workspace / "bin"
    launcher_cmd = bin_dir / f"{word}.cmd"
    launcher_ps1 = bin_dir / f"{word}.ps1"
    if launcher_cmd.exists():
        launch_cmd = f'& "{launcher_cmd}" --permission-mode auto @args'
    elif launcher_ps1.exists():
        launch_cmd = f'& "{launcher_ps1}" --permission-mode auto @args'
    else:
        launch_cmd = f'Set-Location "{workspace}"; claude --permission-mode auto @args'

    return f"\n{open_marker}\nfunction {word} {{\n  {launch_cmd}\n}}\n{close_marker}\n"


def _ps_profile_path(home: Path) -> Path:
    """Return the PowerShell profile path (Windows).

    Tries the standard Documents/PowerShell/Microsoft.PowerShell_profile.ps1
    location derived from Path.home().  Does not invoke pwsh to avoid a
    subprocess dependency in tests.
    """
    return home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"


def install_launcher_shortcut(workspace: Path, *, no_shortcut: bool = False) -> None:
    """Write a one-word launcher shortcut to the user's shell config.

    Parameters
    ----------
    workspace:
        The fully-resolved workspace directory (``Path``).
    no_shortcut:
        When ``True`` the function returns immediately without writing
        anything (honours the ``--no-shortcut`` flag on ``agnes init``).
    """
    if no_shortcut:
        return

    word = workspace.name.lower()
    # Prefer the HOME env var explicitly so tests can redirect writes to a
    # tmp directory via monkeypatch.setenv("HOME", ...) on any platform.
    # Fall back to os.path.expanduser("~") for production use.
    _home_env = os.environ.get("HOME")
    home = Path(_home_env) if _home_env else Path(os.path.expanduser("~"))

    try:
        if sys.platform == "win32":
            _install_windows(word, workspace, home)
        else:
            _install_posix(word, workspace, home)
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"  Warning  : could not install launcher shortcut ({exc}). Add it manually: see `agnes init --help`.",
            err=True,
        )
        return

    typer.echo(f"  Shortcut : type `{word}` from any terminal to launch")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _warn_if_foreign_function(word: str, existing: str) -> None:
    """Warn when a same-named shell function without our marker already exists.

    Covers the pre-FAI-35 manual shortcut: an un-marked ``function <word>``
    the user added by hand from the old homepage step.  We never edit foreign
    lines, so the marked block is appended below it; the later definition wins
    when the shell loads the file, and we tell the user the old line is now a
    harmless leftover they can delete.
    """
    if f"function {word}" in existing:
        typer.echo(
            f"  Note     : found an existing `{word}` shell function without our marker. "
            "The new one takes precedence — you may delete the old line.",
            err=True,
        )


def _install_posix(word: str, workspace: Path, home: Path) -> None:
    rc = _rc_file(home)
    open_marker, _ = _markers(word)
    block = _posix_block(word, workspace)

    # Idempotency check: read existing content. The marker embeds the word, so
    # this only short-circuits for *this* workspace — other workspaces still
    # get their own block.
    if rc.exists():
        existing = rc.read_text(encoding="utf-8")
        if open_marker in existing:
            return  # This workspace's shortcut already installed.
        _warn_if_foreign_function(word, existing)

    # Append the block.
    rc.parent.mkdir(parents=True, exist_ok=True)
    with rc.open("a", encoding="utf-8") as fh:
        fh.write(block)


def _install_windows(word: str, workspace: Path, home: Path) -> None:
    profile = _ps_profile_path(home)
    open_marker, _ = _markers(word)
    block = _windows_block(word, workspace)

    # Idempotency check (per-workspace marker — see _install_posix).
    if profile.exists():
        existing = profile.read_text(encoding="utf-8")
        if open_marker in existing:
            return
        _warn_if_foreign_function(word, existing)

    # Ensure profile directory exists.
    profile.parent.mkdir(parents=True, exist_ok=True)
    with profile.open("a", encoding="utf-8") as fh:
        fh.write(block)
