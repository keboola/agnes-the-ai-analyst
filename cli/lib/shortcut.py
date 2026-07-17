"""Cross-platform one-word launcher script installer.

``install_launcher_shortcut`` is called by ``agnes init`` to install an
executable launcher script named after the workspace into ``~/.local/bin`` —
the same directory ``uv tool install`` puts the ``agnes`` binary itself, so
anyone who can run ``agnes`` already has it on PATH.  The script is
``<word>`` (POSIX ``#!/bin/sh``) or ``<word>.cmd`` (Windows cmd shim) and
jumps into the workspace before launching Claude with
``--permission-mode auto``.

Earlier versions wrote a shell *function* into the user's rc file
(``~/.zshrc`` / ``~/.bashrc`` / PowerShell ``$PROFILE``).  That mutated
personal dotfiles, was invisible to ``which`` and non-interactive shells,
and on Windows silently failed under the default
``ExecutionPolicy Restricted`` (profiles are scripts and don't load).
``install_launcher_shortcut`` therefore also *removes* those legacy marked
rc blocks, and ``migrate_launcher_shortcut`` (called from ``agnes update``)
converges existing installs without re-running ``agnes init``.

Design decisions
----------------
- Vendor-agnostic: the launcher word is derived from the workspace folder
  name (alphanumerics only, lowercased — see ``_launcher_word``), never
  hard-coded.
- IWT convention: when ``<workspace>/bin/<word>`` (POSIX) or
  ``<workspace>/bin/<word>.cmd`` / ``<word>.ps1`` (Windows) exists and is
  executable, the script routes through it (adds ``--permission-mode auto``
  on top) so the operator's welcome skill fires correctly.  When absent, it
  falls back to ``cd <workspace> && exec claude --permission-mode auto``.
- Ownership marker: every script we write carries a
  ``>>> agnes launcher: <word> <<<`` comment line.  Only files carrying the
  marker are ever overwritten; a same-named user file is left untouched.
- Collision-safe: the word must not shadow a POSIX shell built-in, a
  command the toolchain depends on (``agnes``, ``claude`` — #783), an
  existing foreign file in the bin dir, or any other executable already on
  PATH.  Colliding words get an ``ai`` suffix; if that collides too, the
  shortcut is skipped with a warning.
- Opt-out preserved: ``migrate_launcher_shortcut`` acts only on evidence of
  a previous install (legacy marked rc block or our script), so a user who
  ran ``agnes init --no-shortcut`` stays untouched.
- Best-effort: all errors are caught and reported via
  ``typer.echo(err=True)`` so a write failure never aborts ``agnes init``
  or ``agnes update``.
- Reversible: deleting the script removes the shortcut; legacy rc blocks
  are removed automatically.
"""

from __future__ import annotations

import os
import re
import shutil
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

# Commands the Agnes toolchain itself depends on. A launcher with one of these
# names shadows the real binary — e.g. a workspace named "Agnes" produced a
# `function agnes`, hijacking every `agnes` CLI call into a Claude chat
# session (#783); a `claude` launcher would even call itself recursively.
_RESERVED_COMMANDS: frozenset[str] = frozenset(
    {
        "agnes",
        "claude",
    }
)

_OWNERSHIP_TOKEN = ">>> agnes launcher:"


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
    parentheses can never produce an invalid script name.  Clean names
    (e.g. ``FoundryAI`` → ``foundryai``) are unaffected, so the
    ``bin/<word>`` IWT convention still resolves.  Returns ``""`` when the
    name has no alphanumeric characters at all (caller skips + warns).

    Appends ``"ai"`` when the sanitized word collides with a POSIX shell
    built-in (workspace ``"Test"`` → ``"testai"``) or with a command the
    toolchain depends on (workspace ``"Agnes"`` → ``"agnesai"``, #783).
    """
    word = _sanitized_word(workspace_name)
    if word in _SHELL_BUILTINS or word in _RESERVED_COMMANDS:
        word = word + "ai"
    return word


# The open/close sentinels embed the workspace word so each legacy rc block
# (and each script) is identifiable per workspace.
def _markers(word: str) -> tuple[str, str]:
    """Return the (open, close) sentinel comments for ``word``'s block."""
    return (
        f"# >>> agnes launcher: {word} <<<",
        f"# <<< agnes launcher: {word} >>>",
    )


def _bin_dir(home: Path) -> Path:
    """Launcher install dir — ``~/.local/bin`` on every platform.

    ``uv tool install`` places the ``agnes`` binary there (POSIX and
    Windows alike), so PATH is already handled for anyone who can run
    ``agnes`` at all.
    """
    return home / ".local" / "bin"


def _script_ext() -> str:
    return ".cmd" if sys.platform == "win32" else ""


def _script_is_ours(path: Path) -> bool:
    """True when ``path`` carries the ownership marker.

    Only such files may be overwritten. Unreadable/binary files are treated
    as foreign.
    """
    try:
        return _OWNERSHIP_TOKEN in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _posix_script(word: str, raw_word: str, workspace: Path) -> str:
    """POSIX launcher script body.

    Routes through the IWT ``bin/<raw_word>`` launcher when present (the
    lookup tries both the collision-suffixed and the raw name — the IWT
    seeds ``bin/<raw_word>``); otherwise cd + exec claude.  ``cd`` happens
    in the script's own process, so the calling terminal's cwd is never
    touched, and ``exec`` keeps the process tree flat.
    """
    open_marker, _ = _markers(word)
    launch_cmd = f'cd "{workspace}" && exec claude --permission-mode auto "$@"'
    for candidate in dict.fromkeys((word, raw_word)):
        launcher = workspace / "bin" / candidate
        if launcher.exists() and os.access(launcher, os.X_OK):
            launch_cmd = f'exec "{launcher}" --permission-mode auto "$@"'
            break
    return (
        "#!/bin/sh\n"
        f"# {open_marker}\n"
        f"# Launcher for the {workspace.name} workspace. Managed by `agnes init`;\n"
        "# safe to delete together with the workspace.\n"
        f"{launch_cmd}\n"
    )


def _windows_script(word: str, raw_word: str, workspace: Path) -> str:
    """Windows ``.cmd`` shim body.

    Works from cmd.exe AND PowerShell and — unlike the old ``$PROFILE``
    function — is immune to the default ``ExecutionPolicy Restricted``,
    which silently blocks profile loading.  Same ``bin/<raw_word>``
    fallback as ``_posix_script``, with the ``.cmd`` / ``.ps1`` variants.
    """
    open_marker, _ = _markers(word)
    launch_cmd = f'cd /d "{workspace}"\r\nclaude --permission-mode auto %*'
    bin_dir = workspace / "bin"
    candidates = [
        bin_dir / f"{candidate}{ext}" for candidate in dict.fromkeys((word, raw_word)) for ext in (".cmd", ".ps1")
    ]
    for launcher in candidates:
        if launcher.exists():
            if launcher.suffix == ".ps1":
                launch_cmd = f'powershell -ExecutionPolicy Bypass -File "{launcher}" --permission-mode auto %*'
            else:
                launch_cmd = f'call "{launcher}" --permission-mode auto %*'
            break
    # `_markers` returns shell-comment form ("# >>> ..."); strip the leading
    # "# " so the .cmd file carries a clean `rem >>> ... <<<` line.
    return f"@echo off\r\nrem {open_marker.removeprefix('# ')}\r\n{launch_cmd}\r\n"


def _choose_target(workspace_name: str, bin_dir: Path) -> tuple[str, Path] | tuple[None, None]:
    """Pick a non-colliding script name and its target path.

    Collision ladder: the builtin/toolchain guard in ``_launcher_word``
    first, then — per candidate — a foreign file already occupying the
    target, or ``shutil.which`` resolving the word to a foreign executable
    elsewhere on PATH (a workspace named ``Node`` must not shadow ``node``).
    One ``ai``-suffix retry; both colliding ⇒ ``(None, None)``.
    """
    base = _launcher_word(workspace_name)
    if not base:
        return None, None
    ext = _script_ext()
    for candidate in dict.fromkeys((base, base + "ai")):
        target = bin_dir / f"{candidate}{ext}"
        if target.exists() and not _script_is_ours(target):
            continue
        resolved = shutil.which(candidate)
        if resolved is not None:
            resolved_path = Path(resolved)
            if resolved_path != target and not _script_is_ours(resolved_path):
                continue
        return candidate, target
    return None, None


def _ps_profile_paths(home: Path) -> list[Path]:
    """Return the PowerShell profile paths a legacy install may have written.

    Two editions coexist on Windows and read *different* profile files:

    - **Windows PowerShell 5.x** (the built-in ``powershell.exe``, still the
      default on many machines) → ``Documents/WindowsPowerShell/``
    - **PowerShell 7+** (``pwsh``) → ``Documents/PowerShell/``

    Derived from ``Path.home()``-style layout; does not invoke pwsh to
    avoid a subprocess dependency in tests.
    """
    docs = home / "Documents"
    return [
        docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
        docs / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
    ]


def _rc_paths(home: Path) -> list[Path]:
    """Files a legacy (rc-function) install may have written a block to."""
    if sys.platform == "win32":
        return _ps_profile_paths(home)
    return [home / ".zshrc", home / ".bashrc", home / ".bash_profile"]


def _cleanup_words(workspace_name: str, chosen: str | None) -> set[str]:
    """Every word a legacy block may have been written under.

    Covers the pre-#783 raw word (``function agnes``), the guarded word,
    its suffixed form, and the freshly chosen word.
    """
    raw = _sanitized_word(workspace_name)
    base = _launcher_word(workspace_name)
    words = {raw, base, base + "ai" if base else ""}
    if chosen:
        words.add(chosen)
    return {w for w in words if w}


def _defines_function(content: str, word: str) -> bool:
    """True when ``content`` defines a shell function named exactly ``word``.

    Word-boundary match: ``function agnes`` must not fire on
    ``function agnesai`` (or any other name that merely starts with ``word``).
    """
    return re.search(rf"\bfunction {re.escape(word)}\b", content) is not None


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
    # Swallow the surrounding newlines the writer added so cleanup does not
    # accumulate blank lines across re-runs.
    if content[end : end + 1] == "\n":
        end += 1
    if start > 0 and content[start - 1 : start] == "\n":
        start -= 1
    return content[:start] + content[end:]


def _cleanup_legacy_rc_blocks(home: Path, words: set[str]) -> list[Path]:
    """Strip our marked launcher blocks from shell rc / PowerShell profiles.

    Returns the files that were modified.  Only sentinel-delimited text is
    removed (see ``_strip_marked_block``); user lines are never touched.
    """
    cleaned: list[Path] = []
    for rc in _rc_paths(home):
        if not rc.exists():
            continue
        try:
            content = rc.read_text(encoding="utf-8")
        except OSError:
            continue
        stripped = content
        for word in words:
            stripped = _strip_marked_block(stripped, word)
        if stripped != content:
            rc.write_text(stripped, encoding="utf-8")
            cleaned.append(rc)
    return cleaned


def _warn_if_shadowing_function(home: Path, word: str) -> None:
    """Warn when an rc file defines an unmarked ``function <word>``.

    Such a function takes precedence over the PATH script in interactive
    shells.  It is user content — warn, never edit.
    """
    if sys.platform == "win32":
        return
    for rc in _rc_paths(home):
        if not rc.exists():
            continue
        try:
            content = rc.read_text(encoding="utf-8")
        except OSError:
            continue
        if _defines_function(content, word):
            typer.echo(
                f"  Warning  : {rc.name} defines a `{word}` shell function that will "
                f"shadow the new launcher script in interactive shells. Delete that "
                f"`function {word}` block to use the script.",
                err=True,
            )


def _home() -> Path:
    # Prefer the HOME env var explicitly so tests can redirect writes to a
    # tmp directory via monkeypatch.setenv("HOME", ...) on any platform.
    # Fall back to os.path.expanduser("~") for production use.
    home_env = os.environ.get("HOME")
    return Path(home_env) if home_env else Path(os.path.expanduser("~"))


def install_launcher_shortcut(workspace: Path, *, no_shortcut: bool = False, quiet: bool = False) -> None:
    """Install the one-word launcher script into ``~/.local/bin`` and remove
    any legacy rc-function blocks a previous version wrote.

    Parameters
    ----------
    workspace:
        The fully-resolved workspace directory (``Path``).
    no_shortcut:
        When ``True`` the function returns immediately without writing
        anything (honours the ``--no-shortcut`` flag on ``agnes init``).
    quiet:
        Suppress the stdout summary line (used by ``agnes update``, whose
        ``--quiet``/``--json`` contracts require a clean stdout).
    """
    if no_shortcut:
        return

    if not _sanitized_word(workspace.name):
        typer.echo(
            f"  Warning  : workspace name {workspace.name!r} has no alphanumeric "
            "characters to derive a launcher word from; skipping shortcut.",
            err=True,
        )
        return

    home = _home()
    chosen: str | None = None
    try:
        bin_dir = _bin_dir(home)
        chosen, target = _choose_target(workspace.name, bin_dir)

        cleaned = _cleanup_legacy_rc_blocks(home, _cleanup_words(workspace.name, chosen))
        for rc in cleaned:
            typer.echo(
                f"  Note     : removed the legacy launcher shell function from {rc.name} "
                "— the launcher is now a script on PATH.",
                err=True,
            )

        if chosen is None or target is None:
            typer.echo(
                "  Warning  : could not pick a launcher name that does not collide "
                "with an existing command; skipping shortcut.",
                err=True,
            )
            return

        raw_word = _sanitized_word(workspace.name)
        if sys.platform == "win32":
            script = _windows_script(chosen, raw_word, workspace)
        else:
            script = _posix_script(chosen, raw_word, workspace)
        bin_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(script, encoding="utf-8")
        if sys.platform != "win32":
            target.chmod(0o755)

        _warn_if_shadowing_function(home, chosen)
        if shutil.which(chosen) is None:
            typer.echo(
                f"  Note     : {bin_dir} is not on PATH in this shell — add it "
                f'(export PATH="$HOME/.local/bin:$PATH") to use `{chosen}`.',
                err=True,
            )
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"  Warning  : could not install launcher shortcut ({exc}). Add it manually: see `agnes init --help`.",
            err=True,
        )
        return

    if not quiet:
        typer.echo(f"  Shortcut : type `{chosen}` from any terminal to launch")


def migrate_launcher_shortcut(workspace: Path, *, quiet: bool = True) -> str:
    """Converge an existing launcher install to the script form.

    Used by ``agnes update``.  Acts only on evidence of a previous install —
    a legacy marked rc block or an ours-marked script — so
    ``agnes init --no-shortcut`` users stay untouched.

    Returns ``"absent"`` (no evidence, nothing done), ``"migrated"``
    (legacy rc block found; script installed, block removed) or
    ``"converged"`` (script already present, re-asserted).
    """
    home = _home()
    words = _cleanup_words(workspace.name, None)
    if not words:
        return "absent"

    had_legacy = False
    for rc in _rc_paths(home):
        if not rc.exists():
            continue
        try:
            content = rc.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(_markers(w)[0] in content for w in words):
            had_legacy = True
            break

    ext = _script_ext()
    has_script = any((candidate := _bin_dir(home) / f"{w}{ext}").exists() and _script_is_ours(candidate) for w in words)

    if not had_legacy and not has_script:
        return "absent"

    install_launcher_shortcut(workspace, quiet=quiet)
    return "migrated" if had_legacy else "converged"
