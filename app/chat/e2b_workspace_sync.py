"""Per-session workspace ↔ E2B sandbox sync layer.

Q1 (owner-signed): v1 ships the *entire* per-user workspace into the
sandbox at spawn time (rsync-style — every file, every spawn). Cap at
100 MB; refuse upload past the cap rather than half-pushing. Diff-only
mode (option B) is a future optimization.

Transport: the tree is packed into ONE gzipped tarball, written with a
single ``files.write``, and extracted in-sandbox with ``tar`` — one E2B
API round-trip instead of one per file. A workspace with hundreds of
small files (marketplace plugins, ``.claude`` skills) previously paid
one sequential HTTP round-trip *per file*, which dominated chat spawn
latency. The per-file loop is kept as a fallback for sandboxes where the
tar step fails.

Symlink handling: the per-user workspace lives at
``$DATA_DIR/users/<email>/workspace`` and ``WorkdirManager.prepare_session_dir``
mounts a per-session directory whose ``.claude``, ``CLAUDE.md`` etc. are
*symlinks* into the workspace. ``upload_workspace`` follows those
symlinks so the sandbox sees the real file content — the sandbox can't
resolve a host-side symlink target.

E2B SDK 1.x surface used here:
- ``sandbox.files.write(path: str, data: bytes | str)`` — bytes allowed
- ``sandbox.files.list(path) -> list[EntryInfo]`` — entries carry
  ``.name``, ``.type`` (``"FILE"`` | ``"DIR"``), ``.path``
- ``sandbox.files.read(path, format="bytes") -> bytes`` —
  format-defaulted on download
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

from app.chat.e2b_provider import SANDBOX_WORKDIR

logger = logging.getLogger(__name__)


# Directories we never sync into the sandbox. Build/runtime cruft only —
# everything operator-supplied (.claude/*) goes through unchanged.
_EXCLUDE_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".venv",
        ".pytest_cache",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        "build",
        "dist",
        ".eggs",
    }
)

_EXCLUDE_FILE_SUFFIXES = (".pyc", ".pyo")


class WorkspaceTooLarge(Exception):
    """Total file bytes exceeded ``max_bytes``. Surfaced to caller so it
    can emit a user-facing error frame instead of half-syncing."""


def _iter_files(root: Path) -> Iterable[Path]:
    """Yield every regular file under ``root`` (following symlinks),
    skipping excluded build / runtime directories.

    Subdirs and files are visited in sorted order so the cap-breach error
    in ``upload_workspace`` mentions a deterministic running total instead
    of whichever file the filesystem happened to return first (inode order
    on Linux, lexical on macOS — caused a CI flake in
    ``test_workspace_too_large_carries_byte_count``).
    """
    if not root.exists():
        return
    # We can't use rglob alone because we need to prune directory descent
    # for excluded dirs. os.walk lets us prune via the dirs list.
    for current_dir, subdirs, files in os.walk(root, followlinks=True):
        # Prune excluded dirs in-place so os.walk doesn't descend into them.
        # Sort for deterministic descent order (parity with the file sort below).
        subdirs[:] = sorted(d for d in subdirs if d not in _EXCLUDE_DIRS)
        for fname in sorted(files):
            if fname.endswith(_EXCLUDE_FILE_SUFFIXES):
                continue
            yield Path(current_dir) / fname


def _sandbox_path_for(local_path: Path, local_root: Path) -> str:
    """Translate a host filesystem path to the matching sandbox path."""
    rel = local_path.relative_to(local_root)
    # Normalize to forward slashes; sandbox is POSIX regardless of host.
    rel_posix = rel.as_posix()
    return f"{SANDBOX_WORKDIR}/{rel_posix}"


# Sandbox-side staging path for the workspace tarball. Outside /work so a
# failed extraction never leaves the archive in the tree that syncs back to
# the host at session end.
SANDBOX_WORKSPACE_TARBALL = "/tmp/agnes-workspace.tar.gz"

# Sentinel written after the workspace tree is fully in place under /work.
# The runner process starts BEFORE the workspace upload finishes (provider
# .spawn launches it), so it waits on this sentinel before spawning the agent
# CLI — the CLI reads CLAUDE.md / .claude settings from /work at startup, and
# the agent's first tool call reads workspace files. Written even for an
# empty workspace so the runner's bounded wait terminates promptly. Distinct
# from SANDBOX_WHEEL_READY (below), which only gates the CLI wheel install —
# splitting the two lets the in-sandbox pip install run concurrently with
# this (much slower) workspace push.
SANDBOX_WORKSPACE_READY = "/tmp/agnes-workspace.ready"


def _build_workspace_tarball(payloads: list[tuple[str, bytes, int, int]]) -> bytes:
    """Pack ``(rel_posix_path, data, mode, mtime)`` tuples into a gzipped tar.

    ``compresslevel=1``: the bulk of a big workspace is parquet snapshots,
    which are already compressed — cheap gzip keeps CPU out of the spawn
    path while still collapsing the many-small-text-files case.
    """
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=1) as tar:
        for rel_posix, data, mode, mtime in payloads:
            info = tarfile.TarInfo(name=rel_posix)
            info.size = len(data)
            # Preserve the permission bits (hooks/scripts need +x) and mtime.
            info.mode = mode & 0o777
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def upload_workspace(
    sandbox,
    local_root: Path,
    *,
    max_bytes: int,
) -> int:
    """Push ``local_root``'s tree into the sandbox under ``/work/``.

    Preferred transport is a single tarball (one ``files.write`` + one
    in-sandbox ``tar -x``); per-file writes remain as a fallback. Always
    finishes by writing the ``SANDBOX_WORKSPACE_READY`` sentinel so the
    runner's bounded wait unblocks — except on ``WorkspaceTooLarge``,
    where the caller tears the sandbox down anyway.

    Returns the total bytes uploaded. Raises ``WorkspaceTooLarge`` if the
    summed file sizes exceed ``max_bytes`` (counted *before* any upload
    happens, so no partial sync is left in the sandbox).
    """
    files = list(_iter_files(local_root))

    total = 0
    payloads: list[tuple[str, bytes, int, int]] = []
    for f in files:
        try:
            data = f.read_bytes()
            st = f.stat()
        except OSError as e:
            logger.warning("upload_workspace: skip unreadable %s (%s)", f, e)
            continue
        total += len(data)
        if total > max_bytes:
            raise WorkspaceTooLarge(
                f"workspace exceeds cap of {max_bytes} bytes; "
                f"running total {total} bytes; "
                f"raise chat.e2b_workspace_max_bytes or trim files",
            )
        rel_posix = f.relative_to(local_root).as_posix()
        payloads.append((rel_posix, data, st.st_mode, int(st.st_mtime)))

    if payloads:
        try:
            await _upload_via_tarball(sandbox, payloads)
        except Exception:
            logger.exception(
                "upload_workspace: tarball transport failed; falling back to per-file writes",
            )
            for rel_posix, data, _mode, _mtime in payloads:
                await sandbox.files.write(f"{SANDBOX_WORKDIR}/{rel_posix}", data)

    await sandbox.files.write(SANDBOX_WORKSPACE_READY, b"")
    return total


async def _upload_via_tarball(sandbox, payloads: list[tuple[str, bytes, int, int]]) -> None:
    """One-round-trip transport: write the packed tree, extract, remove."""
    blob = _build_workspace_tarball(payloads)
    await sandbox.files.write(SANDBOX_WORKSPACE_TARBALL, blob)
    # Run as the same in-sandbox account the runner uses (see
    # E2BProvider.spawn's ``user="user"``) so extracted files keep an
    # ownership the agent's tools can write through. Foreground run —
    # the SDK raises on a non-zero exit, which the caller turns into the
    # per-file fallback.
    await sandbox.commands.run(
        f"tar -xzf {SANDBOX_WORKSPACE_TARBALL} -C {SANDBOX_WORKDIR} && rm -f {SANDBOX_WORKSPACE_TARBALL}",
        user="user",
        timeout=120,
    )


# Directory the runner pip-installs the agnes CLI wheel from at boot
# (app/chat/runner.py::_install_agnes_cli). Deliberately OUTSIDE
# ``SANDBOX_WORKDIR`` (/work): the workspace under /work is synced back to the
# host at session end (download_workspace), so staging a ~4 MB wheel there
# would bloat every user's workspace and get re-uploaded each spawn. /tmp is
# ephemeral and never synced.
SANDBOX_WHEEL_DIR = "/tmp/agnes-cli"

# Sentinel the runner waits on before installing. The runner process starts
# (inside provider.spawn) BEFORE this upload runs, so without a barrier the
# runner would glob an empty staging dir and skip the install (the race that
# left `agnes` absent). Written right after the wheel — it guarantees the
# wheel ONLY. The manager uploads the wheel FIRST so the in-sandbox pip
# install overlaps with the (slower) workspace push; workspace completeness
# is signalled separately via SANDBOX_WORKSPACE_READY above.
SANDBOX_WHEEL_READY = f"{SANDBOX_WHEEL_DIR}/.ready"


async def upload_agnes_wheel(sandbox) -> str | None:
    """Stage the server's pre-built agnes CLI wheel in the sandbox so the
    runner can ``pip install`` it at boot. Returns the sandbox-side wheel path.

    Always writes the ``.ready`` sentinel last (even when no wheel is found) so
    the runner's bounded wait terminates promptly instead of timing out.

    The wheel is the exact artifact the server already builds at image-build
    time (``uv build --wheel`` → ``/app/dist``) and serves at ``/cli/download``.
    Reusing it — rather than baking the CLI into the template image or pulling
    it from git — guarantees the in-sandbox CLI version matches the running
    server's *exactly*, so the bundled hooks (``agnes admin grant/group/user``)
    and RBAC semantics stay in lockstep. The template bakes the CLI's runtime
    deps, so the runner installs ``--no-deps`` (fast spawn).

    The wheel keeps its original PEP 427 filename
    (``agnes_the_ai_analyst-<ver>-py3-none-any.whl``): ``pip install`` parses
    the filename for name/version and rejects a renamed file
    ("not a valid wheel filename"), so it cannot be flattened to ``agnes.whl``.

    Best-effort: returns ``None`` (and logs a warning) when no wheel is present
    — e.g. a dev image that skipped ``uv build``. The agent still runs; only the
    ``agnes`` verbs (``catalog``, ``query``, ``describe``, ``snapshot``) are
    unavailable.
    """
    # Imported lazily to avoid coupling the chat package to app.api at import
    # time. ``_find_wheel`` is the single source of truth for wheel discovery
    # (it honours AGNES_CLI_DIST_DIR and the /app/dist default).
    from app.api.cli_artifacts import _find_wheel

    wheel = _find_wheel()
    if wheel is None:
        logger.warning(
            "upload_agnes_wheel: no wheel found under %s — the `agnes` CLI "
            "will be absent in the sandbox (dev image without `uv build`?)",
            os.environ.get("AGNES_CLI_DIST_DIR", "/app/dist"),
        )
        # Still signal the runner so it doesn't block on the wait.
        await sandbox.files.write(SANDBOX_WHEEL_READY, b"")
        return None
    dest = f"{SANDBOX_WHEEL_DIR}/{wheel.name}"
    data = wheel.read_bytes()
    await sandbox.files.write(dest, data)
    await sandbox.files.write(SANDBOX_WHEEL_READY, b"")
    logger.info("uploaded agnes wheel %s (%d bytes) to %s", wheel.name, len(data), dest)
    return dest


def _entry_type(e) -> str:
    """Normalize EntryInfo.type — across SDK versions it's been str or enum."""
    t = getattr(e, "type", None)
    if t is None:
        return "FILE"
    s = str(t)
    # FileType.FILE / FileType.DIR style enums end with .FILE or .DIR
    return "DIR" if "DIR" in s.upper() else "FILE"


def _entry_path(e, parent: str) -> str:
    """Resolve the absolute sandbox path for an entry."""
    p = getattr(e, "path", None)
    if p:
        return p
    name = getattr(e, "name", "")
    if parent.endswith("/"):
        return f"{parent}{name}"
    return f"{parent}/{name}"


async def download_workspace(
    sandbox,
    local_root: Path,
    *,
    sandbox_root: str = SANDBOX_WORKDIR,
    skip: bool = False,
) -> int:
    """Walk ``sandbox_root`` and write every file back under ``local_root``.

    Called on session end so persistent edits the runner made inside
    ``/work`` flow back to the per-user workspace on the Agnes host.
    Directory structure is recreated locally; missing intermediate dirs
    are mkdired with ``parents=True``.

    Returns the number of files written.
    """
    if skip:
        return 0  # ephemeral co-session: never persist back (SR-6)
    local_root.mkdir(parents=True, exist_ok=True)
    count = 0

    async def _walk(remote_path: str, local_path: Path) -> None:
        nonlocal count
        entries = await sandbox.files.list(remote_path)
        for e in entries:
            name = getattr(e, "name", "")
            if not name:
                continue
            child_remote = _entry_path(e, remote_path)
            child_local = local_path / name
            etype = _entry_type(e)
            if etype == "DIR":
                child_local.mkdir(parents=True, exist_ok=True)
                await _walk(child_remote, child_local)
            else:
                try:
                    data = await sandbox.files.read(child_remote, format="bytes")
                except TypeError:
                    # Older SDK without format= kwarg
                    data = await sandbox.files.read(child_remote)
                if isinstance(data, str):
                    data = data.encode("utf-8")
                child_local.parent.mkdir(parents=True, exist_ok=True)
                child_local.write_bytes(data)
                count += 1

    await _walk(sandbox_root, local_root)
    return count
