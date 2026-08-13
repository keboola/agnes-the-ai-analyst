"""knowledge_item_domains backfill from the legacy domain scalar (data-only).

Devin Review on #1290 (list_by_domain junction fix): PG's ``create()`` /
``update()`` did not start writing ``knowledge_item_domains`` until #1263
(``5cc4901a6``) — the junction table itself was created three days after the
scalar ``knowledge_items.domain`` column in ``0011_data_packages`` (2026-05-27
vs. 2026-05-24, ``0010_knowledge``), and dual-writing to it began only with
#1263, months later. Any row created in that window carries a scalar
``domain`` with no matching junction row, so #1290's junction-only
``list_by_domain`` (and any other junction-only read) can no longer find it.

No DuckDB counterpart is needed: DuckDB's v49 migration
(``src/db.py::_v51_to_v52``, SCHEMA_VERSION 52) introduced the junction and
DROPPED the scalar column in the SAME migration, backfilling every
historical (item, domain) pair from the scalar at that moment (steps 5/6/9b
— see that function's docstring) before the column disappeared. There was
never a window on DuckDB where the junction existed but the write path
didn't populate it, and going forward no code path can write a domain
without it (the column is gone). PG kept the scalar column indefinitely, so
its junction-adoption landed as a separate, later commit — the real gap
this migration closes. ``SCHEMA_VERSION`` in ``src/db.py`` is intentionally
left untouched by this change.

Mirrors ``_v51_to_v52``'s step 5 for domain resolution: a pre-#1263
``create()`` had no slug validation at all (any free-text string went
straight into the scalar column), so a legacy value may not match any
existing ``memory_domains.slug``. Missing domains are created here (slug
normalized the same way: lowercase, non-alnum runs collapsed to ``-``)
rather than silently dropping the item's domain membership.

Revision ID: 0062_knowledge_domains_backfill
Revises: 0061_agent_status_backfill_v115
Create Date: 2026-08-13

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0062_knowledge_domains_backfill"
down_revision: Union[str, None] = "0061_agent_status_backfill_v115"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Create a memory_domains row for any legacy scalar value that has no
    #    matching slug yet (defensive — same reasoning as _v51_to_v52 step 5).
    op.execute(
        r"""
        INSERT INTO memory_domains (id, slug, name, created_at, updated_at)
        SELECT
            'md_' || lower(regexp_replace(d.domain, '[^a-z0-9]+', '_', 'g')),
            lower(regexp_replace(d.domain, '[^a-z0-9]+', '-', 'g')),
            d.domain,
            now(), now()
          FROM (
              SELECT DISTINCT domain FROM knowledge_items
               WHERE domain IS NOT NULL AND domain <> ''
          ) d
         WHERE NOT EXISTS (
             SELECT 1 FROM memory_domains md
              WHERE md.slug = lower(regexp_replace(d.domain, '[^a-z0-9]+', '-', 'g'))
         )
        ON CONFLICT (slug) DO NOTHING
        """
    )

    # 2) Backfill the junction row for every item whose scalar domain has no
    #    matching knowledge_item_domains entry yet.
    op.execute(
        r"""
        INSERT INTO knowledge_item_domains (item_id, domain_id, added_by, added_at)
        SELECT ki.id, md.id, 'system', now()
          FROM knowledge_items ki
          JOIN memory_domains md
            ON md.slug = lower(regexp_replace(ki.domain, '[^a-z0-9]+', '-', 'g'))
         WHERE ki.domain IS NOT NULL AND ki.domain <> ''
        ON CONFLICT (item_id, domain_id) DO NOTHING
        """
    )


def downgrade() -> None:
    # Not safely invertible: nothing distinguishes a junction row this
    # backfill wrote from one written by ordinary create()/update() traffic
    # afterward (same reasoning as 0061_agent_status_backfill_v115).
    pass
