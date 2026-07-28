"""Seed table_registry with dummy entries across multiple buckets.

Used to exercise the /admin/access UI with the new ResourceType.TABLE
without depending on a real data source. Each entry is registered with
default RBAC (no `is_public` bypass — that column was dropped in v19),
so per-group grants are required for analyst visibility.

Idempotent — TableRegistryRepository.register() does an UPSERT via
ON CONFLICT, so re-running this script just refreshes the rows.

Usage:
    python scripts/seed_dummy_tables.py
"""

from __future__ import annotations

from src.db import get_system_db
from src.repositories.table_registry import TableRegistryRepository


# (bucket, table_id, name, description)
DUMMY_TABLES: list[tuple[str, str, str, str]] = [
    # Finance
    ("in.c-finance", "in_c_finance_orders_dummy",
     "orders_dummy", "Dummy orders fact table — one row per order."),
    ("in.c-finance", "in_c_finance_revenue_daily_dummy",
     "revenue_daily_dummy", "Dummy daily revenue rollup."),
    ("in.c-finance", "in_c_finance_customers_dummy",
     "customers_dummy", "Dummy customer dimension."),
    ("in.c-finance", "in_c_finance_transactions_dummy",
     "transactions_dummy", "Dummy payment transactions."),
    # Marketing
    ("in.c-marketing", "in_c_marketing_campaigns_dummy",
     "campaigns_dummy", "Dummy marketing campaigns metadata."),
    ("in.c-marketing", "in_c_marketing_ad_spend_dummy",
     "ad_spend_dummy", "Dummy ad spend by channel and day."),
    ("in.c-marketing", "in_c_marketing_channels_dummy",
     "channels_dummy", "Dummy marketing channel dimension."),
    ("in.c-marketing", "in_c_marketing_attributions_dummy",
     "attributions_dummy", "Dummy multi-touch attributions."),
    # Product
    ("in.c-product", "in_c_product_events_dummy",
     "events_dummy", "Dummy product event stream."),
    ("in.c-product", "in_c_product_sessions_dummy",
     "sessions_dummy", "Dummy user session aggregates."),
    ("in.c-product", "in_c_product_features_dummy",
     "features_dummy", "Dummy feature flag exposure log."),
    ("in.c-product", "in_c_product_releases_dummy",
     "releases_dummy", "Dummy release/deploy timeline."),
]


def main() -> None:
    conn = get_system_db()
    try:
        repo = TableRegistryRepository(conn)
        before = len(repo.list_all())
        for bucket, table_id, name, description in DUMMY_TABLES:
            repo.register(
                id=table_id,
                name=name,
                source_type="dummy",
                bucket=bucket,
                source_table=name,
                query_mode="local",
                description=description,
                registered_by="seed_dummy_tables",
                profile_after_sync=False,
            )
        after = len(repo.list_all())
        bucket_count = len({b for b, _, _, _ in DUMMY_TABLES})
        print(
            f"Seeded {len(DUMMY_TABLES)} tables across {bucket_count} buckets "
            f"(registry: {before} -> {after} rows)."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
