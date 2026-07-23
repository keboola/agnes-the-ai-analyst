"""The BigQuery Edit modal must surface and recompose ``bq_fqn``.

PR #990 gave the Register modal a "Project" field that composes ``bq_fqn``
(``project.dataset.table``) for Live-access rows. The Edit modal never grew
the same field: ``_openEditBqModal`` populated Dataset/Source Table from
``table.bucket``/``table.source_table`` but never surfaced the row's
``bq_fqn``, and ``saveBqTabEdit``'s PUT payload never sent ``bq_fqn`` either.
Since the PUT uses ``exclude_unset``, an existing ``bq_fqn`` silently
persisted unchanged even when an admin edited Dataset/Source Table on a
cross-project row — the query/scan paths kept resolving against the stale
path. Flagged as a follow-up during PR #990's review.
"""

from __future__ import annotations

import pathlib

import pytest

TEMPLATE = pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_tables.html"


@pytest.fixture(scope="module")
def template_source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestEditModalTemplateHasProjectField:
    def test_edit_modal_has_a_project_input(self, template_source):
        assert 'id="editBqProject"' in template_source

    def test_project_field_is_scoped_to_live_access(self, template_source):
        """The field must only apply to Live access — PR #990 found that
        composing bq_fqn for synced/whole-table rows is inert because the
        materialize scheduler always reads the configured project."""
        idx = template_source.index('id="editBqProject"')
        window = template_source[max(0, idx - 400) : idx]
        assert "bq-edit-access-live" in window

    def test_access_mode_toggle_shows_and_hides_the_live_project_field(self, template_source):
        idx = template_source.index("function onEditBqAccessModeChange")
        body = template_source[idx : idx + 1200]
        assert "bq-edit-access-live" in body

    def test_open_edit_modal_prefills_project_from_bq_fqn(self, template_source):
        """_openEditBqModal must read the row's bq_fqn, not leave the
        Project field stale/empty on every open."""
        idx = template_source.index("function _openEditBqModal")
        body = template_source[idx : idx + 3000]
        assert "editBqProject" in body
        assert "table.bq_fqn" in body

    def test_save_composes_bq_fqn_on_the_live_branch(self, template_source):
        """saveBqTabEdit must send bq_fqn explicitly (not omit it) on the
        Live branch, mirroring _buildBigQueryPayload's register-time
        composition, so a Dataset/Source Table edit doesn't leave a stale
        bq_fqn in place."""
        idx = template_source.index("function saveBqTabEdit")
        end = template_source.index("(async function () {", idx)
        body = template_source[idx:end]
        live_branch = body[body.index("// Live") :]
        assert "editBqProject" in live_branch
        assert "payload.bq_fqn" in live_branch

    def test_save_does_not_send_bq_fqn_on_synced_branches(self, template_source):
        """PR #990's Devin-caught regression: composing bq_fqn for
        synced/whole-table rows doesn't work because the materialize
        scheduler ignores it. The synced (whole/custom) branches of
        saveBqTabEdit must not set payload.bq_fqn."""
        idx = template_source.index("function saveBqTabEdit")
        end = template_source.index("(async function () {", idx)
        body = template_source[idx:end]
        live_start = body.index("// Live")
        synced_branches = body[: body.index("} else {", body.index("accessMode === 'synced'"))]
        assert "payload.bq_fqn" not in synced_branches
        assert live_start > 0

    def test_dataset_from_bq_fqn_helper_extracts_middle_segment(self, template_source):
        idx = template_source.index("function _datasetFromBqFqn")
        body = template_source[idx : idx + 300]
        assert "split('.')" in body
        assert "parts[1]" in body

    def test_open_edit_modal_snapshots_original_bq_fqn_and_dataset(self, template_source):
        """saveBqTabEdit's decoupled-dataset guard (below) only works if
        _openEditBqModal captured the row's pristine bq_fqn/dataset before
        any admin edit."""
        idx = template_source.index("function _openEditBqModal")
        end = template_source.index("function closeEditBqModal", idx)
        body = template_source[idx:end]
        assert "_editBqOriginalBqFqn = table.bq_fqn" in body
        assert "_editBqOriginalDataset = preDataset" in body

    def test_save_preserves_decoupled_dataset_when_dataset_field_unchanged(self, template_source):
        """Devin Review finding on #1008: bq_fqn intentionally decouples the
        physical BigQuery dataset from the `bucket` label the Dataset field
        shows (issue #343) — a row can have bucket != bq_fqn's own dataset
        segment. Recomposing bq_fqn from the Dataset field's value
        unconditionally would silently corrupt a decoupled row's real
        dataset on ANY edit. The Live branch must fall back to the
        pristine bq_fqn's own dataset segment when the Dataset field still
        equals its original (unedited) value."""
        idx = template_source.index("function saveBqTabEdit")
        live_branch = template_source[idx : template_source.index("(async function () {", idx)]
        live_branch = live_branch[live_branch.index("// Live") :]
        guard_idx = live_branch.index("_editBqOriginalBqFqn && dataset === _editBqOriginalDataset")
        fqn_idx = live_branch.index("_datasetFromBqFqn(_editBqOriginalBqFqn)")
        compose_idx = live_branch.index("var bqFqn =")
        # The guard and fallback lookup must both precede the bqFqn
        # composition — otherwise the fallback value can't reach it.
        assert guard_idx < fqn_idx < compose_idx
        # And the composition must actually use the (possibly-overridden)
        # variable, not the raw Dataset-field value directly.
        assert "datasetForFqn" in live_branch[compose_idx : compose_idx + 120]


class TestSaveBqTabEditPutContract:
    """Integration coverage: a PUT shaped like the fixed saveBqTabEdit's
    Live branch must actually update bq_fqn on the registry row, and an
    explicit null must clear a stale one."""

    def test_edit_recomposes_bq_fqn_after_dataset_change(
        self,
        seeded_app,
        bq_instance,
        stub_bq_extractor,
    ):
        client = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])

        resp = client.post(
            "/api/admin/register-table",
            json={
                "name": "cross_project_live",
                "source_type": "bigquery",
                "query_mode": "remote",
                "bucket": "analytics",
                "source_table": "orders",
                "bq_fqn": "other-project.analytics.orders",
            },
            headers=headers,
        )
        assert resp.status_code in (200, 201, 202), resp.text

        # Admin edits Dataset/Source Table (e.g. fixing a typo) and the
        # fixed JS recomposes bq_fqn from the same Project + new
        # dataset/table, sending it explicitly rather than omitting it.
        resp = client.put(
            "/api/admin/registry/cross_project_live",
            json={
                "bucket": "analytics_v2",
                "source_table": "orders_v2",
                "bq_fqn": "other-project.analytics_v2.orders_v2",
                "query_mode": "remote",
                "source_query": None,
                "server_only": False,
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        reg = client.get("/api/admin/registry", headers=headers).json()
        row = next(t for t in reg["tables"] if t["id"] == "cross_project_live")
        assert row["bucket"] == "analytics_v2"
        assert row["source_table"] == "orders_v2"
        assert row["bq_fqn"] == "other-project.analytics_v2.orders_v2"

    def test_edit_clears_bq_fqn_when_project_field_is_blanked(
        self,
        seeded_app,
        bq_instance,
        stub_bq_extractor,
    ):
        """Blanking the Project field composes bqFqn=null in the JS, which
        must be sent as an explicit null so the PUT clears the stale
        cross-project pointer instead of exclude_unset preserving it."""
        client = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])

        resp = client.post(
            "/api/admin/register-table",
            json={
                "name": "drop_cross_project",
                "source_type": "bigquery",
                "query_mode": "remote",
                "bucket": "analytics",
                "source_table": "orders",
                "bq_fqn": "other-project.analytics.orders",
            },
            headers=headers,
        )
        assert resp.status_code in (200, 201, 202), resp.text

        resp = client.put(
            "/api/admin/registry/drop_cross_project",
            json={
                "bucket": "analytics",
                "source_table": "orders",
                "bq_fqn": None,
                "query_mode": "remote",
                "source_query": None,
                "server_only": False,
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

    def test_edit_preserves_decoupled_dataset_on_unrelated_edit(
        self,
        seeded_app,
        bq_instance,
        stub_bq_extractor,
    ):
        """Devin Review finding on #1008: `bucket` (the RBAC/UX label) can
        legitimately differ from bq_fqn's own dataset segment (issue #343
        decoupling). This is the server-side half of the fix — the fixed
        JS now resends the ORIGINAL bq_fqn dataset segment (not the
        `bucket` label) when the admin didn't retype Dataset, so an
        unrelated edit (here: just the description) must round-trip the
        decoupled dataset unchanged rather than collapsing it to `bucket`."""
        client = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])

        resp = client.post(
            "/api/admin/register-table",
            json={
                "name": "decoupled_live",
                "source_type": "bigquery",
                "query_mode": "remote",
                "bucket": "friendly-label",
                "source_table": "orders",
                "bq_fqn": "other-project.real_dataset.orders",
            },
            headers=headers,
        )
        assert resp.status_code in (200, 201, 202), resp.text

        # Simulates the fixed JS: Dataset field still shows "friendly-label"
        # (unedited), so it resends bq_fqn's own dataset segment
        # ("real_dataset") rather than "friendly-label", alongside an
        # unrelated description change.
        resp = client.put(
            "/api/admin/registry/decoupled_live",
            json={
                "bucket": "friendly-label",
                "source_table": "orders",
                "bq_fqn": "other-project.real_dataset.orders",
                "query_mode": "remote",
                "source_query": None,
                "server_only": False,
                "description": "updated description only",
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        reg = client.get("/api/admin/registry", headers=headers).json()
        row = next(t for t in reg["tables"] if t["id"] == "decoupled_live")
        assert row["bucket"] == "friendly-label"
        assert row["bq_fqn"] == "other-project.real_dataset.orders"
