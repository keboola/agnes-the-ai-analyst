"""POST /api/scripts/run-due must write to audit_log."""
from src.db import get_system_db


def test_scripts_run_due_writes_audit_log(seeded_app, admin_user):
    c = seeded_app["client"]
    conn = get_system_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='script_runner.tick'"
    ).fetchone()[0]
    conn.close()

    resp = c.post("/api/scripts/run-due", headers=admin_user)
    assert resp.status_code in (200, 202)

    conn = get_system_db()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='script_runner.tick'"
    ).fetchone()[0]
    conn.close()
    assert after == before + 1
