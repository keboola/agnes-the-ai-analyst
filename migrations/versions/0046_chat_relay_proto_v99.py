"""chat_sessions.relay_protocol_version — restart-invariant sandbox reuse

Mirrors DuckDB ``_v98_to_v99``. Persists the relay protocol version the
runner bound to a session's ``sandbox_id``/``runner_pid`` refs speaks, so
``ChatManager``'s resume-vs-respawn decision survives a process restart
instead of relying on the in-process ``_known_protocol_sessions`` set
(always empty right after a restart). NULL on every existing row (and on
any row whose sandbox refs are cleared) means unknown/legacy — the same
conservative fresh-spawn behavior as before this migration.

Revision ID: 0046_chat_relay_proto_v99
Revises: 0045_user_journey_state_v98
Create Date: 2026-07-23

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0046_chat_relay_proto_v99"
down_revision: Union[str, None] = "0045_user_journey_state_v98"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("relay_protocol_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "relay_protocol_version")
