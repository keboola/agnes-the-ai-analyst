"""Persist + surface the outcome of `agnes self-upgrade` attempts (#478).

The SessionStart hook runs `agnes self-upgrade --quiet 2>/dev/null || true`,
so any failure (network, uv/pip resolution, smoke-test rollback) is
invisible: stdout is suppressed and the `|| true` swallows the exit code.
An analyst can sit on a stale CLI for weeks with no signal.

This module records each self-upgrade outcome to
``$AGNES_CONFIG_DIR/upgrade_status.json``::

    {"last_attempt_ts": 1718000000.0,
     "last_outcome": "failure",
     "consecutive_failures": 3}

The quiet SessionStart path stays silent but increments the counter on
failure and resets it on success. The next NON-quiet `agnes` command emits
a one-line stderr warning (once, via the root-callback banner path in
``cli/main.py``) when ``consecutive_failures >= _WARN_THRESHOLD``. ``--quiet``
commands never warn.

Best-effort throughout: a read/write failure here must never break a
working `agnes` command — that is the whole point of the feature.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from cli.config import _config_dir

_STATUS_FILENAME = "upgrade_status.json"

# Number of consecutive silent self-upgrade failures before the next
# non-quiet command surfaces a warning. Owner default = 3.
_WARN_THRESHOLD = 3


def _status_path():
    return _config_dir() / _STATUS_FILENAME


def read_status() -> dict:
    """Return the persisted status dict, or ``{}`` on missing/malformed file."""
    p = _status_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_status(entry: dict) -> None:
    p = _status_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(entry), encoding="utf-8")
    except OSError:
        pass  # best-effort — a status-write failure must not break the flow


def record_outcome(success: bool, *, now: Optional[float] = None) -> None:
    """Record a self-upgrade attempt outcome.

    On success the consecutive-failure counter resets to 0; on failure it
    increments from the prior value. ``last_outcome`` is ``"success"`` or
    ``"failure"`` and ``last_attempt_ts`` is the wall-clock time of the
    attempt.
    """
    ts = time.time() if now is None else now
    prior = read_status()
    prior_failures = prior.get("consecutive_failures", 0)
    if not isinstance(prior_failures, int) or prior_failures < 0:
        prior_failures = 0
    if success:
        consecutive = 0
        outcome = "success"
    else:
        consecutive = prior_failures + 1
        outcome = "failure"
    _write_status({
        "last_attempt_ts": ts,
        "last_outcome": outcome,
        "consecutive_failures": consecutive,
    })


def consecutive_failures() -> int:
    """How many self-upgrade attempts have failed in a row (0 if unknown)."""
    n = read_status().get("consecutive_failures", 0)
    return n if isinstance(n, int) and n >= 0 else 0


def should_warn() -> bool:
    """True iff the last ``_WARN_THRESHOLD`` self-upgrade attempts all failed
    AND we have not already warned at this exact failure count.

    The "already warned at this count" check keeps the warning to ONCE per
    distinct failure level: a non-quiet command at 3 failures warns; the
    next non-quiet command (still at 3) stays silent. A fresh failure
    (4, 5, …) re-arms the warning so the analyst sees that the situation
    is getting worse, not better."""
    s = read_status()
    n = s.get("consecutive_failures", 0)
    if not (isinstance(n, int) and n >= _WARN_THRESHOLD):
        return False
    warned_at = s.get("warned_at_failures")
    return warned_at != n


def mark_warned() -> None:
    """Record that we surfaced the warning at the current failure count, so
    subsequent non-quiet commands at the same level stay silent."""
    s = read_status()
    n = s.get("consecutive_failures", 0)
    if not (isinstance(n, int) and n >= 0):
        return
    s["warned_at_failures"] = n
    _write_status(s)


def format_failure_notice() -> str:
    """One-line stderr warning for repeated silent self-upgrade failures."""
    n = consecutive_failures()
    return (
        f"agnes self-upgrade has failed {n} times — "
        "run `agnes self-upgrade` to see the error."
    )
