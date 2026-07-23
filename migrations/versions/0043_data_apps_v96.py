"""data_apps registry (hosted user web apps)

Mirrors DuckDB ``_v95_to_v96``. Adds the ``data_apps`` table: the registry
of hosted user web apps (Task 1 of the Data Apps feature) — slug/name/
owner, repo source, deploy state, runtime limits, and idle-sleep policy.

Revision ID: 0043_data_apps_v96
Revises: 0042_usage_summary_idx_fix_v95
Create Date: 2026-07-21

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043_data_apps_v96"
down_revision: Union[str, None] = "0042_usage_summary_idx_fix_v95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "data_apps",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("repo_mode", sa.String(), nullable=False, server_default="internal"),
        sa.Column("repo_url", sa.String(), server_default=""),
        sa.Column("repo_branch", sa.String(), server_default="main"),
        sa.Column("deployed_sha", sa.String(), server_default=""),
        sa.Column("runtime_tag", sa.String(), server_default=""),
        sa.Column("state", sa.String(), nullable=False, server_default="created"),
        sa.Column("state_detail", sa.Text(), server_default=""),
        sa.Column("secrets_enc", sa.Text(), server_default=""),
        sa.Column("env", sa.Text(), server_default="{}"),
        sa.Column("cpu_limit", sa.String(), server_default=""),
        sa.Column("mem_limit", sa.String(), server_default=""),
        sa.Column("idle_timeout_s", sa.Integer(), server_default="1800"),
        sa.Column("sleep_mode", sa.String(), server_default="recreate"),
        sa.Column("service_token_id", sa.String(), server_default=""),
        sa.Column("last_request_at", sa.TIMESTAMP()),
        sa.Column("last_deploy_at", sa.TIMESTAMP()),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("data_apps")
