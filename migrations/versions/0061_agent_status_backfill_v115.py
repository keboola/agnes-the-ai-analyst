"""agents.status backfill — reclassify pre-existing governance agents as ready

Mirrors DuckDB ``_v114_to_v115``. Data-only: no column change.

An agent created through the governance API (``POST /api/v1/agents``,
``app/api/agents_admin.py::create_agent``) always carries an explicit,
caller-chosen slug, and that route refuses to change it afterwards (``PUT``
400s ``slug_immutable``) — the agent is published by definition the moment
it exists. Before this release the create route never set a ``status`` at
all, and both repositories' ``create()`` default a missing one to
``'draft'`` — so a governance-created agent was indistinguishable from a
``/agents`` builder placeholder that was never named. The builder's
draft-rename rule (``app/api/agents.py::_draft_slug_rename``) only freezes a
slug once the agent is ``'ready'``, so a deliberately-chosen slug — the one
a PAT may already be minted against — stayed renameable forever through the
builder's PATCH. The create route now sets ``status='ready'`` going
forward; this migration closes the gap for every agent created before that
fix shipped.

The discriminator is the row's ``id`` PREFIX, not its slug. Every
builder-created row — ``POST /api/agents`` (``app/api/agents.py``,
~line 346) — carries an ``agt_`` prefix regardless of whether the agent was
ever named, because that route mints ``"agt_" + uuid4().hex`` before the
caller supplies (or omits) a ``name``. A row created through the governance
API (``POST /api/v1/agents``, ``app/api/agents_admin.py::create_agent``) or
the seeded default (``AgentsRepository.get_or_create_default``) always gets
a bare ``uuid4()`` — neither path ever applies the prefix. A slug-based
check (matching only the unnamed placeholder lineage ``agent`` / ``agent-N``)
was tried first and is WRONG: a builder draft that the user already named —
e.g. ``finance-bot`` — is not yet published (``_draft_slug_rename`` only
freezes its slug once ``status`` reaches ``'ready'``), but its slug no
longer matches the placeholder pattern, so the slug check promoted it
anyway and permanently froze an address for an agent that was never marked
ready. ``NOT (id LIKE 'agt\\_%' ESCAPE '\\')`` has no such gap: it is true
for every governance/default row and false for every builder row, named or
not, so it selects exactly the intended cohort regardless of naming state.
The seeded default agent (``is_default``) is excluded on top of that,
redundantly but explicitly: it is seeded with no status (COALESCEd to
``'draft'``) and is a PERMANENT draft by design, so promoting it here would
freeze an address that must keep renaming freely.

``downgrade()`` is deliberately a no-op: this migration carries no marker
distinguishing a row it flipped from one made ``'ready'`` afterward through
ordinary use (the builder's "mark ready" action), so there is no way to
revert only the rows this step touched without also reverting legitimate
post-migration state.

Revision ID: 0061_agent_status_backfill_v115
Revises: 0060_pkg_publisher_v113
Create Date: 2026-08-10

Note on the revision id: ``alembic_version.version_num`` is ``VARCHAR(32)``,
so keep new ids short (see 0051_store_publisher_v104's note).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0061_agent_status_backfill_v115"
down_revision: Union[str, None] = "0060_pkg_publisher_v113"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "agents" not in set(insp.get_table_names()):
        return
    op.execute(
        r"""
        UPDATE agents
           SET status = 'ready', updated_at = now()
         WHERE COALESCE(status, 'draft') = 'draft'
           AND NOT COALESCE(is_default, FALSE)
           AND NOT (id LIKE 'agt\_%' ESCAPE '\')
        """
    )


def downgrade() -> None:
    # See the module docstring: this backfill cannot be safely inverted.
    pass
