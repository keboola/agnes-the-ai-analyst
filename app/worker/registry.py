"""Job-kind registry for the worker runtime (wave-2B, spec §3.3).

Task kinds register themselves here (name -> ``JobKind``) before the
worker loop starts — see ``app/worker/kinds.py`` (a later task in the
same wave) for the five real kinds (``data-refresh``, ``jira-refresh``,
``marketplaces-sync``, ``session-collector``, ``corporate-memory``).
Empty by default; this task's tests register fake kinds only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

#: Lane identifiers. Plain string constants matching
#: ``JobsRepository.HEAVY_LANE`` / ``.LIGHT_LANE`` (`src/repositories/jobs.py`)
#: — duplicated rather than imported so this module (which the two backend
#: repo modules know nothing about) doesn't need to pick one of them to
#: depend on. The CONTRACT is the string value ("heavy"/"light"), not the
#: source of truth for it.
HEAVY_LANE = "heavy"
LIGHT_LANE = "light"

_VALID_LANES = (HEAVY_LANE, LIGHT_LANE)


@dataclass
class JobKind:
    """A registered handler for one ``jobs.kind`` value.

    ``handler`` is a plain synchronous callable — the worker loop runs it
    via ``asyncio.to_thread`` so a slow/blocking implementation (DB call,
    HTTP request, subprocess) never stalls the event loop. It receives the
    job's decoded ``payload_json`` dict and returns ``None`` on success;
    any raised exception is caught by the loop and turned into a
    ``fail(..., retry_in_seconds=kind.retry_in_seconds)`` call.
    """

    name: str
    handler: Callable[[dict], None]
    lane: str
    lease_seconds: int = 120
    retry_in_seconds: int | None = 300


#: Process-wide registry: ``kind name -> JobKind``. Populated by
#: ``register_kind()`` calls made before the worker loop starts (see
#: ``app/main.py`` lifespan ordering).
JOB_KINDS: dict[str, JobKind] = {}


def register_kind(kind: JobKind) -> None:
    """Register (or replace) a job kind.

    Raises ``ValueError`` for an unknown lane so a typo'd lane string
    fails loudly at registration time rather than silently never being
    polled by either lane runner.
    """
    if kind.lane not in _VALID_LANES:
        raise ValueError(f"JobKind {kind.name!r}: unknown lane {kind.lane!r} (expected one of {_VALID_LANES})")
    JOB_KINDS[kind.name] = kind
