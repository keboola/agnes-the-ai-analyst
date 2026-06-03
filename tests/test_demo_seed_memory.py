from src.demo_seed import seed_memory


def test_seed_memory_idempotent(system_db):
    seed_memory(system_db)
    first = system_db.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0]
    seed_memory(system_db)
    second = system_db.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0]
    assert first >= 20 and second == first


def test_seed_memory_creates_domains_and_marks_status(system_db):
    seed_memory(system_db)
    domains = system_db.execute("SELECT COUNT(*) FROM memory_domains").fetchone()[0]
    assert domains == 6
    approved = system_db.execute(
        "SELECT COUNT(*) FROM knowledge_items WHERE status = 'approved'").fetchone()[0]
    assert approved >= 20
