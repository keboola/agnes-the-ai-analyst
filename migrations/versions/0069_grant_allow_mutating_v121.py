"""tool_grants.allow_mutating — per-group opt-in for mutating passthrough tools

Mirrors DuckDB ``_v120_to_v121``.

``check_mutating`` (``app/api/mcp_policy.py``) was admin-or-bust: a
``mutating=True`` passthrough tool could never be invoked by a non-admin,
including every agent profile (``AgentPrincipal.is_admin`` is pinned False by
design). The policy module reserved this evolution — "a separate
``mutating_grant`` row" — and this column is it: a ``tool_grants`` row with
``allow_mutating=TRUE`` lets members of that group (and agents whose owner is
a member, still narrowed by connection scope) invoke the tool. Default FALSE
keeps every existing grant read-only, so nothing changes until an admin opts
a group in per tool.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0069_grant_allow_mutating_v121"
down_revision: Union[str, None] = "0068_agent_schedules_v120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tool_grants",
        sa.Column("allow_mutating", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
    )


def downgrade() -> None:
    op.drop_column("tool_grants", "allow_mutating")
