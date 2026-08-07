"""Static contract for the chrome toggles' Terraform → startup-script plumbing.

`ui_layout` / `theme` decide which chrome the web UI renders. Until they
existed on the module, the only way to set them on a provisioned VM was
`instance.ui_layout` / `instance.theme` in `/data/state/instance.yaml` — which
survives a VM recreate (it lives on the data disk) but is invisible to
Terraform, so the deployment's own config could not state what chrome it runs.

Same lightweight read-the-template pattern as `test_startup_studio_toggle.py`,
with one difference that matters: these are **per-VM** fields, not module-wide,
because the redesign is rolled out dev-first. The empty default must write NO
env line at all — `AGNES_UI_LAYOUT=` would resolve to an empty string and, unlike
an absent variable, shadow whatever instance.yaml says.
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")


def _object_type_blocks(body: str) -> list[str]:
    """Return the prod_instance + dev_instances object-type declarations."""
    blocks = []
    for var in ("prod_instance", "dev_instances"):
        m = re.search(rf'variable\s+"{var}"\s*\{{', body)
        assert m, f"variables.tf must declare {var}"
        depth, i = 1, m.end()
        while i < len(body) and depth:
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
            i += 1
        blocks.append(body[m.start() : i])
    return blocks


def test_both_object_types_declare_the_chrome_fields():
    """Terraform silently DROPS attributes absent from the object type, so a
    field declared on only one of the two would reach the module for prod and
    vanish for dev (or vice versa) with no error anywhere."""
    body = (MODULE / "variables.tf").read_text()
    for block in _object_type_blocks(body):
        for field in ("ui_layout", "theme"):
            assert re.search(rf'{field}\s*=\s*optional\(string,\s*""\)', block), (
                f'{field} must be declared as optional(string, "") on both prod_instance and dev_instances'
            )


def test_main_tf_forwards_both_per_vm():
    body = (MODULE / "main.tf").read_text()
    for field in ("ui_layout", "theme"):
        assert re.search(rf"{field}\s*=\s*each\.value\.{field}", body), (
            f"main.tf must forward {field} per-VM (each.value), not module-wide"
        )


def test_tpl_emits_each_env_line_only_when_set():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    for guard, env in (("ui_layout", "AGNES_UI_LAYOUT"), ("theme", "AGNES_INSTANCE_THEME")):
        assert re.search(
            rf'%\{{\s*if\s+{guard}\s*!=\s*""\s*~?\}}\s*\n{env}=\$\{{{guard}\}}\s*\n%\{{\s*endif\s*~?\}}',
            body,
        ), f"tpl must emit {env} guarded by a non-empty {guard}"
        assert body.count(f"\n{env}=") == 1, (
            f"{env} must be emitted exactly once — a second, unguarded line "
            "would write an empty value and shadow instance.yaml"
        )


# ---------------------------------------------------------------------------
# The Terraform allow-lists must track the app's own resolvers
# ---------------------------------------------------------------------------


def _tf_allowed(field: str) -> set[str]:
    """The value set `variables.tf` accepts for `field`, read from the
    `contains([...], ...)` conditions rather than restated here."""
    import re
    from pathlib import Path

    tf = (Path(__file__).resolve().parents[1] / "infra/modules/customer-instance/variables.tf").read_text()
    found: set[str] = set()
    for m in re.finditer(r"contains\(\[([^\]]*)\],\s*[^)]*\.%s\)" % re.escape(field), tf):
        found |= {v.strip().strip('"') for v in m.group(1).split(",") if v.strip()}
    assert found, f"no contains() validation found for {field} — the guard below would pass vacuously"
    return found


def _app_allowed(func_src: str) -> set[str]:
    """The value set the app's resolver accepts, from its own `not in (...)`."""
    import re

    m = re.search(r"if value not in \(([^)]*)\):", func_src)
    assert m, "resolver shape changed — this guard reads its `value not in (...)` line"
    return {v.strip().strip('"').strip("'") for v in m.group(1).split(",") if v.strip()}


def _resolver_src(name: str) -> str:
    import inspect

    import app.instance_config as ic

    return inspect.getsource(getattr(ic, name))


def test_tf_ui_layout_allowlist_matches_the_app():
    """A theme or layout added to the app but not to `variables.tf` would be
    rejected at plan time on every instance that tried to adopt it; one
    removed from the app but left here would apply cleanly and then silently
    fall back. Neither surface can move alone."""
    assert _tf_allowed("ui_layout") - {""} == _app_allowed(_resolver_src("get_ui_layout"))


def test_tf_theme_allowlist_matches_the_app():
    assert _tf_allowed("theme") - {""} == _app_allowed(_resolver_src("get_instance_theme"))
