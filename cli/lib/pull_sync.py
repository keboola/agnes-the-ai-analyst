"""Per-type sync engine for the v49 unified stack (Phase 7, Task 7.5).

Implements the sync semantics from Section 5 of the unified-stack design:

  - Per-type loop over ``data_packages`` / ``memory_domains`` /
    ``direct_tables`` parsed from the extended ``/api/sync/manifest``.
  - Reference-counted parquet store at ``<local_dir>/data/_shared/`` with
    symlink-style "references" under per-package and ``_direct/`` dirs.
  - Windows-friendly fallback hierarchy: ``os.symlink`` → ``os.link``
    (hardlink) → ``shutil.copy2``. Per-file ``strategy`` is recorded in
    ``sync_state.json`` so the reverse delete uses the right unlink.
  - Memory bundles materialized as ``<local_dir>/memory/<slug>/bundle.md``
    via the per-domain ``/api/memory/bundle?domain=<slug>`` endpoint.
  - Invariant audit + auto-heal at the end of every pull.

This module is intentionally decoupled from the legacy
``cli/lib/pull.py:run_pull`` flow (which still writes the
``server/parquet/`` workspace tree consumed by older readers). The new
per-type sync targets ``<local_dir>/data/`` and is invoked from
``run_pull`` after the legacy flow completes.

Layout::

    <local_dir>/
    ├── data/
    │   ├── _shared/<table_id>.parquet         # canonical, ref-counted
    │   ├── _direct/<table_name>.parquet       # → _shared/<id>.parquet
    │   ├── <package_slug>/<table_name>.parquet
    │   └── …
    └── memory/
        └── <domain_slug>/bundle.md
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Reserved subdirs of ``<local_dir>/data/`` that are NOT package slugs.
_SHARED_DIRNAME = "_shared"
_DIRECT_DIRNAME = "_direct"
_RESERVED_DATA_DIRS = frozenset({_SHARED_DIRNAME, _DIRECT_DIRNAME})

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_UNSAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.\-]+")


@dataclass
class TypeReport:
    """Per-type sync outcome — what landed, what changed, what was removed."""

    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: list = field(default_factory=list)


@dataclass
class SyncReport:
    """Aggregate sync outcome the CLI surface uses for the status line."""

    direct_tables: TypeReport = field(default_factory=TypeReport)
    data_packages: TypeReport = field(default_factory=TypeReport)
    memory_domains: TypeReport = field(default_factory=TypeReport)
    invariant_violations: list = field(default_factory=list)

    def total_changes(self) -> int:
        return sum(
            r.added + r.updated + r.removed
            for r in (self.direct_tables, self.data_packages, self.memory_domains)
        )


# ---------------------------------------------------------------------------
# Reference store (symlink/hardlink/copy with strategy tracking)
# ---------------------------------------------------------------------------


def _safe_segment(name: str) -> str:
    """Return a filesystem-safe path segment derived from ``name``.

    Manifest slugs and table ids come from server-controlled rows, but the
    sync layer is the last line of defense before they hit the local
    filesystem. A table's display ``name`` is a human label ("Agnes audit
    log") that is a legal name yet not usable as a raw path segment, so:

    - if ``name`` is already strictly safe (and not ``.``/``..``), return it
      verbatim so previously-synced filenames stay byte-for-byte stable;
    - otherwise coerce every run of unsafe chars to ``_`` and strip leading/
      trailing separators, raising only when nothing usable remains or the
      result would traverse (``.``/``..``).
    """
    if not name:
        raise ValueError(f"unsafe path segment: {name!r}")
    if _SAFE_SEGMENT_RE.match(name) and name not in {".", ".."}:
        return name
    cleaned = _UNSAFE_SEGMENT_RE.sub("_", name).strip("._-")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"unsafe path segment: {name!r}")
    return cleaned


def _safe_segment_map(items: Iterable[dict], key: str, kind: str) -> Dict[str, dict]:
    """Build ``{safe_segment: item}`` from server rows, skipping any row whose
    ``key`` can't yield a usable path segment.

    A single poison row must not abort the whole type's sync: sanitize what we
    can, log + drop what we can't, so the rest of the manifest still syncs.
    """
    out: Dict[str, dict] = {}
    for it in items:
        raw = it.get(key)
        if not raw:
            continue
        try:
            out[_safe_segment(raw)] = it
        except ValueError:
            logger.warning("pull: skipping %s with unsafe %s %r", kind, key, raw)
    return out


def _shared_path(local_data_dir: Path, table_id: str) -> Path:
    return local_data_dir / _SHARED_DIRNAME / f"{_safe_segment(table_id)}.parquet"


def _link_or_copy(
    src: Path, dst: Path,
) -> str:
    """Create a reference from ``dst`` → ``src``.

    Tries ``os.symlink`` first (cheap, observable, works on POSIX +
    modern Windows with developer mode). Falls back to ``os.link``
    (hardlink — same volume only) on OSError, then ``shutil.copy2``
    (dedup lost, function preserved).

    Returns the strategy used so the sync_state row records it and the
    reverse delete picks the right unlink path.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish: if a stale reference exists, drop it first so the create
    # below doesn't trip over FileExistsError on POSIX symlink.
    if dst.exists() or dst.is_symlink():
        try:
            dst.unlink()
        except OSError:
            logger.warning("link/copy: pre-existing %s could not be removed", dst)
    try:
        os.symlink(src, dst)
        return "symlink"
    except OSError:
        pass
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        pass
    shutil.copy2(src, dst)
    logger.warning(
        "fallback to copy for %s — dedup will be lost for this entry",
        dst.name,
    )
    return "copy"


def _remove_reference(ref: Path, strategy: Optional[str]) -> None:
    """Reverse of ``_link_or_copy``. ``strategy`` selects the right unlink
    semantics; ``None`` falls back to a best-effort ``unlink`` (handles
    pre-strategy-tracking state files)."""
    if not ref.exists() and not ref.is_symlink():
        return
    # symlink / hardlink / copy all unlink the same way at the local node
    # — the difference is in inode bookkeeping. shutil.copy2's target is
    # an independent file and must be `unlink`'d, same as the others.
    try:
        ref.unlink()
    except OSError:
        logger.warning("could not unlink reference %s", ref)


def _count_references(shared_path: Path, local_data_dir: Path) -> int:
    """Count how many references in ``local_data_dir`` point at
    ``shared_path``. A reference is a symlink whose resolved target is
    the shared file, OR a hardlink (same inode), OR a copy of the
    canonical file (same byte content — best-effort heuristic).

    For symlinks we use ``Path.resolve()`` and compare; for hardlinks we
    compare ``st_ino``; for copies we don't track them per-shared (the
    fallback path explicitly logs that dedup is lost).
    """
    if not shared_path.exists():
        return 0
    try:
        shared_stat = shared_path.stat()
    except OSError:
        return 0
    count = 0
    for sub in local_data_dir.iterdir():
        if sub.name == _SHARED_DIRNAME or not sub.is_dir():
            continue
        for ref in sub.iterdir():
            if not ref.exists() and not ref.is_symlink():
                continue
            if ref.is_symlink():
                try:
                    target = ref.resolve(strict=False)
                except OSError:
                    continue
                if target == shared_path.resolve(strict=False):
                    count += 1
                continue
            try:
                ref_stat = ref.stat()
            except OSError:
                continue
            if (
                ref_stat.st_dev == shared_stat.st_dev
                and ref_stat.st_ino == shared_stat.st_ino
            ):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Local-state I/O (sync_state.json under <local_dir>)
# ---------------------------------------------------------------------------


def _read_sync_state(local_dir: Path) -> Dict[str, Any]:
    import json

    p = local_dir / "sync_state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("sync_state.json unreadable; treating as empty")
        return {}


def _write_sync_state(local_dir: Path, state: Dict[str, Any]) -> None:
    import json

    local_dir.mkdir(parents=True, exist_ok=True)
    p = local_dir / "sync_state.json"
    p.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# Manifest parsing helpers
# ---------------------------------------------------------------------------


def _server_table_md5(t: dict) -> str:
    """Server-side md5 lives on different keys depending on connector —
    legacy ``tables[]`` uses ``hash``, new ``data_packages[].tables[]``
    uses ``md5``. Accept both."""
    return t.get("md5") or t.get("hash") or ""


def _server_table_url(t: dict) -> str:
    """Download URL. New manifest carries ``parquet_url`` per table;
    legacy code uses ``/api/data/{id}/download`` derived from the id."""
    url = t.get("parquet_url")
    if url:
        return url
    tid = t.get("id")
    if tid:
        return f"/api/data/{tid}/download"
    return ""


def _server_table_skip(t: dict) -> bool:
    """Remote-mode tables have no parquet — skip them in the per-type
    sync (the master DuckDB ATTACH still resolves them on demand)."""
    return (t.get("query_mode") or "").lower() == "remote"


# ---------------------------------------------------------------------------
# Per-type sync
# ---------------------------------------------------------------------------


def _sync_table_into(
    *,
    table: dict,
    dest: Path,
    local_data_dir: Path,
    table_state: dict,
    fetcher: Callable[[str, Path], None],
    md5_of: Callable[[Path], str],
) -> Tuple[Optional[dict], bool]:
    """Materialize one server-side table into ``dest`` (a reference
    inside a package or ``_direct/``). Returns ``(state_entry, did_fetch)``.

    The canonical parquet lives in ``_shared/<id>.parquet``. If a
    matching file already exists with the right md5 it is reused (no
    re-fetch); otherwise it's fetched once and every package that
    references it links to it.

    Skips remote-mode tables and tables missing both ``id`` and an md5
    (defensive — a malformed manifest entry shouldn't crash the pull).
    """
    if _server_table_skip(table):
        return None, False
    tid = table.get("id")
    if not tid:
        return None, False
    expected_md5 = _server_table_md5(table)
    shared = _shared_path(local_data_dir, tid)
    must_fetch = (
        not shared.exists()
        or (expected_md5 and md5_of(shared) != expected_md5)
    )
    fetched = False
    if must_fetch:
        shared.parent.mkdir(parents=True, exist_ok=True)
        url = _server_table_url(table)
        if not url:
            raise ValueError(f"manifest table {tid} has no parquet_url and no id")
        fetcher(url, shared)
        fetched = True
        if expected_md5:
            actual = md5_of(shared)
            if actual != expected_md5:
                shared.unlink(missing_ok=True)
                raise ValueError(
                    f"md5 mismatch on {tid}: expected {expected_md5[:12]}, got {actual[:12]}"
                )

    strategy = _link_or_copy(shared, dest)
    entry = {
        "table_id": tid,
        "md5": expected_md5,
        "shared_path": str(shared),
        "ref_path": str(dest),
        "strategy": strategy,
    }
    return entry, fetched


def _delete_table_reference(
    *,
    ref_path: Path,
    shared_path: Path,
    local_data_dir: Path,
    strategy: Optional[str],
) -> None:
    """Remove a reference and, if no references remain, the shared
    parquet itself. Reference counting walks every NON-``_shared``
    subdir under ``data/`` looking for other references."""
    _remove_reference(ref_path, strategy)
    # Refresh count AFTER our own unlink so we don't count it ourselves.
    remaining = _count_references(shared_path, local_data_dir)
    if remaining == 0 and shared_path.exists():
        try:
            shared_path.unlink()
        except OSError:
            logger.warning("could not unlink orphan shared %s", shared_path)


def sync_direct_tables(
    *,
    server_tables: List[dict],
    local_data_dir: Path,
    prev_state: Dict[str, Any],
    fetcher: Callable[[str, Path], None],
    md5_of: Callable[[Path], str],
) -> Tuple[Dict[str, Any], TypeReport]:
    """Sync the ``direct_tables`` array.

    Each table lives under ``data/_direct/<name>.parquet`` linked to
    ``data/_shared/<id>.parquet``. The state dict key is the table's
    ``name`` (used as the on-disk filename) so removes can find the
    correct reference even when ``id`` rotates server-side.
    """
    report = TypeReport()
    new_state: Dict[str, Any] = {}
    server_names = _safe_segment_map(server_tables, "name", "direct table")
    prev_names = set(prev_state.keys())

    direct_dir = local_data_dir / _DIRECT_DIRNAME

    # to_add ∪ to_update
    for name, table in server_names.items():
        if _server_table_skip(table):
            continue
        dest = direct_dir / f"{name}.parquet"
        prev = prev_state.get(name)
        is_new = prev is None
        try:
            entry, fetched = _sync_table_into(
                table=table,
                dest=dest,
                local_data_dir=local_data_dir,
                table_state=prev or {},
                fetcher=fetcher,
                md5_of=md5_of,
            )
        except Exception as exc:
            report.errors.append({"name": name, "error": str(exc)})
            continue
        if entry is None:
            continue
        new_state[name] = entry
        if is_new:
            report.added += 1
        elif fetched:
            report.updated += 1

    # to_delete = previous − server
    for name in prev_names - set(server_names):
        prev = prev_state.get(name) or {}
        ref_path = Path(prev.get("ref_path") or (direct_dir / f"{name}.parquet"))
        shared = Path(prev.get("shared_path") or "")
        if not shared.exists() and prev.get("table_id"):
            shared = _shared_path(local_data_dir, prev["table_id"])
        _delete_table_reference(
            ref_path=ref_path,
            shared_path=shared,
            local_data_dir=local_data_dir,
            strategy=prev.get("strategy"),
        )
        report.removed += 1

    return new_state, report


def sync_data_packages(
    *,
    server_packages: List[dict],
    local_data_dir: Path,
    prev_state: Dict[str, Any],
    fetcher: Callable[[str, Path], None],
    md5_of: Callable[[Path], str],
) -> Tuple[Dict[str, Any], TypeReport]:
    """Sync the ``data_packages`` array.

    State is a 2-level dict keyed by ``slug`` → ``{table_name: entry}``.
    Tables share the canonical ``_shared`` store across packages.
    """
    report = TypeReport()
    new_state: Dict[str, Dict[str, Any]] = {}

    server_by_slug = _safe_segment_map(server_packages, "slug", "data package")
    prev_slugs = set(prev_state.keys())

    for slug, pkg in server_by_slug.items():
        pkg_dir = local_data_dir / slug
        prev_pkg = prev_state.get(slug) or {}
        server_tables = pkg.get("tables") or []
        server_table_by_name = _safe_segment_map(server_tables, "name", "package table")
        new_pkg_state: Dict[str, Any] = {}

        for name, table in server_table_by_name.items():
            if _server_table_skip(table):
                continue
            dest = pkg_dir / f"{name}.parquet"
            prev = prev_pkg.get(name)
            is_new_table = prev is None
            try:
                entry, fetched = _sync_table_into(
                    table=table,
                    dest=dest,
                    local_data_dir=local_data_dir,
                    table_state=prev or {},
                    fetcher=fetcher,
                    md5_of=md5_of,
                )
            except Exception as exc:
                report.errors.append(
                    {"package": slug, "name": name, "error": str(exc)}
                )
                continue
            if entry is None:
                continue
            new_pkg_state[name] = entry
            if is_new_table:
                report.added += 1
            elif fetched:
                report.updated += 1

        # Tables in prev but not server → drop references
        for name in set(prev_pkg) - set(server_table_by_name):
            prev = prev_pkg.get(name) or {}
            ref_path = Path(prev.get("ref_path") or (pkg_dir / f"{name}.parquet"))
            shared = Path(prev.get("shared_path") or "")
            if not shared.exists() and prev.get("table_id"):
                shared = _shared_path(local_data_dir, prev["table_id"])
            _delete_table_reference(
                ref_path=ref_path,
                shared_path=shared,
                local_data_dir=local_data_dir,
                strategy=prev.get("strategy"),
            )
            report.removed += 1

        new_state[slug] = new_pkg_state

    # Packages in prev but not server → drop the whole package's references
    # and the package's directory.
    for slug in prev_slugs - set(server_by_slug):
        prev_pkg = prev_state.get(slug) or {}
        pkg_dir = local_data_dir / slug
        for name, prev in prev_pkg.items():
            ref_path = Path(prev.get("ref_path") or (pkg_dir / f"{name}.parquet"))
            shared = Path(prev.get("shared_path") or "")
            if not shared.exists() and prev.get("table_id"):
                shared = _shared_path(local_data_dir, prev["table_id"])
            _delete_table_reference(
                ref_path=ref_path,
                shared_path=shared,
                local_data_dir=local_data_dir,
                strategy=prev.get("strategy"),
            )
            report.removed += 1
        # Drop the (now-empty) package dir.
        if pkg_dir.exists():
            try:
                pkg_dir.rmdir()
            except OSError:
                # Non-empty (e.g. a stale file we don't track) — leave it
                # for the next audit_invariants to surface.
                pass

    return new_state, report


def sync_memory_domains(
    *,
    server_domains: List[dict],
    local_memory_dir: Path,
    prev_state: Dict[str, Any],
    bundle_fetcher: Callable[[str], bytes],
) -> Tuple[Dict[str, Any], TypeReport]:
    """Sync the ``memory_domains`` array.

    Each domain materializes a single ``<slug>/bundle.md`` written from
    ``/api/memory/bundle?domain=<slug>``. The state row carries
    ``md5`` so unchanged bundles aren't re-fetched on idempotent pulls.
    """
    report = TypeReport()
    new_state: Dict[str, Any] = {}

    server_by_slug = _safe_segment_map(server_domains, "slug", "memory domain")
    prev_slugs = set(prev_state.keys())

    for slug, dom in server_by_slug.items():
        prev = prev_state.get(slug)
        is_new = prev is None
        expected_md5 = dom.get("md5") or ""
        bundle_path = local_memory_dir / slug / "bundle.md"
        must_fetch = (
            not bundle_path.exists()
            or expected_md5 != (prev or {}).get("md5")
        )
        if must_fetch:
            try:
                body = bundle_fetcher(slug)
            except Exception as exc:
                report.errors.append({"slug": slug, "error": str(exc)})
                continue
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            bundle_path.write_bytes(body)
            if is_new:
                report.added += 1
            else:
                report.updated += 1
        new_state[slug] = {
            "slug": slug,
            "md5": expected_md5,
            "path": str(bundle_path),
        }

    for slug in prev_slugs - set(server_by_slug):
        prev = prev_state.get(slug) or {}
        bundle_path = Path(prev.get("path") or (local_memory_dir / slug / "bundle.md"))
        if bundle_path.exists():
            try:
                bundle_path.unlink()
            except OSError:
                pass
        # Remove empty dir for hygiene.
        if bundle_path.parent.exists():
            try:
                bundle_path.parent.rmdir()
            except OSError:
                pass
        report.removed += 1

    return new_state, report


# ---------------------------------------------------------------------------
# Invariant audit
# ---------------------------------------------------------------------------


def audit_invariants(
    local_data_dir: Path, sync_state: Dict[str, Any]
) -> List[str]:
    """Surface drift between disk + sync_state.

    Returns a list of human-readable violation strings — emitted as
    WARNING and used by the next pull to auto-heal:

      - ``orphan _shared parquet`` — file in ``_shared/`` with zero
        references → can be deleted next pull (we only WARN here, not
        auto-clean, because the file may belong to a future package the
        caller hasn't pulled yet; the next full pull will reconcile).
      - ``broken reference`` — a state-recorded ref path doesn't exist
        on disk → next pull will re-create it via _sync_table_into.
      - ``dangling shared`` — state references a shared parquet that
        doesn't exist → next pull re-fetches.
    """
    violations: List[str] = []
    shared_dir = local_data_dir / _SHARED_DIRNAME
    if not shared_dir.exists():
        return violations

    # 1. Walk _shared/* — flag files with no references.
    referenced_shared = set()
    for type_state in sync_state.values():
        if not isinstance(type_state, dict):
            continue
        # Direct tables: type_state is {name: entry}
        for v in type_state.values():
            if isinstance(v, dict) and v.get("shared_path"):
                referenced_shared.add(Path(v["shared_path"]).resolve(strict=False))
            elif isinstance(v, dict):
                # Package: v is {name: entry}
                for inner in v.values():
                    if isinstance(inner, dict) and inner.get("shared_path"):
                        referenced_shared.add(
                            Path(inner["shared_path"]).resolve(strict=False)
                        )

    for f in shared_dir.iterdir():
        if not f.is_file():
            continue
        if f.resolve(strict=False) not in referenced_shared:
            # Independent verification: also scan disk references — a
            # _shared parquet referenced only through copy-strategy
            # leaves no symlink but is still legitimate.
            if _count_references(f, local_data_dir) == 0:
                violations.append(f"orphan _shared parquet: {f.name}")

    # 2. Verify every reference in sync_state still exists on disk.
    def _walk_entries(state):
        if isinstance(state, dict):
            for v in state.values():
                if isinstance(v, dict):
                    if "ref_path" in v:
                        yield v
                    else:
                        yield from _walk_entries(v)

    for entry in _walk_entries(sync_state):
        ref = Path(entry.get("ref_path") or "")
        if ref and not ref.exists() and not ref.is_symlink():
            violations.append(f"broken reference: {ref}")
        shared = Path(entry.get("shared_path") or "")
        if shared and not shared.exists():
            violations.append(f"dangling shared: {shared}")

    return violations


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


@dataclass
class PullStackOptions:
    """Inputs to ``run_stack_sync``. Kept as a dataclass so callers can
    pass in test-friendly fetchers/hashers without piling kwargs."""

    manifest: Dict[str, Any]
    local_dir: Path
    fetcher: Callable[[str, Path], None]
    md5_of: Callable[[Path], str]
    bundle_fetcher: Callable[[str], bytes]


def run_stack_sync(opts: PullStackOptions) -> SyncReport:
    """Top-level sync entry point.

    1. Read prior ``sync_state.json`` under ``<local_dir>/``.
    2. Sync ``direct_tables`` → ``<local_dir>/data/_direct/``.
    3. Sync ``data_packages`` → ``<local_dir>/data/<slug>/``.
    4. Sync ``memory_domains`` → ``<local_dir>/memory/<slug>/``.
    5. Persist new sync_state.
    6. Audit invariants, log violations.

    Steps 2-4 share the canonical ``<local_dir>/data/_shared/`` store
    with reference counting.
    """
    local_dir = Path(opts.local_dir)
    local_data_dir = local_dir / "data"
    local_memory_dir = local_dir / "memory"
    local_data_dir.mkdir(parents=True, exist_ok=True)
    (local_data_dir / _SHARED_DIRNAME).mkdir(parents=True, exist_ok=True)

    prev = _read_sync_state(local_dir)
    prev_direct = prev.get("direct_tables") or {}
    prev_packages = prev.get("data_packages") or {}
    prev_memory = prev.get("memory_domains") or {}

    direct_state, direct_report = sync_direct_tables(
        server_tables=opts.manifest.get("direct_tables") or [],
        local_data_dir=local_data_dir,
        prev_state=prev_direct,
        fetcher=opts.fetcher,
        md5_of=opts.md5_of,
    )
    pkg_state, pkg_report = sync_data_packages(
        server_packages=opts.manifest.get("data_packages") or [],
        local_data_dir=local_data_dir,
        prev_state=prev_packages,
        fetcher=opts.fetcher,
        md5_of=opts.md5_of,
    )
    mem_state, mem_report = sync_memory_domains(
        server_domains=opts.manifest.get("memory_domains") or [],
        local_memory_dir=local_memory_dir,
        prev_state=prev_memory,
        bundle_fetcher=opts.bundle_fetcher,
    )

    new_state = {
        **prev,
        "direct_tables": direct_state,
        "data_packages": pkg_state,
        "memory_domains": mem_state,
        "last_sync_unix": int(time.time()),
    }
    _write_sync_state(local_dir, new_state)

    report = SyncReport(
        direct_tables=direct_report,
        data_packages=pkg_report,
        memory_domains=mem_report,
    )

    violations = audit_invariants(local_data_dir, new_state)
    if violations:
        for v in violations:
            logger.warning("sync invariant violation: %s", v)
        report.invariant_violations = violations
    return report
