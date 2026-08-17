"""Atomic parquet-publish protocol for the extract layout.

Every extractor eventually writes a parquet file into a location that OTHER,
unrelated code treats as already-complete: `src/orchestrator.py`'s
`_hash_table_parts` MD5s whatever bytes sit under `table_dir.rglob("*.parquet")`,
`cli/lib/pull.py` ships that hash (and the file) to every analyst, and the
master DuckDB views the orchestrator builds glob the same directory on every
query — including mid-write. A writer that lands bytes directly at the served
path, or that stages through a temp file without the guarantees below, can be
hashed, pulled, or queried half-written.

Lives here (`src/`), not under any one `connectors/` package, because the
invariant belongs to the READERS, not to any single writer: the hasher, the
view glob, and `agnes pull` are shared infrastructure every connector answers
to, so the primitive that keeps them safe belongs next to the contract it
protects. `connectors/jira/organizations.py` already had to reach across into
`connectors/jira/transform.py` — a module flagged sensitive in CLAUDE.md — for
what is a generic filesystem primitive with no Jira-specific content; that
reach-across was the tell that the helper had outgrown its original home.

Counter-argument, kept here rather than discarded: a reuse pass on the
original Jira-local helper (#1354) argued for keeping it connector-local,
because the writers publishing through it are genuinely not interchangeable —
different formats (Parquet via PyArrow, via DuckDB ``COPY``, via pandas),
different concurrency stories (a webhook-driven per-issue upsert vs. a
scheduled full-table materialize vs. an admin-triggered one-shot), and two of
the call sites this module now serves are not PyArrow at all. That argument is
right about the writers and wrong about the conclusion: the piece that is
actually shared is the PUBLISH protocol (temp path -> chmod -> replace), not
the writer. So this module exposes exactly that protocol and has no opinion
on format or compression — `atomic_publish` hands back a temp path and gets
out of the way; every call site still writes its own bytes with its own
writer and its own options, visible at the call site, not funneled through a
lowest-common-denominator wrapper here.

Mechanism (unchanged from the Jira original — see git history for
``connectors/jira/transform.py::write_parquet_atomic`` prior to this move):
write the full file to a **per-process** temp path beside the destination,
``chmod`` it ``0o644``, then ``os.replace`` it onto the destination.
``os.replace`` is atomic within a filesystem, so every reader sees either the
whole previous file or the whole new one, never a prefix.

Two incidents shaped this, both worth keeping in mind before changing it:

- The temp name is per-process (``<dest name>.<pid>.tmp``), not a fixed name
  derived only from ``dest``. A shared name let two writers each
  ``os.replace`` the other's in-flight file, with the loser's cleanup then
  deleting the winner's temp out from under it (Devin Review on #1274).
- This deliberately does not use ``tempfile.mkstemp``: it creates the file
  ``0600``, and ``os.replace`` preserves the mode, so the published parquet
  would silently drop from ``0644`` to ``0600`` (incident #203). The explicit
  ``chmod`` also defends the same outcome arriving from the writer's own
  default mode (``0666 & umask``) under a restrictive umask (``0077``, seen
  in some container/systemd units) — without it, the write's own umask would
  decide the published permissions by accident.
- Cleanup on a failed publish lives on the exception path, not in a
  ``finally``: a successful ``os.replace`` has already moved the temp away,
  so a ``finally`` would spend a failing ``unlink(2)`` (or a swallowed
  ``FileNotFoundError`` without ``missing_ok=True``) on every single
  successful publish — several of these call sites run on a poll/schedule
  measured in thousands of invocations per cycle. ``except BaseException``
  (not ``Exception``) keeps the coverage a ``finally`` had for
  ``KeyboardInterrupt``/``SystemExit``; unlinking only the CURRENT process's
  own temp — never a glob, never anything else in ``dest``'s directory — is
  what keeps that cleanup safe while another writer is concurrently
  mid-publish to the same ``dest``.

The temp name never matches a reader's ``*.parquet`` glob (it always ends in
``.tmp``), so a stray one left behind by a hard kill (SIGKILL, OOM) is inert —
never served, never hashed — until an operator or a later run cleans it up.

Two call shapes, one protocol:

- Most writers fit inside a single ``with`` block::

      with atomic_publish(dest) as tmp:
          pq.write_table(table, tmp, **your_own_options)

  or, for a DuckDB ``COPY``::

      with atomic_publish(dest) as tmp:
          safe = str(tmp).replace("'", "''")
          conn.execute(f"COPY (...) TO '{safe}' (FORMAT PARQUET)")

- A few writers span control flow too complex to nest cleanly inside one
  ``with`` (retries, several branches that may each populate the same temp).
  Those compute the temp path once with `atomic_publish_temp_path`, write to
  it however many steps that takes — cleaning up on their own failure paths,
  same as before this module existed — and call `atomic_publish_finalize`
  once, at the end, to commit.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

__all__ = [
    "atomic_publish",
    "atomic_publish_finalize",
    "atomic_publish_temp_path",
]


def atomic_publish_temp_path(dest: Path | str) -> Path:
    """The per-process temp path a publish to *dest* must stage through.

    Exposed so callers that can't use the `atomic_publish` context manager
    directly (a write spanning retries/branches — see the module docstring)
    can still get the exact same per-process name, and so tests can assert
    against it without duplicating the naming scheme. Two processes (or two
    calls with a different ``os.getpid()``) publishing to the same *dest*
    always get two different temp paths.
    """
    dest = Path(dest)
    return dest.parent / f"{dest.name}.{os.getpid()}.tmp"


def atomic_publish_finalize(tmp: Path | str, dest: Path | str) -> Path:
    """Commit a completed temp write: ``chmod 0644``, then atomically replace.

    Pairs with `atomic_publish_temp_path` for call sites whose write is too
    spread out to nest inside `atomic_publish`'s ``with`` block. This
    function only implements the commit half — cleaning up ``tmp`` on a
    failed write remains the caller's own responsibility along the way, same
    as `atomic_publish` does internally for the single-block case.
    """
    tmp = Path(tmp)
    dest = Path(dest)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)
    return dest


@contextmanager
def atomic_publish(dest: Path | str) -> Iterator[Path]:
    """Publish *dest* so no reader ever observes a partial file.

    Yields the per-process temp path (see `atomic_publish_temp_path`) to
    write the FULL new content of *dest* to. Write it with whatever writer
    and options belong at your call site — ``pq.write_table(table, tmp,
    **your_options)``, ``df.to_parquet(tmp)``, or a DuckDB
    ``conn.execute(f"COPY (...) TO '{tmp}' (FORMAT PARQUET)")`` all work
    identically; this context manager has no opinion on format.

    On a clean exit, the temp is committed onto *dest* via
    `atomic_publish_finalize` (``chmod 0o644`` then ``os.replace`` — atomic
    within a filesystem). On any exception raised inside the ``with`` block,
    the temp is removed and the exception propagates; *dest* is left exactly
    as it was before the call. See the module docstring for the
    per-process-naming and cleanup-not-``finally`` reasoning (incidents
    #1274, #203).
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = atomic_publish_temp_path(dest)
    try:
        yield tmp
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    else:
        atomic_publish_finalize(tmp, dest)
