"""v55+ table_registry + store_entities extended columns.

Prod DuckDB schema has columns the PG cutover branch was missing.
``_filtered_columns`` in ``scripts/migrate_duckdb_to_pg`` silently
dropped them with a WARNING — meaning every prod snapshot lost the
operator-curated content (``bq_fqn``, ``sample_questions``,
``things_to_know``, etc.) without any explicit signal to the user.
Worse, ``RegisterTableRequest`` already accepts ``bq_fqn`` from the
admin API but had no column to persist it into, so POST silently
discarded the field.

Columns added:

  table_registry:
    bq_fqn            — operator-supplied fully-qualified BQ name
                        for materialized rows that don't have a
                        clean (bucket, source_table) projection.
    sample_questions  — JSONB list of analyst questions surfaced
                        on the package detail page.
    things_to_know    — markdown notes shown above the schema panel.
    pairs_well_with   — JSONB list of related table ids.
    grain             — describes the row grain ("one row per
                        session", "one row per order event", etc.).
    platforms         — comma/JSON list of source platforms.
    partition_col     — partition column name (BQ).
    history           — markdown body — historical evolution of
                        the dataset.
    gotchas           — markdown body — known data caveats.

  store_entities:
    title          — human-readable display title (separate from
                     ``name`` which is the slug).
    tagline        — one-line description.
    synthetic_name — generated slug "your-name-by-<author>" used
                     when ``name`` is left blank on upload.

All NULLABLE + additive; no behaviour change for existing rows.

Revision: 0013
Revises:  0012_data_packages
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0013_v55_extended"
down_revision = "0012_data_packages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # table_registry — admin-curated documentation surface.
    op.add_column("table_registry", sa.Column("bq_fqn", sa.String, nullable=True))
    op.add_column("table_registry", sa.Column("sample_questions", JSONB, nullable=True))
    op.add_column("table_registry", sa.Column("things_to_know", sa.Text, nullable=True))
    op.add_column("table_registry", sa.Column("pairs_well_with", JSONB, nullable=True))
    op.add_column("table_registry", sa.Column("grain", sa.String, nullable=True))
    op.add_column("table_registry", sa.Column("platforms", sa.String, nullable=True))
    op.add_column("table_registry", sa.Column("partition_col", sa.String, nullable=True))
    op.add_column("table_registry", sa.Column("history", sa.Text, nullable=True))
    op.add_column("table_registry", sa.Column("gotchas", sa.Text, nullable=True))

    # store_entities — display-layer fields (title vs slug).
    op.add_column("store_entities", sa.Column("title", sa.String, nullable=True))
    op.add_column("store_entities", sa.Column("tagline", sa.String, nullable=True))
    op.add_column("store_entities", sa.Column("synthetic_name", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("store_entities", "synthetic_name")
    op.drop_column("store_entities", "tagline")
    op.drop_column("store_entities", "title")

    op.drop_column("table_registry", "gotchas")
    op.drop_column("table_registry", "history")
    op.drop_column("table_registry", "partition_col")
    op.drop_column("table_registry", "platforms")
    op.drop_column("table_registry", "grain")
    op.drop_column("table_registry", "pairs_well_with")
    op.drop_column("table_registry", "things_to_know")
    op.drop_column("table_registry", "sample_questions")
    op.drop_column("table_registry", "bq_fqn")
