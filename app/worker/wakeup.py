"""Low-latency job wakeup for the worker runtime (three-plane spec §3.3).

The lane slots in ``app/worker/runtime.py`` poll ``claim_next`` every
``poll_interval_s``. On Postgres we additionally ``LISTEN`` on the
``agnes_jobs`` channel so a fresh enqueue (which issues ``NOTIFY agnes_jobs``,
see ``JobsPgRepository.enqueue``) wakes an idle slot immediately instead of
waiting out the poll interval.

Polling stays the floor — it is what covers:
  * ``run_after``/retry eligibility (a backed-off job becomes claimable with
    no fresh enqueue, so no NOTIFY fires for it),
  * the DuckDB backend (no ``LISTEN``/``NOTIFY``),
  * any listener failure.

``idle_wait`` degrades to a plain ``poll_interval_s`` sleep whenever nothing
signals the event, so the worst case is exactly the pre-existing poll-only
behavior — this optimization can never make latency worse or skip a job.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

#: PG NOTIFY channel. MUST match the literal used by
#: ``src.repositories.jobs_pg.JobsPgRepository.enqueue`` (kept as separate
#: literals rather than a shared import so the repo layer carries no
#: dependency on the ``app`` package).
NOTIFY_CHANNEL = "agnes_jobs"

#: Backoff before re-establishing a dropped LISTEN connection.
_RECONNECT_BACKOFF_S = 5.0

_wake = asyncio.Event()


def signal() -> None:
    """Wake any lane slot currently in :func:`idle_wait`. Safe to call from
    the notify listener; a spurious signal just costs one extra
    ``claim_next`` attempt that returns ``None``."""
    _wake.set()


async def idle_wait(poll_interval_s: float) -> None:
    """Sleep up to ``poll_interval_s``, returning early if :func:`signal` was
    called. Always safe: with no signaller wired up (DuckDB backend, or the
    listener failed to start) this is just ``asyncio.sleep(poll_interval_s)``.
    """
    try:
        await asyncio.wait_for(_wake.wait(), timeout=poll_interval_s)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    finally:
        # Clear unconditionally: one signal releases whichever slot wins the
        # wake; the others fall through to their own claim attempt on the
        # next loop iteration regardless (claim_next is the real arbiter).
        _wake.clear()


async def notify_listener() -> None:
    """Best-effort PG ``LISTEN agnes_jobs`` loop that calls :func:`signal` on
    every notification. Postgres-only; a clean no-op on DuckDB.

    Robustness: any prerequisite gap (not on PG, psycopg missing, URL
    unresolvable) returns quietly → poll-only. A dropped connection
    reconnects after ``_RECONNECT_BACKOFF_S``. ``CancelledError`` (worker
    shutdown) propagates. Nothing here can fail the worker — the lane slots
    keep polling regardless.
    """
    from src.repositories import use_pg

    if not use_pg():
        return
    try:
        import psycopg  # noqa: F401

        from src.db_pg import get_engine

        # psycopg wants a libpq URL (``postgresql://``), not SQLAlchemy's
        # ``postgresql+psycopg://`` dialect form.
        url = get_engine().url.set(drivername="postgresql").render_as_string(hide_password=False)
    except Exception:
        logger.debug("job wakeup: prerequisites unavailable; worker stays poll-only", exc_info=True)
        return

    import psycopg

    while True:
        try:
            aconn = await psycopg.AsyncConnection.connect(url, autocommit=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "job wakeup: LISTEN connect failed; retrying in %.0fs (poll-only meanwhile)",
                _RECONNECT_BACKOFF_S,
                exc_info=True,
            )
            await asyncio.sleep(_RECONNECT_BACKOFF_S)
            continue
        try:
            await aconn.execute(f"LISTEN {NOTIFY_CHANNEL}")
            logger.info("job wakeup: LISTEN %s active", NOTIFY_CHANNEL)
            async for _notify in aconn.notifies():
                signal()
            # notifies() ended without raising — the server closed the
            # connection gracefully. Back off same as the error path below;
            # otherwise a connection that keeps ending cleanly reconnects in
            # a tight loop instead of waiting.
            logger.warning(
                "job wakeup: LISTEN stream ended; reconnecting in %.0fs (poll-only meanwhile)",
                _RECONNECT_BACKOFF_S,
            )
            await asyncio.sleep(_RECONNECT_BACKOFF_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "job wakeup: LISTEN loop dropped; reconnecting in %.0fs (poll-only meanwhile)",
                _RECONNECT_BACKOFF_S,
                exc_info=True,
            )
            await asyncio.sleep(_RECONNECT_BACKOFF_S)
        finally:
            with contextlib.suppress(Exception):
                await aconn.close()
