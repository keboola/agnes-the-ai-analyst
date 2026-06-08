"""TEMPORARY heap-profiling endpoints (admin-only).

Diagnostic instrumentation for the BQ-instance anonymous-memory growth
investigation. NOT for permanent inclusion — added on a dev branch to
empirically locate the allocations that grow with BigQuery query volume.

Flow:
  1. POST /api/admin/debug/tracemalloc/start   → start tracing + baseline
  2. (generate churn: run warmup / BQ queries repeatedly)
  3. GET  /api/admin/debug/tracemalloc/top     → top allocation diffs vs baseline
  4. GET  /api/admin/debug/meminfo             → RSS + gc + suspect-structure sizes

All gated on require_admin.
"""

from __future__ import annotations

import gc
import logging
import os
import tracemalloc

from fastapi import APIRouter, Depends

from app.auth.access import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

_baseline: tracemalloc.Snapshot | None = None


def _rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return -1.0


def _frames(stat, limit: int) -> list[str]:
    out = []
    for fr in stat.traceback[-limit:]:
        out.append(f"{fr.filename}:{fr.lineno}")
    return out


@router.post("/api/admin/debug/tracemalloc/start")
async def tm_start(nframe: int = 25, user: dict = Depends(require_admin)):
    global _baseline
    if not tracemalloc.is_tracing():
        tracemalloc.start(nframe)
    _baseline = tracemalloc.take_snapshot()
    cur, peak = tracemalloc.get_traced_memory()
    logger.warning("tracemalloc baseline captured (rss=%.0f MiB)", _rss_mb())
    return {
        "tracing": tracemalloc.is_tracing(),
        "nframe": nframe,
        "traced_current_mb": cur / 1048576,
        "traced_peak_mb": peak / 1048576,
        "rss_mb": _rss_mb(),
    }


@router.get("/api/admin/debug/tracemalloc/top")
async def tm_top(
    n: int = 30,
    group: str = "traceback",
    frames: int = 8,
    user: dict = Depends(require_admin),
):
    if not tracemalloc.is_tracing():
        return {"error": "not tracing — call /start first"}
    snap = tracemalloc.take_snapshot()
    key = "traceback" if group == "traceback" else "lineno"
    if _baseline is not None:
        stats = snap.compare_to(_baseline, key)
        mode = "diff_vs_baseline"
    else:
        stats = snap.statistics(key)
        mode = "absolute"
    cur, peak = tracemalloc.get_traced_memory()
    top = []
    for s in stats[:n]:
        top.append({
            "size_diff_kb": round(getattr(s, "size_diff", 0) / 1024, 1),
            "size_kb": round(s.size / 1024, 1),
            "count_diff": getattr(s, "count_diff", 0),
            "count": s.count,
            "where": _frames(s, frames),
        })
    return {
        "mode": mode,
        "rss_mb": _rss_mb(),
        "traced_current_mb": round(cur / 1048576, 1),
        "traced_peak_mb": round(peak / 1048576, 1),
        "top": top,
    }


@router.post("/api/admin/debug/malloc_trim")
async def malloc_trim(user: dict = Depends(require_admin)):
    """Call glibc malloc_trim(0) to force freed-but-retained heap back to the
    OS. Proves whether high RSS is freeable allocator retention (RSS drops)
    vs genuinely-live memory (RSS unchanged)."""
    import ctypes
    before = _rss_mb()
    gc.collect()
    try:
        libc = ctypes.CDLL("libc.so.6")
        ret = int(libc.malloc_trim(0))
    except Exception as e:
        return {"error": str(e), "rss_mb": before}
    after = _rss_mb()
    return {
        "malloc_trim_ret": ret,
        "rss_before_mb": round(before, 1),
        "rss_after_mb": round(after, 1),
        "freed_mb": round(before - after, 1),
        "malloc_arena_max": os.environ.get("MALLOC_ARENA_MAX", "(unset)"),
        "malloc_trim_threshold": os.environ.get("MALLOC_TRIM_THRESHOLD_", "(unset)"),
    }


@router.get("/api/admin/debug/meminfo")
async def meminfo(user: dict = Depends(require_admin)):
    gc.collect()
    counts = {}
    try:
        from collections import Counter
        c = Counter(type(o).__name__ for o in gc.get_objects())
        counts = dict(c.most_common(25))
    except Exception as e:
        counts = {"error": str(e)}
    suspects = {}
    try:
        from connectors.bigquery import access as _bq
        suspects["bq_pool_len"] = len(_bq._pool)
    except Exception as e:
        suspects["bq_pool_err"] = str(e)
    return {
        "rss_mb": _rss_mb(),
        "gc_counts": gc.get_count(),
        "gc_objects_total": len(gc.get_objects()),
        "top_object_types": counts,
        "suspects": suspects,
        "pid": os.getpid(),
    }
