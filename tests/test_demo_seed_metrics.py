from src.demo_seed import seed_metrics


def test_seed_metrics_idempotent(system_db):
    n1 = seed_metrics(system_db)
    assert n1 > 0                          # imported the bundled docs/metrics
    seed_metrics(system_db)                # second run must not error or duplicate
    cnt = system_db.execute("SELECT COUNT(*) FROM metric_definitions").fetchone()[0]
    assert cnt == n1
