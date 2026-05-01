"""Renderer depth/array/map tests for /admin/server-config.

The base renderer in `admin_server_config.html` already supports arbitrary
depth for `kind="object"` with `fields` (recursion is bounded only by the
browser stack). This file pins down the harder shapes corporate_memory
needs:

- Arrays of scalars (e.g. domains, detection_types) rendered as a
  per-element stack with add/remove buttons rather than a single JSON
  textarea.
- Maps of scalars (e.g. confidence.base) rendered as key:value rows with
  add/remove.
- Maps whose values are arrays of strings (e.g. domain_owners,
  entity_resolution.entities) rendered as key + nested array rows.
- Dotted keys present in *data* (e.g. confidence.base keys like
  ``user_verification.correction``) survive round-trip without being
  mistaken for nested-path separators.

We assert structurally on the static template (the page is a shell — JS
fills the form from /api/admin/server-config). The markers we look for
are the JS function/identifier names that implement each shape.
"""


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_renderer_supports_array_of_scalars(seeded_app, monkeypatch, tmp_path):
    """An array-of-strings registry leaf renders as a vertical stack of
    text inputs, not a JSON textarea.

    Marker: the JS contains a renderer entry point for arrays-of-scalars
    that produces add/remove controls — `renderArrayField` or equivalent
    plus an "addArrayItem" / "removeArrayItem" interaction handler.
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
        # The renderer ships a dedicated array-of-scalars path.
        assert "renderArrayField" in body, \
            "JS must implement renderArrayField for kind='array'+item_kind=scalar"
        # Add/remove handlers for individual array items.
        assert "data-array-add" in body, "missing add-row interaction marker"
        assert "data-array-remove" in body, "missing remove-row interaction marker"
    finally:
        ic._instance_config = None


def test_renderer_supports_map_of_scalars(seeded_app, monkeypatch, tmp_path):
    """A map of string→float renders as key:value rows with add/remove,
    not as a JSON textarea. Marker: `renderMapField` exists in the JS.
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
        assert "renderMapField" in body, \
            "JS must implement renderMapField for kind='map'"
        assert "data-map-add" in body, "missing map add-row interaction marker"
        assert "data-map-remove" in body, "missing map remove-row interaction marker"
    finally:
        ic._instance_config = None


def test_renderer_path_is_json_encoded_not_dotted_string(seeded_app, monkeypatch, tmp_path):
    """When data keys themselves contain dots (e.g.
    ``confidence.base.user_verification.correction`` where
    ``user_verification.correction`` is one map key), the renderer must
    NOT split on '.' to reconstruct the patch shape — that would break
    the dotted data key into two path segments.

    Implementation: leaf inputs carry a `data-path` attribute holding the
    JSON-encoded array of segments. The collector reads that array
    instead of splitting `data-key` on '.'. The dotted `data-key` stays
    around for backward compatibility (existing nested object fields
    use it), but maps emit JSON paths so their keys round-trip intact.
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
        # The collector must understand JSON-encoded path arrays so map
        # keys with embedded dots survive round-trip.
        assert "data-path" in body, "JSON path attribute missing from renderer"
        # The collector should prefer data-path over splitting data-key on '.'
        # Look for the parsing entry point.
        assert "JSON.parse" in body and "data-path" in body, \
            "collector must parse JSON-encoded data-path arrays"
    finally:
        ic._instance_config = None


def test_renderer_handles_4_level_object_nesting(seeded_app, monkeypatch, tmp_path):
    """Smoke check: the recursive renderer doesn't bail out at depth 4.
    The renderer is `renderNestedField(... depth)`; recursion is unbounded
    on the JS side. We assert by ensuring the renderer's nested-form path
    is wired with a depth-incrementing recursion call (literal markers in
    the JS).
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
        # The recursion marker — depth bumps in the recursive call.
        assert "renderNestedField(" in body
        assert "(depth || 0) + 1" in body, \
            "recursion must increment depth on each nested call"
    finally:
        ic._instance_config = None
