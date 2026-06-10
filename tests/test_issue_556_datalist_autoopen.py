"""UI regression for #556 — after Keboola bucket/table discovery, the
native <datalist> suggestion popup should open automatically.

When an operator clicks Discover / List tables in the register or edit
Keboola modal, the datalist gets repopulated and a toast confirms it, but
the browser suggestion dropdown stayed closed — the operator had to click
back into the input to see the loaded options. The fix focuses the
associated input and dispatches a synthetic `input` event (the
cross-browser heuristic Chromium honors) so the popup opens on its own.

The discover helpers (and this wiring) render only on a keboola-typed
instance — gated by `{% if data_source_type == 'keboola' %}` in
admin_tables.html. `get_data_source_type()` honors the `DATA_SOURCE` env
var first (app/instance_config.py), so we force keboola that way.
"""


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _keboola_admin_tables_html(seeded_app, monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "keboola")
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200
    return r.text


def test_datalist_popup_helper_present(seeded_app, monkeypatch):
    """The shared helper finds the input via its `list=` attribute, focuses
    it, and dispatches an `input` event to open the native popup."""
    html = _keboola_admin_tables_html(seeded_app, monkeypatch)
    # Sanity: discover helpers render on a keboola-typed instance.
    assert "function discoverKeboolaBuckets(" in html
    assert "function discoverKeboolaTables(" in html
    # The auto-open helper exists and does focus + synthetic input dispatch.
    assert "function _openKbDatalistPopup(" in html
    assert "input[list=\"' + datalistId + '\"]" in html
    assert "inputEl.focus()" in html
    assert "inputEl.dispatchEvent(new Event('input'" in html


def test_discover_handlers_open_popup_after_populating(seeded_app, monkeypatch):
    """Both discover handlers call the helper once the datalist has options
    (guarded so an empty result doesn't pop an empty dropdown)."""
    html = _keboola_admin_tables_html(seeded_app, monkeypatch)
    # Bucket discovery opens the bucket datalist popup.
    assert "if (dl.children.length) _openKbDatalistPopup(datalistId);" in html
    # Table discovery opens the table datalist popup.
    assert "if (dl.children.length) _openKbDatalistPopup(tablesDatalistId);" in html
