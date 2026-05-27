"""v49+ feature tables — data_packages, memory_domains, recipes, etc.

Main added a cluster of tables between the fork point and the rebase
that the PG cutover branch was missing. Without them the data-packages
admin UI, memory-domains CRUD, recipe surface, and stack-subscription
opt-ins all fail at the first ``SELECT FROM <table>`` with
``UndefinedTable``.

Tables added:

  - ``data_packages``                — admin-curated package metadata
                                       (slug, status, category, owner,
                                       cover image, long description).
  - ``data_package_tables``          — package → table_registry M:N.
  - ``memory_domains``               — first-class domain entity
                                       replacing the legacy scalar
                                       ``knowledge_items.domain`` string.
  - ``knowledge_item_domains``       — knowledge_item → memory_domain M:N.
  - ``memory_domain_suggestions``    — non-admin users' new-domain
                                       suggestions awaiting admin review.
  - ``recipes``                      — admin-curated multi-table query
                                       templates.
  - ``user_stack_subscriptions``     — per-user opt-in for ``available``
                                       grants on ``data_package`` /
                                       ``memory_domain`` resource types.
  - ``setup_banner``                 — formerly-used singleton (kept as
                                       no-op for forward compat).

Revision: 0012
Revises:  0011_reconstructed_vis
"""
from alembic import op
import sqlalchemy as sa


revision = "0012_data_packages"
down_revision = "0011_reconstructed_vis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_packages",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("slug", sa.String, nullable=False, unique=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("icon", sa.String, nullable=True),
        sa.Column("color", sa.String, nullable=True),
        sa.Column("cover_image_url", sa.String, nullable=True),
        sa.Column("status", sa.String, server_default=sa.text("'prod'")),
        sa.Column("category", sa.String, nullable=True),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # v56 extended-content surface — all NULLABLE, all additive.
        sa.Column("owner_name", sa.String, nullable=True),
        sa.Column("owner_team", sa.String, nullable=True),
        sa.Column("tags", sa.String, nullable=True),
        sa.Column("long_description", sa.Text, nullable=True),
        sa.Column("when_to_use", sa.String, nullable=True),
        sa.Column("when_not_to_use", sa.String, nullable=True),
        sa.Column("example_questions", sa.String, nullable=True),
        sa.Column("created_by", sa.String, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    op.create_table(
        "data_package_tables",
        sa.Column(
            "package_id",
            sa.String,
            sa.ForeignKey("data_packages.id"),
            nullable=False,
        ),
        sa.Column(
            "table_id",
            sa.String,
            sa.ForeignKey("table_registry.id"),
            nullable=False,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("added_by", sa.String, nullable=True),
        sa.PrimaryKeyConstraint("package_id", "table_id"),
    )
    op.create_index(
        "idx_data_package_tables_table",
        "data_package_tables",
        ["table_id"],
    )

    op.create_table(
        "memory_domains",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("slug", sa.String, nullable=False, unique=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("icon", sa.String, nullable=True),
        sa.Column("color", sa.String, nullable=True),
        sa.Column("cover_image_url", sa.String, nullable=True),
        sa.Column("status", sa.String, server_default=sa.text("'prod'")),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("created_by", sa.String, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    op.create_table(
        "knowledge_item_domains",
        sa.Column(
            "item_id",
            sa.String,
            sa.ForeignKey("knowledge_items.id"),
            nullable=False,
        ),
        sa.Column(
            "domain_id",
            sa.String,
            sa.ForeignKey("memory_domains.id"),
            nullable=False,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("added_by", sa.String, nullable=True),
        sa.PrimaryKeyConstraint("item_id", "domain_id"),
    )
    op.create_index(
        "idx_knowledge_item_domains_domain",
        "knowledge_item_domains",
        ["domain_id"],
    )

    op.create_table(
        "memory_domain_suggestions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("rationale", sa.Text, nullable=True),
        sa.Column("status", sa.String, server_default=sa.text("'pending'")),
        sa.Column("created_by", sa.String, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("resolved_by", sa.String, nullable=True),
        sa.Column("resolution_note", sa.Text, nullable=True),
        sa.Column("created_domain_id", sa.String, nullable=True),
    )
    op.create_index(
        "idx_memory_domain_suggestions_status",
        "memory_domain_suggestions",
        ["status"],
    )

    op.create_table(
        "recipes",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("slug", sa.String, nullable=False, unique=True),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("icon", sa.String, nullable=True),
        sa.Column("color", sa.String, nullable=True),
        sa.Column("sql_template", sa.Text, nullable=True),
        # JSON list of related table_registry.id values
        sa.Column("related_table_ids", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String, server_default=sa.text("'prod'")),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("created_by", sa.String, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    op.create_table(
        "user_stack_subscriptions",
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("resource_type", sa.String, nullable=False),
        sa.Column("resource_id", sa.String, nullable=False),
        sa.Column(
            "subscribed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "resource_type", "resource_id"),
    )


def downgrade() -> None:
    op.drop_table("user_stack_subscriptions")
    op.drop_table("recipes")
    op.drop_index(
        "idx_memory_domain_suggestions_status",
        table_name="memory_domain_suggestions",
    )
    op.drop_table("memory_domain_suggestions")
    op.drop_index(
        "idx_knowledge_item_domains_domain",
        table_name="knowledge_item_domains",
    )
    op.drop_table("knowledge_item_domains")
    op.drop_table("memory_domains")
    op.drop_index(
        "idx_data_package_tables_table",
        table_name="data_package_tables",
    )
    op.drop_table("data_package_tables")
    op.drop_table("data_packages")
