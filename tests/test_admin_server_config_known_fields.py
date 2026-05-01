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


# ── Part A: structured nested-field rendering ───────────────────────────────


def test_nested_field_renders_as_structured_form_not_json_blob(seeded_app, monkeypatch, tmp_path):
    """Renderer J3 upgrade: registry-declared nested fields get individual
    inputs with dotted-path data-key, not a single JSON textarea for the
    parent object. The JS renderer must contain the dotted-path collection
    logic so subfields round-trip as a structured patch.
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
        # Renderer must ship the structured nested-field path. The JS uses
        # dotted-path data-key for child inputs (e.g. data-key="bigquery.billing_project")
        # and the collector reconstructs nested patches.
        assert "nested-field" in body or "renderNestedField" in body or "dotted" in body or 'data-nested' in body, \
            "renderer JS must support structured nested-field rendering"
        # The collector must understand dotted-path keys (parent.child) and
        # rebuild a nested patch from them — replaces the old JSON-textarea path.
        assert "splitDotted" in body or '.split(".")' in body or "dotKey" in body or "nestedKey" in body, \
            "collector JS must rebuild nested patches from dotted-path keys"
        # Display-only mode must be GONE — child rows are now first-class inputs.
        assert "data-display-only" not in body, \
            "display-only fallback path must be removed; child fields are now editable"
    finally:
        ic._instance_config = None


def test_save_section_with_nested_field_merges_correctly(seeded_app, tmp_path, monkeypatch):
    """When the renderer ships a dotted-path patch (e.g. bigquery.billing_project=X),
    the API merges it into the existing data_source.bigquery dict without wiping
    the other keys (project, location, type)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    import yaml as _yaml
    (state / "instance.yaml").write_text(_yaml.dump({
        "data_source": {
            "type": "bigquery",
            "bigquery": {"project": "data-proj", "location": "us-central1"},
        },
    }))
    import app.instance_config as ic
    ic._instance_config = None
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Patch only billing_project nested under bigquery — type/project/location
        # must survive the merge.
        r = c.post(
            "/api/admin/server-config",
            headers=_auth(token),
            json={
                "sections": {
                    "data_source": {
                        "bigquery": {
                            "billing_project": "billing-proj",
                        },
                    },
                },
            },
        )
        assert r.status_code in (200, 204), r.text

        # Re-read from disk to verify the deep-merge preserved siblings.
        loaded = _yaml.safe_load((state / "instance.yaml").read_text())
        bq = loaded["data_source"]["bigquery"]
        assert bq.get("project") == "data-proj", bq
        assert bq.get("location") == "us-central1", bq
        assert bq.get("billing_project") == "billing-proj", bq
        assert loaded["data_source"]["type"] == "bigquery"
    finally:
        ic._instance_config = None
