"""Cross-platform one-word launcher shortcut installer.

``install_launcher_shortcut`` is called by ``agnes init`` to write a shell
function (bash/zsh on POSIX, PowerShell on Windows) that jumps into the
workspace and launches Claude with ``--permission-mode auto``.

Design decisions
----------------
- Vendor-agnostic: the shortcut word is derived from the workspace folder
  name (alphanumerics only, lowercased — see ``_launcher_word``), never
  hard-coded.
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
- Collision-safe: the launcher word must not shadow a POSIX shell built-in
  or a command the Agnes toolchain itself depends on (``agnes``, ``claude``)
  — a ``function agnes`` would hijack every CLI call into a chat session
  (#783).  Colliding words get an ``ai`` suffix, and a re-run of
  ``agnes init`` removes the stale shadowing block a pre-fix version wrote
  (safe: the block carries our own marker).
- Best-effort: all errors are caught and reported via ``typer.echo(err=True)``
  so a write failure never aborts ``agnes init``.
- Reversible: deleting the marked block from the rc file removes the shortcut.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import typer


# POSIX built-ins / common commands that must not be shadowed by the launcher.
# Sourced from the POSIX spec plus a handful of universally-present utilities.
_SHELL_BUILTINS: frozenset[str] = frozenset(
    {
        "alias",
        "bg",
        "break",
        "builtin",
        "cd",
        "command",
        "continue",
        "echo",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "fc",
        "fg",
        "getopts",
        "hash",
        "jobs",
        "kill",
        "let",
        "local",
        "logout",
        "printf",
        "pwd",
        "read",
        "readonly",
        "return",
        "set",
        "shift",
        "source",
        "test",
        "times",
        "trap",
        "true",
        "type",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "wait",
    }
)

# Commands the Agnes toolchain itself depends on. A launcher function with one
# of these names shadows the real binary once the rc file is sourced — e.g. a
# workspace named "Agnes" produced `function agnes`, hijacking every `agnes`
# CLI call into a Claude chat session (#783); a `function claude` would even
# call itself recursively.
_RESERVED_COMMANDS: frozenset[str] = frozenset(
    {
        "agnes",
        "claude",
    }
)


def _sanitized_word(workspace_name: str) -> str:
    """Workspace folder name stripped to lowercase alphanumerics.

    Mirrors the server's ``get_workspace_dir_name`` sanitization
    (``re.sub(r'[^A-Za-z0-9]', '', ...)``).  This raw word — before any
    collision suffix — is also the name the IWT contract uses for the
    ``bin/<word>`` launcher script.
    """
    return re.sub(r"[^A-Za-z0-9]", "", workspace_name).lower()


def _launcher_word(workspace_name: str) -> str:
    """Derive a shell-safe launcher word from the workspace folder name.

    Sanitizes via ``_sanitized_word`` so a folder name with spaces, dots or
    parentheses can never produce a syntactically invalid ``function <word>``
    block in the user's rc file.  Clean names (e.g. ``FoundryAI`` →
    ``foundryai``) are unaffected, so the ``bin/<word>`` IWT convention still
    resolves.  Returns ``""`` when the name has no alphanumeric characters at
    all (caller skips + warns).

    Appends ``"ai"`` when the sanitized word collides with a POSIX shell
    built-in (workspace ``"Test"`` → ``"testai"``) or with a command the
    toolchain depends on (workspace ``"Agnes"`` → ``"agnesai"``, #783).
    """
    word = _sanitized_word(workspace_name)
    if word in _SHELL_BUILTINS or word in _RESERVED_COMMANDS:
        word = word + "ai"
    return word


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


def _posix_block(word: str, raw_word: str, workspace: Path) -> str:
    """Shell function block for bash/zsh.

    The IWT naming contract seeds ``bin/<raw_word>`` (sanitized folder name,
    no collision suffix), so when the collision guard renamed the function the
    lookup tries both names — the shortcut must keep routing through the
    operator's launcher.  Invoking it by absolute path cannot re-shadow.
    """
    open_marker, close_marker = _markers(word)
    launch_cmd = f'cd "{workspace}" && claude --permission-mode auto "$@"'
    for candidate in dict.fromkeys((word, raw_word)):
        launcher = workspace / "bin" / candidate
        if launcher.exists() and os.access(launcher, os.X_OK):
            launch_cmd = f'"{launcher}" --permission-mode auto "$@"'
            break

    return f"\n{open_marker}\nfunction {word} {{\n  {launch_cmd}\n}}\n{close_marker}\n"


def _windows_block(word: str, raw_word: str, workspace: Path) -> str:
    """PowerShell function block for Windows.

    Same ``bin/<raw_word>`` fallback as ``_posix_block``, with the Windows
    ``.cmd`` / ``.ps1`` launcher variants.
    """
    open_marker, close_marker = _markers(word)
    launch_cmd = f'Set-Location "{workspace}"; claude --permission-mode auto @args'
    bin_dir = workspace / "bin"
    candidates = [
        bin_dir / f"{candidate}{ext}" for candidate in dict.fromkeys((word, raw_word)) for ext in (".cmd", ".ps1")
    ]
    for launcher in candidates:
        if launcher.exists():
            launch_cmd = f'& "{launcher}" --permission-mode auto @args'
            break

    return f"\n{open_marker}\nfunction {word} {{\n  {launch_cmd}\n}}\n{close_marker}\n"


def _ps_profile_paths(home: Path) -> list[Path]:
    """Return the PowerShell profile paths to write on Windows.

    Two editions coexist on Windows and read *different* profile files:

    - **Windows PowerShell 5.x** (the built-in ``powershell.exe``, still the
      default on many machines) → ``Documents/WindowsPowerShell/``
    - **PowerShell 7+** (``pwsh``) → ``Documents/PowerShell/``

    We write the function to both so the shortcut loads regardless of which
    one the user launches.  Derived from ``Path.home()``; does not invoke
    pwsh to avoid a subprocess dependency in tests.
    """
    docs = home / "Documents"
    return [
        docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
        docs / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
    ]


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

    raw_word = _sanitized_word(workspace.name)
    word = _launcher_word(workspace.name)
    if not word:
        typer.echo(
            f"  Warning  : workspace name {workspace.name!r} has no alphanumeric "
            "characters to derive a launcher word from; skipping shortcut.",
            err=True,
        )
        return
    # Prefer the HOME env var explicitly so tests can redirect writes to a
    # tmp directory via monkeypatch.setenv("HOME", ...) on any platform.
    # Fall back to os.path.expanduser("~") for production use.
    _home_env = os.environ.get("HOME")
    home = Path(_home_env) if _home_env else Path(os.path.expanduser("~"))

    try:
        if sys.platform == "win32":
            _install_windows(word, raw_word, workspace, home)
        else:
            _install_posix(word, raw_word, workspace, home)
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


def _defines_function(content: str, word: str) -> bool:
    """True when ``content`` defines a shell function named exactly ``word``.

    Word-boundary match: ``function agnes`` must not fire on
    ``function agnesai`` (or any other name that merely starts with ``word``).
    """
    return re.search(rf"\bfunction {re.escape(word)}\b", content) is not None


def _warn_if_foreign_function(word: str, existing: str) -> None:
    """Warn when a same-named shell function without our marker already exists.

    Covers the pre-FAI-35 manual shortcut: an un-marked ``function <word>``
    the user added by hand from the old homepage step.  We never edit foreign
    lines, so the marked block is appended below it; the later definition wins
    when the shell loads the file, and we tell the user the old line is now a
    harmless leftover they can delete.
    """
    if _defines_function(existing, word):
        typer.echo(
            f"  Note     : found an existing `{word}` shell function without our marker. "
            "The new one takes precedence — you may delete the old line.",
            err=True,
        )


def _strip_marked_block(content: str, word: str) -> str:
    """Remove ``word``'s marked launcher block from ``content``, if present.

    Only ever removes text between our own sentinels (including them), so
    user-authored lines are never touched.  A malformed block (open marker
    without close) is left as-is.
    """
    open_marker, close_marker = _markers(word)
    start = content.find(open_marker)
    if start == -1:
        return content
    end = content.find(close_marker, start)
    if end == -1:
        return content
    end += len(close_marker)
    # Swallow the surrounding newlines the writer added so healing does not
    # accumulate blank lines across re-runs.
    if content[end : end + 1] == "\n":
        end += 1
    if start > 0 and content[start - 1 : start] == "\n":
        start -= 1
    return content[:start] + content[end:]


def _heal_stale_shadowing_block(content: str, word: str, raw_word: str, target_name: str) -> str:
    """Drop the pre-collision-guard block and warn about foreign shadowers.

    A pre-fix ``agnes init`` wrote the launcher under ``raw_word`` (e.g.
    ``function agnes``), shadowing the very command it collides with (#783).
    When the guard renamed the word, remove that stale block — it carries our
    own marker, so this is safe — and tell the user.  An *unmarked* same-named
    function is user content we must not edit; warn that it still shadows.
    """
    if raw_word == word:
        return content
    healed = _strip_marked_block(content, raw_word)
    if healed != content:
        typer.echo(
            f"  Note     : removed the old `{raw_word}` launcher shortcut from "
            f"{target_name} — it shadowed the `{raw_word}` command. "
            f"The shortcut is now `{word}`.",
            err=True,
        )
    if _defines_function(healed, raw_word):
        typer.echo(
            f"  Warning  : your shell config still defines a `{raw_word}` function "
            f"that shadows the `{raw_word}` command. Delete that `function {raw_word}` "
            f"block manually; the Agnes shortcut is `{word}`.",
            err=True,
        )
    return healed


def _install_posix(word: str, raw_word: str, workspace: Path, home: Path) -> None:
    rc = _rc_file(home)
    open_marker, _ = _markers(word)

    existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
    # Heal first: a pre-collision-guard install may have left a block that
    # shadows the `agnes` CLI itself (#783).
    content = _heal_stale_shadowing_block(existing, word, raw_word, rc.name)

    # Idempotency check: the marker embeds the word, so this only
    # short-circuits for *this* workspace — other workspaces still get their
    # own block.
    if open_marker not in content:
        _warn_if_foreign_function(word, content)
        content += _posix_block(word, raw_word, workspace)

    if content != existing:
        rc.parent.mkdir(parents=True, exist_ok=True)
        rc.write_text(content, encoding="utf-8")


def _install_windows(word: str, raw_word: str, workspace: Path, home: Path) -> None:
    open_marker, _ = _markers(word)
    block = _windows_block(word, raw_word, workspace)

    # Write to both the Windows PowerShell 5.x and PowerShell 7+ profiles so
    # the shortcut loads regardless of which edition the user launches.
    for profile in _ps_profile_paths(home):
        existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
        # Heal + idempotency check (per-workspace marker — see _install_posix).
        content = _heal_stale_shadowing_block(existing, word, raw_word, profile.name)
        if open_marker not in content:
            _warn_if_foreign_function(word, content)
            content += block

        if content != existing:
            profile.parent.mkdir(parents=True, exist_ok=True)
            profile.write_text(content, encoding="utf-8")
