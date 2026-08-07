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
