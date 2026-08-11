"""Approval surfaces must say what approval does.

`agnes pull` writes approved and required items into every analyst's
`.claude/rules/`, which Claude Code reads as project rules. An admin
approving an item is therefore also publishing an instruction, and until
now no surface said so. These tests pin the annotation on each surface an
approval can be issued from — the review queue, the single-item GET, the
single approve, and the batch endpoint the CLI drives.

Advisory throughout: every assertion here checks that the action still
succeeds and only that the caller is told what it just published.
"""

from __future__ import annotations

import json
import uuid

from src.db import get_system_db
from src.repositories.knowledge import KnowledgeRepository

# The note that prompted this, verbatim: an ordinary session recap.
DIRECTIVE_CONTENT = (
    "Next step is to type /exit and rerun claude from /srv so the marketplace "
    "and session hooks load, with recaps disabled in /config."
)
CLEAN_CONTENT = "Revenue excludes internal test accounts — join on account_id, never the display name."


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_item(content: str, *, status: str = "pending", title: str = "T") -> str:
    conn = get_system_db()
    item_id = "ki_" + uuid.uuid4().hex[:8]
    KnowledgeRepository(conn).create(
        id=item_id,
        title=title,
        content=content,
        category="engineering",
        status=status,
    )
    conn.close()
    return item_id


def _approve_audit_params(item_id: str) -> dict | None:
    conn = get_system_db()
    rows = conn.execute(
        "SELECT params FROM audit_log WHERE resource = ? AND action = 'corporate_memory.approve' ORDER BY timestamp",
        [item_id],
    ).fetchall()
    conn.close()
    return json.loads(rows[-1][0]) if rows and rows[-1][0] else None


class TestPendingQueue:
    def test_pending_row_carries_the_spans_that_read_as_orders(self, seeded_app):
        item_id = _create_item(DIRECTIVE_CONTENT)
        resp = seeded_app["client"].get(
            "/api/memory/admin/pending",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        row = next(i for i in body["items"] if i["id"] == item_id)
        kinds = {w["kind"] for w in row["delivery_warnings"]}
        assert {"slash_command", "session_control", "harness_config"} <= kinds
        assert all(w["excerpt"] for w in row["delivery_warnings"])

    def test_notice_is_sent_once_per_response_not_per_row(self, seeded_app):
        _create_item(DIRECTIVE_CONTENT)
        body = (
            seeded_app["client"]
            .get(
                "/api/memory/admin/pending",
                headers=_auth(seeded_app["admin_token"]),
            )
            .json()
        )
        assert ".claude/rules/" in body["delivery_notice"]
        assert "delivery_notice" not in body["items"][0]

    def test_ordinary_knowledge_is_annotated_as_clean(self, seeded_app):
        item_id = _create_item(CLEAN_CONTENT)
        body = (
            seeded_app["client"]
            .get(
                "/api/memory/admin/pending",
                headers=_auth(seeded_app["admin_token"]),
            )
            .json()
        )
        row = next(i for i in body["items"] if i["id"] == item_id)
        # Present and empty, not absent — a client can tell "scanned, clean"
        # from "this server does not scan".
        assert row["delivery_warnings"] == []


class TestSingleItemGet:
    def test_admin_get_item_carries_warnings(self, seeded_app):
        item_id = _create_item(DIRECTIVE_CONTENT)
        resp = seeded_app["client"].get(
            f"/api/memory/admin/{item_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["delivery_warnings"]


class TestApprove:
    def test_approve_still_approves_and_reports_what_it_published(self, seeded_app):
        item_id = _create_item(DIRECTIVE_CONTENT)
        resp = seeded_app["client"].post(
            f"/api/memory/admin/approve?item_id={item_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        # Advisory, not a gate: the item is approved either way.
        assert body["status"] == "approved"
        assert body["delivery_warnings"]
        assert ".claude/rules/" in body["delivery_notice"]

    def test_clean_approval_carries_no_notice(self, seeded_app):
        item_id = _create_item(CLEAN_CONTENT)
        body = (
            seeded_app["client"]
            .post(
                f"/api/memory/admin/approve?item_id={item_id}",
                headers=_auth(seeded_app["admin_token"]),
            )
            .json()
        )
        assert body["delivery_warnings"] == []
        assert body["delivery_notice"] is None

    def test_audit_row_records_the_count(self, seeded_app):
        """ "Who approved a note carrying agent directives" must be answerable."""
        item_id = _create_item(DIRECTIVE_CONTENT)
        seeded_app["client"].post(
            f"/api/memory/admin/approve?item_id={item_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        params = _approve_audit_params(item_id)
        assert params and params["delivery_warning_count"] >= 3


class TestBatch:
    def test_batch_approve_maps_warnings_by_item(self, seeded_app):
        dirty = _create_item(DIRECTIVE_CONTENT)
        clean = _create_item(CLEAN_CONTENT)
        resp = seeded_app["client"].post(
            "/api/memory/admin/batch",
            headers=_auth(seeded_app["admin_token"]),
            json={"item_ids": [dirty, clean], "action": "approve"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["success"]) == {dirty, clean}
        assert dirty in body["delivery_warnings"]
        # Clean items are omitted rather than mapped to [] — the CLI prints
        # one line per entry, so an empty entry would be a blank warning.
        assert clean not in body["delivery_warnings"]

    def test_mandate_delivers_too_and_is_reported(self, seeded_app):
        """Required items get their own km_<id>.md, so the same warning applies."""
        item_id = _create_item(DIRECTIVE_CONTENT, status="approved")
        body = (
            seeded_app["client"]
            .post(
                "/api/memory/admin/batch",
                headers=_auth(seeded_app["admin_token"]),
                json={"item_ids": [item_id], "action": "mandate"},
            )
            .json()
        )
        assert item_id in body["delivery_warnings"]

    def test_reject_reports_nothing_because_it_removes_from_the_channel(self, seeded_app):
        item_id = _create_item(DIRECTIVE_CONTENT)
        body = (
            seeded_app["client"]
            .post(
                "/api/memory/admin/batch",
                headers=_auth(seeded_app["admin_token"]),
                json={"item_ids": [item_id], "action": "reject"},
            )
            .json()
        )
        assert body["success"] == [item_id]
        assert "delivery_warnings" not in body

    def test_batch_approve_with_only_clean_items_sends_no_notice(self, seeded_app):
        item_id = _create_item(CLEAN_CONTENT)
        body = (
            seeded_app["client"]
            .post(
                "/api/memory/admin/batch",
                headers=_auth(seeded_app["admin_token"]),
                json={"item_ids": [item_id], "action": "approve"},
            )
            .json()
        )
        assert body["delivery_warnings"] == {}
        assert body["delivery_notice"] is None


class TestTheAllItemsTabSeesThemToo:
    """Devin Review on #1258: the second approval surface showed nothing.

    `/api/memory` backs the All Items tab, whose batch bar exposes Approve and
    Mark required — but the list carried no `delivery_warnings`, so the per-item
    strip and the notice never rendered there. A warning that appears on only
    one of the two surfaces an approval can be issued from does not do its job.
    """

    def test_the_list_annotates_rows_for_an_admin(self, seeded_app):
        _create_item(DIRECTIVE_CONTENT, status="approved", title="Recap")

        resp = seeded_app["client"].get("/api/memory", headers=_auth(seeded_app["admin_token"]))

        assert resp.status_code == 200, resp.text
        data = resp.json()
        rows = [it for it in data["items"] if it.get("title") == "Recap"]
        assert rows, data
        kinds = {w["kind"] for w in rows[0]["delivery_warnings"]}
        assert {"slash_command", "session_control", "harness_config"} <= kinds, rows[0]
        assert data.get("delivery_notice")

    def test_a_clean_list_carries_no_notice(self, seeded_app):
        _create_item(CLEAN_CONTENT, status="approved", title="Clean")

        data = seeded_app["client"].get("/api/memory", headers=_auth(seeded_app["admin_token"])).json()

        rows = [it for it in data["items"] if it.get("title") == "Clean"]
        assert rows and rows[0]["delivery_warnings"] == []
        assert data.get("delivery_notice") is None

    def test_a_non_admin_pays_for_no_scan(self, seeded_app):
        """Nobody else can approve, so nobody else needs the annotation."""
        _create_item(DIRECTIVE_CONTENT, status="approved", title="Recap")

        data = seeded_app["client"].get("/api/memory", headers=_auth(seeded_app["analyst_token"])).json()

        assert all("delivery_warnings" not in it for it in data["items"]), data["items"][:1]
        assert "delivery_notice" not in data


class TestTheBrowseTabSeesThemToo:
    """Devin Review on #1258, second round: three surfaces, not two.

    Browse cards carry the same approve actions, and `/api/memory/tree` fed
    them rows with no `delivery_warnings` — so the one tab an admin actually
    browses in showed nothing about what approval publishes.
    """

    def test_tree_rows_are_annotated_for_an_admin(self, seeded_app):
        _create_item(DIRECTIVE_CONTENT, status="approved", title="Recap")

        resp = seeded_app["client"].get("/api/memory/tree", headers=_auth(seeded_app["admin_token"]))

        assert resp.status_code == 200, resp.text
        data = resp.json()
        rows = [it for g in data["groups"] for it in g["items"] if it.get("title") == "Recap"]
        assert rows, data
        assert {w["kind"] for w in rows[0]["delivery_warnings"]} >= {"slash_command"}
        assert data.get("delivery_notice")

    def test_a_clean_tree_carries_no_notice(self, seeded_app):
        _create_item(CLEAN_CONTENT, status="approved", title="Clean")

        data = seeded_app["client"].get("/api/memory/tree", headers=_auth(seeded_app["admin_token"])).json()

        rows = [it for g in data["groups"] for it in g["items"] if it.get("title") == "Clean"]
        assert rows and rows[0]["delivery_warnings"] == []
        assert data.get("delivery_notice") is None

    def test_a_non_admin_pays_for_no_scan(self, seeded_app):
        _create_item(DIRECTIVE_CONTENT, status="approved", title="Recap")

        data = seeded_app["client"].get("/api/memory/tree", headers=_auth(seeded_app["analyst_token"])).json()

        rows = [it for g in data["groups"] for it in g["items"]]
        assert all("delivery_warnings" not in it for it in rows)
        assert data.get("delivery_notice") is None
