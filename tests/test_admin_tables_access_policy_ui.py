"""Task 15 — admin UI: the Access column, the policy editor modal, the
inline interlock warning, the preview call, and policy history on
/admin/tables (table access policies design doc §13, §13.1).

The per-table listing renders entirely client-side
(``_renderFlatTableRows`` fetches ``/api/admin/registry`` and builds
``<tr>``s in JS — see ``loadAdminTablesLayout``), so — like every other
admin_tables UI test in this suite (``test_admin_tables_tab_ui.py``,
``test_admin_tables_warmup_ui.py``, ``test_admin_tables_ui_materialized.py``)
— these are structural: assert the served HTML (the inline ``<script>``
ships verbatim) carries the renderer, the three Access-column states, the
modal's DOM, and the inline-error wiring. A full click-through needs a
headless browser this suite doesn't run.
"""


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_access_column_header_present(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert "'<th>Access</th>'" in r.text


def test_access_column_renders_three_states(seeded_app):
    """``renderAccessPolicyChip`` emits: (1) a plain "—" for a table that
    could carry a policy but doesn't, (2) a muted "not available —
    distributed" for a table that isn't eligible (not remote/server_only),
    and (3) a tinted "Policy" chip for a table that carries one."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "function renderAccessPolicyChip" in body
    assert "access-chip--none" in body
    assert "access-chip--unavailable" in body
    assert "access-chip--active" in body
    assert "not available — distributed" in body
    assert ">Policy</button>" in body


def test_access_column_omits_the_unwired_mapping_warn_state(seeded_app):
    """The design doc names a fourth "Policy · check" warn chip for an
    empty/stale mapping table (§15.1). No surface exposes that signal yet
    (would need server-side SQL-reference parsing + mapping sync-state
    cross-reference), so it is deliberately NOT implemented — this pins
    that as a documented decision, not a silent gap."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "access-chip--warn" not in body
    assert "TODO: wire once that signal exists" in body


def test_internal_tables_get_a_fixed_non_interactive_access_chip(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "access-chip--fixed" in body
    assert "Internal tables use their own built-in row scoping" in body


def test_access_policy_modal_is_a_plain_textarea_no_syntax_highlighting(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="accessPolicyModal"' in body
    assert '<textarea class="form-textarea" id="apSql"' in body
    assert '<textarea class="form-textarea" id="apNote"' in body
    for forbidden in ("codemirror", "CodeMirror", "monaco-editor", "ace-builds", "ace.js"):
        assert forbidden not in body, f"unexpected syntax-highlighting dependency: {forbidden}"


def test_access_policy_modal_shows_the_variable_vocabulary_and_prefill(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "$user_email" in body
    assert "$user_id" in body
    assert "$user_groups" in body
    assert "Use table as base" in body
    assert "function apUseTableAsBase" in body
    assert "'SELECT * FROM '" in body


def test_access_policy_note_is_required_before_save(seeded_app):
    """The API 422s a policy attach with no note (Task 14); the modal
    surfaces that requirement inline before even calling the API."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apNote"' in body
    assert "async function apSavePolicy" in body
    assert "access_policy_note is required" in body


def test_access_policy_save_and_clear_wired_to_registry_put(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "async function apClearPolicy" in body
    assert 'id="apClearBtn"' in body
    assert "/api/admin/registry/" in body
    assert "access_policy_sql" in body
    assert "confirmModal(" in body


def test_access_policy_inline_interlock_warning_mirrors_the_bq_pattern(seeded_app):
    """Deliverable 3: the interlock warning is computed and shown BEFORE
    save — the same pattern as ``onEditBqAccessModeChange`` — and it names
    the fix (set server_only, or query_mode='remote')."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apInterlockWarning"' in body
    assert "function _apRenderInterlockWarning" in body
    assert "onEditBqAccessModeChange" in body  # explicit cross-reference in the comment
    assert "server_only=true" in body
    assert "query_mode='remote'" in body


def test_access_policy_save_failure_renders_inline_not_as_a_toast(seeded_app):
    """A rejected save (the §16 ``reason: detail`` error contract) must
    render inline in the modal's ``apSaveError`` slot, not the 4s
    auto-hide ``showToast`` other saves use."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apSaveError"' in body
    assert "function _apShowSaveError" in body
    assert "function _apHideSaveError" in body
    # Both the PUT-rejected branch and the network-error branch route
    # through the inline renderer, not showToast(..., 'error').
    assert body.count("_apShowSaveError(") >= 4


def test_access_policy_preview_is_wired_to_the_preview_endpoint(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "async function apRunPreview" in body
    assert "/policy/preview" in body
    assert "as_user" in body
    assert "as_groups" in body
    assert "rows_visible" in body
    assert "rows_total" in body


def test_access_policy_preview_is_single_persona_with_a_documented_todo(seeded_app):
    """v1 ships single-persona preview; the full persona -> rows -> columns
    matrix (§13.1: union coverage + pairwise overlap across every distinct
    group-set) is explicitly deferred, not silently missing."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "TODO: once that primitive exists" in body
    assert "union coverage" in body
    assert "pairwise overlap" in body


def test_access_policy_history_reads_the_existing_activity_endpoint(seeded_app):
    """History reuses ``GET /api/admin/activity`` (resource + action_prefix
    filters) rather than a new endpoint — zero backend plumbing added."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "async function _apLoadHistory" in body
    assert 'id="apHistorySection"' in body
    assert "/api/admin/activity?resource=" in body
    assert "action_prefix=update_table" in body


def test_registered_table_row_wires_the_access_chip_to_the_modal(seeded_app):
    """A registered table's row calls ``openAccessPolicyModal(t)`` with the
    full registry row as payload, so the modal can prefill from
    access_policy_sql/_note without a second round-trip."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    c.post(
        "/api/admin/register-table",
        headers=_auth(token),
        json={
            "name": "ap_ui_orders",
            "source_type": "keboola",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "query_mode": "local",
        },
    )
    r = c.get("/admin/tables", headers=_auth(token))
    assert "openAccessPolicyModal(" in r.text
