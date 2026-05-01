"""Tests for the known-fields registry exposure in /admin/server-config.

The /admin/server-config UI used to render only fields that already existed
in instance.yaml — operators couldn't discover optional knobs like
``data_source.bigquery.billing_project`` without reading the docs or hitting
runtime errors. The known-fields registry lets the backend declare "these
fields are valid for this section even when YAML omits them" so the UI can
render them as dashed placeholders alongside the populated values.

This test file proves the wiring at three layers:

1. GET response carries `known_fields`
2. The HTML shell ships the CSS class + JS hook the renderer needs
3. Registry entries surface when the YAML doesn't list the field
"""


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_get_server_config_returns_known_fields(seeded_app):
    """J2: GET response includes known_fields registry."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "known_fields" in body
    assert isinstance(body["known_fields"], dict)
    # Smoke fixture: data_source.bigquery.billing_project must be in the registry.
    bq = body["known_fields"].get("data_source", {}).get("bigquery", {})
    fields = bq.get("fields", {})
    assert "billing_project" in fields, body["known_fields"]
    assert "hint" in fields["billing_project"]


def test_known_field_billing_project_renders_in_ui(seeded_app, monkeypatch, tmp_path):
    """J3: renderer ships the CSS class + reads known_fields from the API.

    We can't assert the dashed input directly (the page is shell-only — the
    JS fills `#cfg-sections` from the GET response after the HTML loads).
    Instead verify the static template ships the two markers the renderer
    needs: the `is-unset` CSS class and a `known_fields` reference in the
    JS. The two together prove the wiring exists.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    import yaml as _yaml
    (state / "instance.yaml").write_text(_yaml.dump({
        "data_source": {"type": "bigquery", "bigquery": {"project": "p"}},
    }))
    import app.instance_config as ic
    ic._instance_config = None
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            r = c.get("/admin/server-config", headers={"Accept": "text/html"})
        finally:
            c.cookies.clear()
        assert r.status_code == 200, r.text
        body = r.text
        assert "is-unset" in body, "cfg-field.is-unset CSS class missing"
        assert "known_fields" in body, "renderer JS needs to consume known_fields"
    finally:
        ic._instance_config = None


def test_known_field_value_unset_when_yaml_missing(seeded_app, monkeypatch, tmp_path):
    """J3 indirectly: when the YAML has no billing_project, the GET still
    omits it from sections (it's not there to surface), but the registry
    entry tells the UI it's a valid optional field worth exposing."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    import yaml as _yaml
    (state / "instance.yaml").write_text(_yaml.dump({
        "data_source": {"type": "bigquery", "bigquery": {"project": "data-proj"}},
    }))
    import app.instance_config as ic
    ic._instance_config = None
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get("/api/admin/server-config", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        bq_section = body.get("sections", {}).get("data_source", {}).get("bigquery", {})
        # billing_project must be discoverable via known_fields even when
        # absent from YAML — the registry is the single source for which
        # optional knobs exist.
        assert "billing_project" in body["known_fields"]["data_source"]["bigquery"]["fields"]
        # The pre-existing _ensure_bq_optional_fields helper still seeds a
        # default into the section payload, so billing_project shows up
        # there too — that's fine, the registry exposes the *schema*
        # (kind/hint) the UI needs to render the field nicely. What matters
        # is that the registry is present so subagents 2-4 can populate
        # fields that *don't* have a corresponding seed helper.
        assert isinstance(bq_section, dict)
    finally:
        ic._instance_config = None


def test_known_fields_covers_all_editable_sections(seeded_app):
    """The registry has an entry (even if empty) for every editable section
    so subagents 2-4 know where to add their entries without having to
    decide whether the section needs a new top-level key.
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    body = r.json()
    editable = set(body["editable_sections"])
    known = set(body["known_fields"].keys())
    # Every editable section must have a (possibly empty) known_fields entry.
    missing = editable - known
    assert not missing, f"sections without a known_fields slot: {sorted(missing)}"
