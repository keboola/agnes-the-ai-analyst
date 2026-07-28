"""Request-id -> job-id log correlation (three-plane wave-2D, Task 4).

The request-id middleware (``app/middleware/request_id.py``) already binds
a per-request id into ``app.logging_config.request_id_var`` for the life of
an HTTP request, and every structured log line picks it up automatically
(``app/logging_config.py``'s ``_RequestIdFilter`` / ``_JSONFormatter``).
That correlation stops at the API boundary though: once a handler enqueues
a job (``jobs_repo().enqueue(...)``), the job runs later, on the worker, in
a completely different asyncio context — there is no request in flight
there to read a request-id from.

This module closes that gap with two small, symmetric helpers:

- ``stamp_request_id`` — called at the API-layer enqueue call site, before
  handing the payload to ``jobs_repo().enqueue()``. Copies the *current*
  request-id (if any) into the payload under the reserved
  ``_enqueued_by_request`` key.
- ``bind_request_id`` / ``unbind_request_id`` — called by the worker
  (``app/worker/runtime.py``) around running a claimed job's handler.
  Reads ``_enqueued_by_request`` back out of the payload (if present) and
  binds it into the very same ``request_id_var`` contextvar the request
  middleware uses, so every log line emitted while the handler runs
  carries the originating request-id too.

Deliberately NOT in ``src/repositories/jobs.py``/``jobs_pg.py``: those
repos are pure persistence with no notion of HTTP requests or logging
contextvars, and both DuckDB/PG backends would otherwise need the same
import. Keeping this at the app layer means the repos stay backend-parity
simple, and any future enqueue call site (API route, webhook handler, CLI
admin command) opts in with a single ``stamp_request_id(...)`` wrap
around its payload dict.

Both directions are deliberately no-op-safe:

- ``stamp_request_id`` on a payload with no live request-id (a
  worker-internal or scheduler-internal enqueue with no request in
  flight) returns the payload unchanged — no key is added.
- ``bind_request_id`` on a payload missing the key, or carrying something
  malformed (not a non-empty string — e.g. an old job enqueued before
  this feature shipped, or a payload built by hand in a test), returns
  ``None`` and binds nothing. It never raises, so a malformed key can
  never break job execution.
"""

from __future__ import annotations

import contextvars
from typing import Any

from app.logging_config import request_id_var

#: Reserved payload key carrying the request-id that enqueued a job.
#: Prefixed with ``_`` to mark it as queue plumbing, not job business data
#: — handlers reading ``payload_json`` should ignore it.
ENQUEUED_BY_REQUEST_KEY = "_enqueued_by_request"


def stamp_request_id(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``payload`` with ``_enqueued_by_request`` set to the
    current request-id, if one is bound. No-op (returns ``payload``
    unchanged, same object) when there is no live request-id."""
    rid = request_id_var.get()
    if not rid:
        return payload
    return {**payload, ENQUEUED_BY_REQUEST_KEY: rid}


def bind_request_id(payload_json: dict[str, Any] | None) -> contextvars.Token | None:
    """Bind a claimed job's ``_enqueued_by_request`` (if present and a
    non-empty string) into ``request_id_var`` for the duration of running
    its handler. Returns the ``contextvars.Token`` to pass to
    ``unbind_request_id`` once the handler finishes, or ``None`` if nothing
    was bound — missing key, non-dict payload, or a malformed (non-string /
    empty) value all degrade to ``None`` rather than raising.
    """
    rid = payload_json.get(ENQUEUED_BY_REQUEST_KEY) if isinstance(payload_json, dict) else None
    if not isinstance(rid, str) or not rid:
        return None
    return request_id_var.set(rid)


def unbind_request_id(token: contextvars.Token | None) -> None:
    """Reset ``request_id_var`` using ``token`` from ``bind_request_id``.
    No-op if ``token`` is ``None`` (nothing was bound)."""
    if token is not None:
        request_id_var.reset(token)
