"""POST /api/upload/sessions must write to audit_log; filename is sanitized."""
import io
import json
from src.db import get_system_db


def test_upload_sessions_writes_audit_log(seeded_app, analyst_user):
    c = seeded_app["client"]
    conn = get_system_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='upload.session'"
    ).fetchone()[0]
    conn.close()

    jsonl = b'{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}\n'
    files = {"file": ("sess-test.jsonl", io.BytesIO(jsonl), "application/x-ndjson")}
    resp = c.post("/api/upload/sessions", files=files, headers=analyst_user)
    assert resp.status_code == 200

    conn = get_system_db()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='upload.session'"
    ).fetchone()[0]
    row = conn.execute(
        "SELECT params FROM audit_log WHERE action='upload.session' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert after == before + 1
    params = json.loads(row[0]) if row[0] else {}
    assert "filename" in params
    assert "bytes" in params


def test_upload_sessions_rejects_dangerous_filename(seeded_app, analyst_user):
    """Conventions sanitization rule #3 — filename limited to [A-Za-z0-9._-]."""
    c = seeded_app["client"]
    jsonl = b'{"role":"user","content":"x"}\n'
    files = {"file": ("<script>alert(1)</script>.jsonl", io.BytesIO(jsonl), "application/x-ndjson")}
    resp = c.post("/api/upload/sessions", files=files, headers=analyst_user)
    assert resp.status_code == 400
    assert "filename" in resp.text.lower()
