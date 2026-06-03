from src.demo_seed import seed_data_package


def test_seed_data_package_idempotent(system_db):
    seed_data_package(system_db)
    seed_data_package(system_db)
    rows = system_db.execute(
        "SELECT COUNT(*) FROM data_packages WHERE slug = 'ecommerce-analytics'").fetchone()[0]
    assert rows == 1


def test_seed_data_package_skips_missing_tables(system_db):
    # In the unit-test DB the demo tables are NOT registered → package is still
    # created, just with no attached tables (no crash).
    seed_data_package(system_db)
    pkg = system_db.execute(
        "SELECT id FROM data_packages WHERE slug = 'ecommerce-analytics'").fetchone()
    assert pkg is not None
