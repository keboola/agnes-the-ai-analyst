"""baseline

Establishes the start of the Postgres migration chain. Intentionally
empty: every subsequent revision builds on top.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-24

"""
from __future__ import annotations

from typing import Sequence, Union


revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No-op. The empty baseline is the chain's anchor; every meaningful
    # table is added by a later revision.
    pass


def downgrade() -> None:
    pass
