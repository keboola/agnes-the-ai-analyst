"""Two orchestrator rebuilds running concurrently must serialize.

Mirrors ``tests/db_pg/test_seed_lease_contract.py`` — uses the repo's
existing PG test fixture (``pg_engine`` from ``tests/db_pg/conftest.py``).
"""

import threading
import time

from src.db_pg import rebuild_lease


def test_rebuild_lease_serializes(pg_engine, monkeypatch):
    # Point src.db_pg's process-wide singleton at the fixture's pgserver
    # instance (same pattern the ``state_backend`` fixture in conftest.py
    # uses) and force the PG lease path regardless of the ambient backend.
    import src.db_pg as db_pg

    monkeypatch.setenv("DATABASE_URL", str(pg_engine.url))
    monkeypatch.setattr(db_pg, "_lease_use_pg", lambda: True)
    db_pg.dispose()

    order: list[str] = []

    def hold():
        with rebuild_lease():
            order.append("first-in")
            time.sleep(0.5)
            order.append("first-out")

    def contend():
        time.sleep(0.1)
        with rebuild_lease():
            order.append("second-in")

    try:
        t1, t2 = threading.Thread(target=hold), threading.Thread(target=contend)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    finally:
        db_pg.dispose()

    assert order == ["first-in", "first-out", "second-in"]


def test_rebuild_lease_noop_on_duckdb(monkeypatch):
    monkeypatch.setattr("src.db_pg._lease_use_pg", lambda: False)
    with rebuild_lease():
        pass  # must not require a PG connection
