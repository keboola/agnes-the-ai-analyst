"""Per-session workspace ↔ E2B sandbox sync layer.

Q1 (owner-signed): v1 ships the *entire* per-user workspace into the
sandbox at spawn time (rsync-style — every file, every spawn). Cap at
100 MB; refuse upload past the cap rather than half-pushing. Diff-only
mode (option B) is a future optimization.

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
_EXCLUDE_DIRS = frozenset({
    "__pycache__", ".git", ".venv", ".pytest_cache", "node_modules",
    ".mypy_cache", ".ruff_cache", "build", "dist", ".eggs",
})

_EXCLUDE_FILE_SUFFIXES = (".pyc", ".pyo")


class WorkspaceTooLarge(Exception):
    """Total file bytes exceeded ``max_bytes``. Surfaced to caller so it
    can emit a user-facing error frame instead of half-syncing."""


def _iter_files(root: Path) -> Iterable[Path]:
    """Yield every regular file under ``root`` (following symlinks),
    skipping excluded build / runtime directories."""
    if not root.exists():
        return
    # We can't use rglob alone because we need to prune directory descent
    # for excluded dirs. os.walk lets us prune via the dirs list.
    for current_dir, subdirs, files in os.walk(root, followlinks=True):
        # Prune excluded dirs in-place so os.walk doesn't descend into them.
        subdirs[:] = [d for d in subdirs if d not in _EXCLUDE_DIRS]
        for fname in files:
            if fname.endswith(_EXCLUDE_FILE_SUFFIXES):
                continue
            yield Path(current_dir) / fname


def _sandbox_path_for(local_path: Path, local_root: Path) -> str:
    """Translate a host filesystem path to the matching sandbox path."""
    rel = local_path.relative_to(local_root)
    # Normalize to forward slashes; sandbox is POSIX regardless of host.
    rel_posix = rel.as_posix()
    return f"{SANDBOX_WORKDIR}/{rel_posix}"


async def upload_workspace(
    sandbox,
    local_root: Path,
    *,
    max_bytes: int,
) -> int:
    """Push ``local_root``'s tree into the sandbox under ``/work/``.

    Returns the total bytes uploaded. Raises ``WorkspaceTooLarge`` if the
    summed file sizes exceed ``max_bytes`` (counted *before* any upload
    happens, so no partial sync is left in the sandbox).
    """
    files = list(_iter_files(local_root))
    if not files:
        return 0

    total = 0
    payloads: list[tuple[str, bytes]] = []
    for f in files:
        try:
            data = f.read_bytes()
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
        payloads.append((_sandbox_path_for(f, local_root), data))

    for sandbox_path, data in payloads:
        await sandbox.files.write(sandbox_path, data)
    return total


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
) -> int:
    """Walk ``sandbox_root`` and write every file back under ``local_root``.

    Called on session end so persistent edits the runner made inside
    ``/work`` flow back to the per-user workspace on the Agnes host.
    Directory structure is recreated locally; missing intermediate dirs
    are mkdired with ``parents=True``.

    Returns the number of files written.
    """
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
