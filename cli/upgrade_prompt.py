"""One-time interactive upgrade prompt on version drift (#617).

Builds on :mod:`cli.update_check` (installed-vs-latest + the existing
out-of-date banner) and :mod:`cli.commands.self_upgrade` (the actual
install/smoke-test/rollback flow).

Behaviour, on a remote-touching command where the local CLI is behind the
server's pinned version:

- interactive TTY, not bypassed, no skip-state for the current server
  version → prompt ONCE with a 5s default-``Y`` timeout::

      agnes <local> is <N> versions behind the server (latest: <server>).
      Upgrade now? [Y/n] (5s default Y)

- ``Y`` / 5s timeout → run the self-upgrade flow, then RE-EXEC the user's
  original command (``os.execv`` with the freshly-installed binary +
  original argv) so it runs without a second manual invocation. A re-exec
  loop is guarded by an env sentinel (:data:`_REEXEC_SENTINEL`).
- ``n`` → do NOT upgrade; touch the skip-state file so the prompt does not
  reappear until the server's pinned version moves forward. The existing
  banner still fires on every call.

Safety gates (all → SKIP the prompt; the banner stays as fallback):

- non-TTY (CI, pipes, no stdin) — never default-``Y`` non-interactively,
- ``--no-update-check`` flag or ``AGNES_NO_UPDATE_CHECK=1`` env var,
- already prompted-and-re-exec'd this process (sentinel set),
- a skip-state file already exists for the server's current version.

Best-effort throughout: any failure falls through to the banner; a broken
prompt must never break a working ``agnes`` command.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from cli.config import _config_dir
from cli.update_check import UpdateInfo, is_disabled

# Set on the child process right before re-exec so the freshly-installed
# binary does not prompt again (and can't loop). Distinct from
# self_upgrade's AGNES_SELF_UPGRADE_IN_PROGRESS recursion barrier — that one
# guards the smoke-test `agnes --version`; this one guards the post-upgrade
# re-exec of the user's original command.
_REEXEC_SENTINEL = "AGNES_UPGRADE_PROMPTED"

# Argv tokens that bypass the prompt (mirrors the env var). The root
# callback fires before per-command option parsing, so we inspect argv.
_BYPASS_FLAG = "--no-update-check"

_PROMPT_TIMEOUT_SECONDS = 5.0


def _skip_state_dir() -> Path:
    return _config_dir() / "state"


def skip_state_path(server_version: str) -> Path:
    """``$AGNES_CONFIG_DIR/state/skipped-upgrade-<server-version>``.

    Keyed on the server's pinned version so declining is sticky only until
    the server's version moves forward — a newer pinned version yields a
    different filename and re-arms the prompt.
    """
    return _skip_state_dir() / f"skipped-upgrade-{server_version}"


def skip_state_present(server_version: str) -> bool:
    try:
        return skip_state_path(server_version).exists()
    except OSError:
        return False


def write_skip_state(server_version: str) -> None:
    """Touch the skip-state file. Best-effort — a write failure just means
    the prompt may reappear next time, never a crash."""
    try:
        p = skip_state_path(server_version)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(exist_ok=True)
    except OSError:
        pass


def is_bypassed(argv: Optional[list[str]] = None) -> bool:
    """True if the prompt is suppressed by the env var or the
    ``--no-update-check`` flag. The banner is unaffected by either."""
    if is_disabled():  # AGNES_NO_UPDATE_CHECK=1
        return True
    args = sys.argv[1:] if argv is None else argv
    return _BYPASS_FLAG in args


def reexec_sentinel_set() -> bool:
    return os.environ.get(_REEXEC_SENTINEL) == "1"


def should_prompt_upgrade(
    info: Optional[UpdateInfo],
    *,
    isatty: bool,
    bypassed: bool,
    skip_present: bool,
    sentinel_set: bool,
) -> bool:
    """Pure decision: prompt the user to upgrade now?

    True only when ALL hold: we have update info, the CLI is behind, stdin
    is an interactive TTY, the prompt is not bypassed, no skip-state exists
    for the server version, and we are not a re-exec'd child.
    """
    if info is None or not info.is_outdated():
        return False
    if not isatty:
        return False
    if bypassed:
        return False
    if sentinel_set:
        return False
    if skip_present:
        return False
    return True


def _versions_behind(installed: str, latest: str) -> int:
    """Best-effort count of releases between installed and latest.

    Uses the integer delta of the leading minor/patch component when both
    parse as simple dotted ints sharing a major; otherwise returns 1 (we
    only need a non-zero, human-meaningful 'N versions behind' — the exact
    number is cosmetic, never gates behaviour)."""
    try:
        a = tuple(int(x) for x in installed.split("."))
        b = tuple(int(x) for x in latest.split("."))
    except ValueError:
        return 1
    # Compare on the last differing component for a rough release count.
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    # Sum positive per-component deltas, weighted trivially — good enough
    # for "N versions behind" copy. Floor at 1.
    delta = 0
    for x, y in zip(a, b):
        if y > x:
            delta += y - x
    return delta if delta > 0 else 1


def format_prompt(info: UpdateInfo) -> str:
    n = _versions_behind(info.installed, info.latest or info.installed)
    return (
        f"agnes {info.installed} is {n} versions behind the server "
        f"(latest: {info.latest}).\n"
        f"Upgrade now? [Y/n] (5s default Y)"
    )


def _read_yn_with_timeout(timeout: float) -> bool:
    """Read a single Y/n answer from stdin with a default-``Y`` timeout.

    Returns True for accept (``y``/empty/timeout), False for decline
    (``n``). Uses ``select`` on the stdin fd; on platforms/streams where
    select isn't available (Windows, non-selectable streams) we fall back
    to a plain blocking ``input()`` with no timeout — the TTY gate upstream
    guarantees stdin is interactive, so blocking is acceptable there."""
    sys.stderr.write("")  # ensure prompt already flushed by caller
    try:
        import select

        fd = sys.stdin.fileno()
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            # Timeout → default Y.
            sys.stderr.write("\n[no input in 5s — defaulting to Yes]\n")
            return True
        line = sys.stdin.readline()
        if line == "":
            # EOF (Ctrl+D / closed stdin) — distinguishable from an empty
            # input line: `readline()` returns `""` for EOF and `"\n"` for
            # bare Enter. Don't silently auto-upgrade on EOF (could be a
            # SIGTERM'd shell, a piped non-interactive `echo` finishing,
            # or a deliberate Ctrl+D dismissal). Defer to a later run
            # by treating it as a No. Devin Review ANALYSIS_0003 on #619.
            return False
    except (OSError, ValueError, ImportError):
        # No selectable fd — fall back to a blocking read.
        try:
            line = sys.stdin.readline()
            if line == "":
                # EOF on the fallback path too — same rationale as above.
                return False
        except (OSError, ValueError):
            return True  # can't read → default Y, matching timeout semantics
    answer = (line or "").strip().lower()
    if answer in ("n", "no"):
        return False
    return True  # y / yes / empty (Enter) → accept


def _reexec_with_new_binary() -> None:
    """Re-exec the user's original command against the freshly-installed
    binary. Sets the re-exec sentinel so the child never re-prompts (and
    can't loop). On any failure we return (caller prints a fallback hint)
    rather than raising — a failed re-exec must not crash the CLI."""
    from cli.commands.self_upgrade import _pip_bin_path, _uv_tool_bin_path

    binary = _uv_tool_bin_path() or _pip_bin_path()
    if binary is None:
        return
    os.environ[_REEXEC_SENTINEL] = "1"
    argv = [str(binary)] + sys.argv[1:]
    os.execv(str(binary), argv)  # replaces the process; never returns on success


def maybe_prompt_and_upgrade(info: Optional[UpdateInfo]) -> bool:
    """Entry point from the root callback. Returns True if it handled the
    drift interactively (prompted) — in which case the caller should NOT
    also emit the banner. Returns False to fall through to the banner
    (declined, skipped, gated, or non-applicable).

    Never raises — every failure path returns False so the banner fires.
    """
    try:
        server_version = info.latest if info else None
        decision = should_prompt_upgrade(
            info,
            isatty=_stdin_isatty(),
            bypassed=is_bypassed(),
            skip_present=(
                skip_state_present(server_version) if server_version else False
            ),
            sentinel_set=reexec_sentinel_set(),
        )
        if not decision:
            return False

        assert info is not None and server_version is not None  # narrowed above
        import typer

        typer.echo(format_prompt(info), err=True)
        accept = _read_yn_with_timeout(_PROMPT_TIMEOUT_SECONDS)

        if not accept:
            # Decline: remember the choice for this server version so the
            # prompt does not reappear until the pinned version moves
            # forward. Banner still fires (return False).
            write_skip_state(server_version)
            return False

        # Accept (or 5s timeout): run the self-upgrade flow, then re-exec.
        # Honor the install outcome — if it failed (pip error, smoke-test
        # rollback), don't print the "[upgraded → …]" line and don't try
        # to re-exec the (still-old) binary. The user already saw the
        # install error on stderr; falling through lets their original
        # command proceed unchanged. Devin Review BUG_0001 on #619.
        if not _run_self_upgrade():
            return False
        typer.echo(
            f"[upgraded → {server_version}] running your original command...",
            err=True,
        )
        _reexec_with_new_binary()
        # If re-exec failed to replace the process, fall through with a hint
        # and let the (now-stale) command continue rather than dying.
        typer.echo(
            "agnes: upgrade installed; re-run your command to use the new version.",
            err=True,
        )
        return True
    except Exception:
        return False


def _stdin_isatty() -> bool:
    try:
        return sys.stdin.isatty()
    except (OSError, ValueError, AttributeError):
        return False


def _run_self_upgrade() -> bool:
    """Invoke the existing self-upgrade flow in-process (quiet=False so the
    user sees install progress). Returns True on a clean install (caller
    proceeds to re-exec), False otherwise (caller surfaces the failure to
    the user and does NOT pretend the upgrade succeeded).

    Mirrors the post-install wiring of the ``self_upgrade`` CLI callback:
    persists the install outcome via ``record_outcome`` so the #478
    consecutive-failure warning still fires from this entry point, and
    runs ``_try_refresh_hooks`` on success so any wire-format change in
    the new release lands on the next session-start. Isolated so tests
    can mock it. Devin Review BUG_0001 + ANALYSIS_0001 on #619.
    """
    from cli.commands import self_upgrade as su
    from cli.upgrade_status import record_outcome

    info = su._resolve_info(force=False)
    if not isinstance(info, UpdateInfo):
        # No install needed (CLI current, offline, or unreachable without
        # --force) — `_resolve_info` already handled those branches. The
        # interactive prompt only reached here because we have a real
        # UpdateInfo, so anything else is a regression.
        return False

    rc = su._do_install_with_smoke_and_rollback(info, quiet=False)
    record_outcome(success=(rc == 0))
    if rc == 0:
        su._try_refresh_hooks(quiet=False)
        return True
    return False
