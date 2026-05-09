"""Bundle metadata — SHA256 + total size of a baked plugin tree.

Computed at upload time (right after ``_bake_plugin_tree``) and persisted
on the submission row. SHA256 survives the TTL purge so admins can still
correlate "this submitter tried the same payload N times" or "this hash
matches a known-bad pattern" after the bundle bytes are gone.

Hashing is content-addressed and order-stable: we sort relative paths
asc and hash ``relpath + NUL + bytes + NUL`` per file. Two bundles with
the same files in different on-disk traversal order produce the same
hash. Empty dirs are ignored (no semantic content).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


_CHUNK = 64 * 1024
_NUL = b"\x00"


@dataclass(frozen=True)
class BundleMeta:
    sha256: str
    file_size: int


def compute_bundle_meta(plugin_dir: Path) -> BundleMeta:
    """Walk plugin_dir, hash + size every file, return aggregate.

    ``file_size`` is the sum of byte sizes of every file in the tree
    (mirrors what ``_bake_plugin_tree`` already returns from
    ``app/api/store.py``, but that helper isn't exposed for re-use and
    doesn't compute a hash). Used by:

    * ``app/api/store.py`` — populate submission row at upload time
    * ``src/store_guardrails/purge.py`` — confirm a bundle existed
      before TTL purge writes ``bundle_purged_at``
    * tests — assert hash stability across re-uploads
    """
    if not plugin_dir.exists() or not plugin_dir.is_dir():
        return BundleMeta(sha256="", file_size=0)

    h = hashlib.sha256()
    total = 0
    files = sorted(
        (p for p in plugin_dir.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(plugin_dir).as_posix(),
    )
    for path in files:
        rel = path.relative_to(plugin_dir).as_posix().encode("utf-8")
        h.update(rel)
        h.update(_NUL)
        try:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    total += len(chunk)
        except OSError:
            # Skip unreadable files but mark in the hash so two bundles
            # that differ only in unreadable-file presence still hash
            # distinctly.
            h.update(b"<unreadable>")
        h.update(_NUL)
    return BundleMeta(sha256=h.hexdigest(), file_size=total)
