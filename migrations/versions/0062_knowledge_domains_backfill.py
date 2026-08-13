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

**The scalar is not authoritative — the junction is.** Both steps below
touch ONLY items that have no ``knowledge_item_domains`` row at all.
``MemoryDomainsPgRepository.replace_domains_for_item`` — the admin
item-edit modal's ``domain_ids`` chip-input, and since #1263 the only
UI path to an item's domains — rewrites the junction and deliberately
never touches ``knowledge_items.domain``. An item created under
``q-team`` and later moved to ``ops`` therefore carries a stale scalar
``q-team`` forever, and a backfill keyed on the scalar alone would file
it back under ``q-team`` on upgrade: the admin's move, silently undone,
and by construction the very stale-scalar membership #1290 stopped
honouring. An item with any junction row has been spoken for; leave it.

Residual ambiguity, stated plainly: for an item with a non-empty scalar
and ZERO junction rows the data cannot tell "never migrated" (the cohort
this migration exists for) from "an admin deliberately emptied it" —
``replace_domains_for_item(item, [])`` deletes the rows and writes
nothing back, and ``hard_delete`` of a domain cascades its rows away, so
all three end in byte-identical state. There is no timestamp, flag or
tombstone that separates them: the clear path leaves nothing behind, and
``knowledge_items.updated_at`` moves for unrelated edits too. Skipping
the whole ambiguous set is not the conservative option — it is deleting
the migration, since the target cohort lives entirely inside it. So the
backfill runs, but every row it writes is stamped ``added_by =
'migration_0062'`` (and every domain it mints ``created_by =
'migration_0062'``), which makes the write attributable in the admin UI
and lets ``downgrade()`` take back exactly it and nothing else. An
operator who lands on the wrong side of the ambiguity has a one-command
undo instead of a silent, permanent clobber.

``lower()`` is applied INSIDE ``regexp_replace``, not around it: Postgres'
``regexp_replace`` is case-sensitive, so ``[^a-z0-9]`` matches uppercase
letters too. Normalizing the raw value would turn a perfectly ordinary
legacy ``'Finance'`` into ``'-inance'`` — a junk ``memory_domains`` row
with every such item filed under it instead of the real domain, and
``downgrade()`` below cannot take it back.

The normalized value is also ``btrim``-med of leading/trailing ``-`` for
the same reason: the collapse step turns surrounding whitespace or
punctuation into edge separators (``' finance'`` → ``'-finance'``,
``'(Ops)'`` → ``'-ops-'``), which would again miss the real slug and mint
a junk domain. A value that normalizes to nothing at all (``'   '``
passes the ``<> ''`` guard but trims to ``''``) is excluded outright —
there is no domain to resolve it to and nothing sane to mint.

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


#: Stamped on every row this migration writes so ``downgrade()`` can take
#: back exactly its own writes, and so an operator auditing a surprise
#: membership can see where it came from. Ordinary create()/update() and
#: replace_domains_for_item traffic overwrite it naturally.
MARKER = "migration_0062"

#: Items this backfill is allowed to touch: a non-empty legacy scalar AND no
#: junction row of any kind. Anything with a junction row has already been
#: spoken for by the post-#1263 write path or by an admin's explicit
#: multi-domain edit, and the scalar on it may be stale by design.
_UNMIGRATED = """
    ki.domain IS NOT NULL AND ki.domain <> ''
    AND NOT EXISTS (
        SELECT 1 FROM knowledge_item_domains kid WHERE kid.item_id = ki.id
    )
"""

#: One normalization expression used by BOTH steps (they must stay identical
#: or step 2's join misses the row step 1 minted): lowercase first (PG's
#: regex classes are case-sensitive), collapse non-alnum runs to '-', then
#: btrim the edge separators that surrounding whitespace/punctuation leaves
#: behind (' finance' → 'finance', '(Ops)' → 'ops'). A whitespace-only
#: value trims to '' and is excluded by the ``<> ''`` guards below.
_SLUG = "btrim(regexp_replace(lower(ki.domain), '[^a-z0-9]+', '-', 'g'), '-')"


def upgrade() -> None:
    # 1) Create a memory_domains row for any legacy scalar value that has no
    #    matching slug yet (defensive — same reasoning as _v51_to_v52 step 5).
    #    Scanned over the SAME cohort step 2 backfills: minting from the raw
    #    column would invent a grantable, browsable domain out of the stale
    #    scalar of an item this migration is not going to touch anyway.
    #
    #    The grouping key is the NORMALIZED slug, not the raw value:
    #    normalization is many-to-one ('Finance' / 'finance' / 'FINANCE', and
    #    'q team' / 'q-team', all collapse to one slug), and the generated
    #    `id` collapses with it, so a raw-value DISTINCT would feed several
    #    rows carrying the same slug AND the same primary key into one
    #    INSERT. `ON CONFLICT (slug)` names only the slug arbiter and in any
    #    case does not deduplicate rows within a single statement, so that
    #    shape aborts the migration on the `memory_domains_pkey` violation.
    #    min(domain) picks one raw spelling to keep as the display name,
    #    deterministically.
    op.execute(
        rf"""
        INSERT INTO memory_domains (id, slug, name, created_by, created_at, updated_at)
        SELECT
            'md_' || replace(d.slug, '-', '_'),
            d.slug,
            d.name,
            '{MARKER}',
            now(), now()
          FROM (
              SELECT {_SLUG} AS slug,
                     min(btrim(ki.domain)) AS name
                FROM knowledge_items ki
               WHERE {_UNMIGRATED}
                 AND {_SLUG} <> ''
               GROUP BY {_SLUG}
          ) d
         WHERE NOT EXISTS (
             SELECT 1 FROM memory_domains md WHERE md.slug = d.slug
         )
        ON CONFLICT (slug) DO NOTHING
        """
    )

    # 2) Backfill the junction row for every unmigrated item — and ONLY those.
    #    An item that already carries a junction row is left exactly as it is,
    #    however stale its scalar column reads.
    op.execute(
        rf"""
        INSERT INTO knowledge_item_domains (item_id, domain_id, added_by, added_at)
        SELECT ki.id, md.id, '{MARKER}', now()
          FROM knowledge_items ki
          JOIN memory_domains md
            ON md.slug = {_SLUG}
         WHERE {_UNMIGRATED}
           AND {_SLUG} <> ''
        ON CONFLICT (item_id, domain_id) DO NOTHING
        """
    )


def downgrade() -> None:
    # Invertible for exactly the rows this migration wrote, and no others:
    # the marker is overwritten the moment ordinary create()/update() or
    # replace_domains_for_item traffic rewrites an item's membership, so a
    # surviving marked row is one nothing has touched since the upgrade.
    op.execute(f"DELETE FROM knowledge_item_domains WHERE added_by = '{MARKER}'")
    # Domains minted in step 1 are deliberately NOT dropped: an admin may
    # have granted, renamed or filled one in the meantime, and a domain with
    # no items is inert. They stay identifiable by created_by = MARKER.
