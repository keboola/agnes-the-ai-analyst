"""GET /api/attachments/{source}/{attachment_id}/download — the generic
connector-catalogued attachment download surface (Jira first).

Covers the acceptance list: RBAC 403 via the catalogue table, byte-for-byte
success, misses distinguishable from denials (`attachment_not_found` vs
`attachment_not_stored` vs 403), unknown source 404, tampered-path
containment, audit rows for granted AND denied fetches, and a second source
registering with no route change.
"""

from pathlib import Path

import pytest

from src.attachment_sources import _SOURCES, AttachmentSource
from src.db import get_system_db
from tests.conftest import create_mock_extract


def _grant_table(user_id: str, table_id: str, group_name: str) -> None:
    from tests.conftest import grant_table_via_package

    conn = get_system_db()
    try:
        grant_table_via_package(conn, table_id, user_id, group_name=group_name)
    finally:
        conn.close()


def _last_audit_row():
    conn = get_system_db()
    try:
        return conn.execute(
            "SELECT user_id, action, resource, result, params FROM audit_log "
            "WHERE action='attachment.download' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


@pytest.fixture
def jira_attachment_env(seeded_app, monkeypatch):
    """A registered + extracted `attachments` catalogue, a real file on
    disk under the permitted root, and Config.JIRA_DATA_DIR repointed at it.

    Rows: id 101 → stored file, id 102 → empty local_path (transform-time
    miss), id 103 → file since removed, id 104 → tampered path escaping the
    root (the target file EXISTS, so only containment can refuse it).
    """
    from connectors.jira.service import Config

    env = seeded_app["env"]
    c = seeded_app["client"]
    jira_data_dir = env["data_dir"] / "src_data" / "raw" / "jira"
    root = jira_data_dir / "attachments"
    (root / "SUP-1").mkdir(parents=True)
    monkeypatch.setattr(Config, "JIRA_DATA_DIR", jira_data_dir)

    stored = root / "SUP-1" / "101_report.pdf"
    payload = b"%PDF-1.4 attachment bytes for the roundtrip test"
    stored.write_bytes(payload)

    secret = env["data_dir"] / "secret.txt"
    secret.write_bytes(b"never served")

    create_mock_extract(
        env["extracts_dir"],
        "jira",
        [
            {
                "name": "attachments",
                "data": [
                    {
                        "attachment_id": "101",
                        "issue_key": "SUP-1",
                        "filename": "quarterly report.pdf",
                        "local_path": str(stored),
                    },
                    {
                        "attachment_id": "102",
                        "issue_key": "SUP-1",
                        "filename": "huge.zip",
                        "local_path": "",
                    },
                    {
                        "attachment_id": "103",
                        "issue_key": "SUP-1",
                        "filename": "gone.png",
                        "local_path": str(root / "SUP-1" / "103_gone.png"),
                    },
                    {
                        "attachment_id": "104",
                        "issue_key": "SUP-1",
                        "filename": "secret.txt",
                        "local_path": str(secret),
                    },
                ],
            }
        ],
    )
    from src.orchestrator import SyncOrchestrator

    SyncOrchestrator().rebuild()

    resp = c.post(
        "/api/admin/register-table",
        json={"name": "attachments", "source_type": "jira", "query_mode": "local"},
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert resp.status_code == 201

    return {**seeded_app, "payload": payload, "root": root}


def test_unknown_source_is_404(seeded_app, analyst_user):
    resp = seeded_app["client"].get("/api/attachments/nope/1/download", headers=analyst_user)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "unknown_attachment_source"


def test_denied_without_table_access_and_audited(jira_attachment_env, analyst_user):
    resp = jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=analyst_user)
    assert resp.status_code == 403
    # The refusal is the catalogue table's RBAC message — a client can tell
    # this apart from every 404 miss below.
    assert "attachments" in str(resp.json()["detail"])
    row = _last_audit_row()
    assert row is not None
    assert row[0] == "analyst1"
    # `denied`, not an error spelling: the audit read layer
    # (src/audit_helpers.py) buckets `error%` as malfunctions, and an RBAC
    # refusal is correct policy — it must not inflate the error rate.
    assert row[3] == "denied"


def test_granted_fetch_streams_the_same_bytes(jira_attachment_env, analyst_user):
    _grant_table("analyst1", "attachments", "att-ok")
    resp = jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=analyst_user)
    assert resp.status_code == 200
    assert resp.content == jira_attachment_env["payload"]
    # The advertised length must equal the body actually delivered (it comes
    # from fstat of the descriptor being streamed).
    assert resp.headers["content-length"] == str(len(jira_attachment_env["payload"]))
    cd = resp.headers.get("content-disposition", "")
    # The catalogue's `filename` (what Jira shows), not the on-disk
    # "<id>_<name>" — RFC 5987-encoded because of the space.
    assert "quarterly%20report.pdf" in cd
    assert "101_" not in cd
    row = _last_audit_row()
    assert row[0] == "analyst1"
    assert row[3] == "success"
    assert str(len(jira_attachment_env["payload"])) in (row[4] or "")


def test_admin_god_mode_passes_the_table_gate(jira_attachment_env, admin_user):
    resp = jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=admin_user)
    assert resp.status_code == 200
    assert resp.content == jira_attachment_env["payload"]


def test_server_only_catalogue_table_keeps_its_binaries(jira_attachment_env, analyst_user, admin_user):
    """`server_only` is the admin's "these bytes do not leave the server"
    lever; the parquet download honours it (`_distribution_refusal`,
    app/api/data.py) and this route must not be the way around it. Asserted
    WITH table access granted — the gate is a property of the table, not an
    authorization check — and for admin god-mode too, exactly like the
    parquet route. Audited as `denied` (policy refusal, not malfunction)."""
    _grant_table("analyst1", "attachments", "att-so")
    c = jira_attachment_env["client"]
    conn = get_system_db()
    try:
        conn.execute("UPDATE table_registry SET server_only = TRUE WHERE id = 'attachments'")
    finally:
        conn.close()

    resp = c.get("/api/attachments/jira/101/download", headers=analyst_user)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "attachment_table_server_only"
    row = _last_audit_row()
    assert row is not None
    assert row[3] == "denied"

    resp = c.get("/api/attachments/jira/101/download", headers=admin_user)
    assert resp.status_code == 403, "server_only is a table property — god-mode does not bypass it"


def test_unknown_id_is_not_found(jira_attachment_env, analyst_user):
    _grant_table("analyst1", "attachments", "att-noid")
    resp = jira_attachment_env["client"].get("/api/attachments/jira/99999/download", headers=analyst_user)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "attachment_not_found"


@pytest.mark.parametrize(
    "attachment_id",
    ["102", "103", "104"],
    ids=["no-path-recorded", "file-removed-since", "path-escapes-root"],
)
def test_no_stored_bytes_is_distinguishable(jira_attachment_env, analyst_user, attachment_id):
    """Empty path, vanished file, and a tampered path that escapes the
    permitted root all answer `attachment_not_stored` — a client falls back
    to the upstream API for these, and the escape is never served."""
    _grant_table("analyst1", "attachments", f"att-miss-{attachment_id}")
    resp = jira_attachment_env["client"].get(f"/api/attachments/jira/{attachment_id}/download", headers=analyst_user)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "attachment_not_stored"
    assert b"never served" not in resp.content


def test_second_source_needs_no_route_change(seeded_app, analyst_user, monkeypatch):
    """Registering another declaration makes the same route + CLI path serve
    it — the acceptance criterion for source-parameterized reuse."""
    env = seeded_app["env"]
    c = seeded_app["client"]
    root = env["data_dir"] / "zendesk_files"
    root.mkdir()
    stored = root / "7_export.csv"
    stored.write_bytes(b"a,b\n1,2\n")

    create_mock_extract(
        env["extracts_dir"],
        "zendesk",
        [
            {
                "name": "zendesk_attachments",
                "data": [{"file_id": "7", "path": str(stored)}],
            }
        ],
    )
    from src.orchestrator import SyncOrchestrator

    SyncOrchestrator().rebuild()

    resp = c.post(
        "/api/admin/register-table",
        json={"name": "zendesk_attachments", "source_type": "keboola", "query_mode": "local"},
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert resp.status_code == 201

    monkeypatch.setitem(
        _SOURCES,
        "zendesk",
        AttachmentSource(
            source="zendesk",
            table="zendesk_attachments",
            id_column="file_id",
            path_column="path",
            root=lambda: root,
        ),
    )
    _grant_table("analyst1", "zendesk_attachments", "att-zd")
    resp = c.get("/api/attachments/zendesk/7/download", headers=analyst_user)
    assert resp.status_code == 200
    assert resp.content == b"a,b\n1,2\n"


class TestOpenContained:
    """Unit coverage of the containment guard, per the security playbook."""

    def _root(self, tmp_path: Path) -> Path:
        root = tmp_path / "attachments"
        root.mkdir()
        return root

    def test_rejects_traversal_and_unsafe_values(self, tmp_path):
        from app.api.attachments import _open_contained

        root = self._root(tmp_path)
        (tmp_path / "secret.txt").write_bytes(b"x")
        for stored in [
            str(tmp_path / "secret.txt"),  # absolute escape
            "../secret.txt",  # relative escape
            "a/../../secret.txt",
            "a\\b.txt",  # backslash
            "a\x00b",  # NUL
        ]:
            fh, st, reason = _open_contained(root, stored)
            assert fh is None, stored
            assert st is None, stored
            assert reason == "path_rejected", stored

    def test_null_and_missing_are_distinct_reasons(self, tmp_path):
        from app.api.attachments import _open_contained

        root = self._root(tmp_path)
        assert _open_contained(root, None) == (None, None, "no_path_recorded")
        assert _open_contained(root, "") == (None, None, "no_path_recorded")
        assert _open_contained(root, str(root / "nope.png")) == (None, None, "file_missing")
        # A directory is not servable bytes either.
        assert _open_contained(root, str(root)) == (None, None, "file_missing")

    def test_accepts_absolute_and_relative_inside_root(self, tmp_path):
        from app.api.attachments import _open_contained

        root = self._root(tmp_path)
        (root / "SUP-1").mkdir()
        f = root / "SUP-1" / "1_a.png"
        f.write_bytes(b"ok")
        for stored in (str(f), "SUP-1/1_a.png"):
            fh, st, reason = _open_contained(root, stored)
            assert fh is not None and fh.read() == b"ok"
            fh.close()
            assert st is not None and st.st_size == 2
            assert reason == ""

    def test_fstat_is_of_the_opened_descriptor(self, tmp_path):
        """The advertised size must describe the inode being streamed: an
        atomic re-publish (os.replace) after open must not change what this
        descriptor reads or the size already fstat'd."""
        import os

        from app.api.attachments import _open_contained

        root = self._root(tmp_path)
        f = root / "1_a.bin"
        f.write_bytes(b"old-bytes")
        fh, st, reason = _open_contained(root, str(f))
        assert reason == "" and st.st_size == 9
        # connector-style atomic rewrite while the response is in flight
        tmp = root / "1_a.bin.tmp-1"
        tmp.write_bytes(b"NEW")
        os.replace(tmp, f)
        assert fh.read() == b"old-bytes"  # complete old file, matching st
        fh.close()


class TestDeclarationMatchesConnector:
    """Pin the declaration to what the connector actually emits — the
    blocker class this guards against is a declaration naming a table that
    resolves nowhere: master views are named verbatim from _meta.table_name,
    and the Jira connector's table names are UNPREFIXED."""

    def test_jira_table_is_a_real_connector_table(self):
        from connectors.jira.extract_init import JIRA_TABLES
        from src.attachment_sources import get_attachment_source

        decl = get_attachment_source("jira")
        assert decl.table in JIRA_TABLES, (
            f"declared catalogue table {decl.table!r} is not one the Jira "
            f"connector emits ({JIRA_TABLES}) — it would resolve to no view"
        )

    def test_jira_columns_exist_in_the_transform_output(self, tmp_path):
        from connectors.jira.transform import transform_attachments
        from src.attachment_sources import get_attachment_source

        decl = get_attachment_source("jira")
        raw = {
            "key": "SUP-1",
            "fields": {"attachment": [{"id": "1", "filename": "a.png", "author": None}]},
        }
        records = transform_attachments(raw, attachments_dir=tmp_path)
        assert records, "synthetic payload produced no attachment record"
        cols = set(records[0])
        declared = {decl.id_column, decl.path_column, decl.filename_column}
        assert declared <= cols, f"declared columns {declared - cols} missing from {sorted(cols)}"
