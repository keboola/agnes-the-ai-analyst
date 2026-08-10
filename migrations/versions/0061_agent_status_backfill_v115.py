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

The discriminator is sound ONLY for rows that predate this release: prior
to this PR ``update_agent`` never wrote ``slug`` at all, so a ``draft``
row's slug is exactly what ``create()`` put there. The builder's placeholder
lineage is the literal ``agent`` or a suffixed ``agent-N``
(``app/api/agents.py::_auto_slug``'s fallback for an unnamed agent) — a
``draft`` row on that slug is a builder-created agent never given a real
address, and must keep re-deriving its slug on the next rename. Anything
else on a ``draft`` row can only have arrived through an explicit,
caller-chosen slug at create time. The seeded default agent (``is_default``)
is excluded regardless of its slug: it is seeded with no status (COALESCEd
to ``'draft'``) and is a PERMANENT draft by design, so promoting it here
would freeze an address that must keep renaming freely.

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
        """
        UPDATE agents
           SET status = 'ready', updated_at = now()
         WHERE COALESCE(status, 'draft') = 'draft'
           AND NOT COALESCE(is_default, FALSE)
           AND NOT (slug = 'agent' OR slug ~ '^agent-[0-9]+$')
        """
    )


def downgrade() -> None:
    # See the module docstring: this backfill cannot be safely inverted.
    pass
