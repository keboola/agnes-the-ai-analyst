"""One policy for every wall-clock benchmark in the suite.

Several tests measure elapsed time against a target and describe that target,
in their own message, as guidance — "tune in a follow-up if this is a
persistent regression", "thresholds are guidance, not hard gates". They then
enforced it with a plain ``assert``, so a contended runner failed the build and
the only available response was to raise the number. `test_stack_resolver_perf`
had its manifest target raised three times that way (200 → 500 → 600 → 1000 ms)
and still went red at 1293 ms; `test_grants_soft_downgrade` went red at 1.088 s
against a 1.0 s budget on a PR that changed two documentation files.

Each rerun costs a full CI cycle, and each bump makes the number mean less. So
the policy lives here instead, once:

  - under the target        → silent
  - over it, under the      → record the measurement and warn; build stays green
    ceiling
  - at or past the ceiling  → fail

``PERF_CEILING_FACTOR`` (default 4, ``AGNES_PERF_CEILING_FACTOR`` to override)
is what makes that safe to do. A 4x overshoot is not a busy runner, it is a
regression — and unlike the per-benchmark targets, the ceiling does not need
bumping when CI has a bad morning.
"""

from __future__ import annotations

import os
import warnings

PERF_CEILING_FACTOR = float(os.environ.get("AGNES_PERF_CEILING_FACTOR", "4"))


def check_perf(label: str, actual_ms: float, target_ms: float) -> None:
    """Report a benchmark against ``target_ms`` per the module policy.

    Both arguments are milliseconds; a caller holding seconds converts first,
    so the failure message reads in one unit no matter which benchmark raised
    it.
    """
    if actual_ms < target_ms:
        return
    over = actual_ms / target_ms
    detail = f"{label} {actual_ms:.2f}ms exceeds target {target_ms:.0f}ms ({over:.1f}x)"
    if actual_ms >= target_ms * PERF_CEILING_FACTOR:
        raise AssertionError(
            f"{detail} — past the {PERF_CEILING_FACTOR:.0f}x ceiling, so this is a "
            f"regression rather than a slow runner. Profile the change; raise "
            f"AGNES_PERF_CEILING_FACTOR only with a measurement that says why."
        )
    warnings.warn(
        f"{detail} — under the {PERF_CEILING_FACTOR:.0f}x ceiling, so not failing "
        f"the build. Persistent readings here mean the target is stale or the "
        f"code regressed; tune deliberately rather than by re-running.",
        stacklevel=2,
    )
