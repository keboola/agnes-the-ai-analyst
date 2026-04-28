"""Snapshot sidecar metadata + file lock helpers (spec §4.2)."""

from __future__ import annotations
import contextlib
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# `fcntl` is POSIX-only. The CLI is primarily targeted at Mac/Linux laptops, but
# import-time failure on Windows would make the whole module (incl. read_meta /
# list_snapshots) unusable. Make the import lazy so non-locking helpers still
# work; `snapshot_lock` raises a clear error if anything tries to acquire it.
try:
    import fcntl as _fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — exercised only on Windows
    _fcntl = None  # type: ignore[assignment]


@dataclass
class SnapshotMeta:
    name: str
    table_id: str
    select: Optional[list[str]]
    where: Optional[str]
    limit: Optional[int]
    order_by: Optional[list[str]]
    fetched_at: str               # ISO 8601 UTC
    effective_as_of: str          # ISO 8601 UTC, server-side eval time
    rows: int
    bytes_local: int
    estimated_scan_bytes_at_fetch: int
    result_hash_md5: str


def _meta_path(snap_dir: Path, name: str) -> Path:
    return snap_dir / f"{name}.meta.json"


def write_meta(snap_dir: Path, meta: SnapshotMeta) -> None:
    snap_dir.mkdir(parents=True, exist_ok=True)
    with _meta_path(snap_dir, meta.name).open("w") as f:
        json.dump(asdict(meta), f, indent=2)


def read_meta(snap_dir: Path, name: str) -> Optional[SnapshotMeta]:
    p = _meta_path(snap_dir, name)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return SnapshotMeta(**data)


def list_snapshots(snap_dir: Path) -> list[SnapshotMeta]:
    if not snap_dir.exists():
        return []
    out = []
    for meta_file in snap_dir.glob("*.meta.json"):
        try:
            data = json.loads(meta_file.read_text())
            out.append(SnapshotMeta(**data))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def delete_snapshot(snap_dir: Path, name: str) -> bool:
    """Delete the snapshot's parquet + meta. Returns True if removed, False if missing."""
    parquet = snap_dir / f"{name}.parquet"
    meta = _meta_path(snap_dir, name)
    removed = False
    if parquet.exists():
        parquet.unlink(); removed = True
    if meta.exists():
        meta.unlink(); removed = True
    return removed


@contextlib.contextmanager
def snapshot_lock(snap_dir: Path):
    """Exclusive flock on snap_dir/.lock — serializes snapshot installs.

    Concurrent `da fetch` invocations queue here.
    """
    if _fcntl is None:
        raise RuntimeError(
            "snapshot_lock requires POSIX fcntl — Windows is not supported. "
            "Run `da` from a Mac or Linux machine, or use a WSL shell."
        )
    snap_dir.mkdir(parents=True, exist_ok=True)
    lock_file = snap_dir / ".lock"
    lock_file.touch(exist_ok=True)
    fd = open(lock_file, "r+")
    try:
        _fcntl.flock(fd.fileno(), _fcntl.LOCK_EX)
        yield
    finally:
        _fcntl.flock(fd.fileno(), _fcntl.LOCK_UN)
        fd.close()
