from src.demo_seed import seed_demo


def test_seed_demo_runs_all(system_db, monkeypatch):
    monkeypatch.setenv("SEED_DEMO", "1")
    seed_demo(system_db)
    assert system_db.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0] >= 20
    assert system_db.execute("SELECT COUNT(*) FROM metric_definitions").fetchone()[0] > 0
    assert system_db.execute(
        "SELECT COUNT(*) FROM data_packages WHERE slug = 'ecommerce-analytics'").fetchone()[0] == 1
    assert system_db.execute(
        "SELECT COUNT(*) FROM marketplace_registry WHERE url LIKE 'local:%'").fetchone()[0] == 1


def test_seed_demo_noop_when_disabled(system_db, monkeypatch):
    monkeypatch.delenv("SEED_DEMO", raising=False)
    seed_demo(system_db)
    assert system_db.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0] == 0


def test_seed_demo_idempotent(system_db, monkeypatch):
    monkeypatch.setenv("SEED_DEMO", "1")
    seed_demo(system_db)
    seed_demo(system_db)
    assert system_db.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0] >= 20
    assert system_db.execute(
        "SELECT COUNT(*) FROM data_packages WHERE slug = 'ecommerce-analytics'").fetchone()[0] == 1
