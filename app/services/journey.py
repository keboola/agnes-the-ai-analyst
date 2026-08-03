"""Onboarding-journey milestones, marked from the actions that earn them.

The onboarding checklist (``user_journey_state``, surfaced by the rail's "Set up
Agnes" card) used to be written from exactly one place: the browser, when the
reader clicked a row in the checklist itself. That made the card a manual to-do
list rather than a record of progress — someone who followed the coach-mark to
the Library, clicked **Add** on a data package and watched the toast confirm it
came back to a checklist that still said *Put knowledge in your stack*. Guiding
someone to do a thing and then not noticing they did it is worse than not
guiding them at all.

So the milestone is recorded where the real work happens — the endpoint that
carries out the action — which also makes it surface-agnostic: putting something
in your stack from the CLI, from chat, from MCP or from the Library page all
count the same, because they all end up in the same handler.

Two rules for every call site:

* **Only ever set a flag to True.** These are "this happened at least once"
  milestones; nothing here may un-tick a step. (The checklist's own "Start over"
  is the single writer allowed to clear them, via PUT /api/chat/journey.)
* **Never let bookkeeping break the action.** A failure here means an onboarding
  card is one tick behind, which is worth nothing next to failing the subscribe
  the user actually asked for — so everything is swallowed. The step is also
  still reachable by hand from the checklist.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def mark_journey(user_id: str | None, **flags: bool) -> None:
    """Best-effort: record onboarding milestones for ``user_id``.

    No-op when there is no user (service tokens, unauthenticated paths), when no
    flag is passed, or when every flag is already set — the read-before-write
    keeps a hot path like "add to stack" from issuing an upsert per call for a
    user who passed this milestone months ago.
    """
    if not user_id or not flags:
        return
    try:
        from src.repositories import user_journey_repo

        repo = user_journey_repo()
        current = repo.get(user_id)
        # True-only, and only what actually changes.
        pending = {k: True for k, v in flags.items() if v and not current.get(k)}
        if not pending:
            return
        repo.update(user_id, **pending)
    except Exception:  # pragma: no cover - defensive; see module docstring
        log.debug("journey: could not mark %s for %s", sorted(flags), user_id, exc_info=True)
