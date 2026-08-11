"""Static contract for the experience preset's Terraform → startup-script plumbing.

`experience` is the one-line redesign adoption switch (app >= 0.83.1): it
flips the app-side DEFAULTS of the coupled knobs (ui_layout → rail, theme →
paper, features.stack_auto_membership → on). The per-VM module field writes
`AGNES_INSTANCE_EXPERIENCE` so a VM recreate cannot silently strip the
preset an instance runs with.

Same read-the-template pattern as `test_startup_ui_layout_theme_toggle.py`,
whose fields this one mirrors exactly — per-VM (dev-first rollout), empty
default writes NO env line (an `AGNES_INSTANCE_EXPERIENCE=` empty line
would shadow whatever `instance.experience` says in instance.yaml), and the
Terraform allowlist must track the app's own accepted set. The app-side set
lives on the `experience` Switch in `app/switches.py` (kind="select") —
`get_experience()` resolves through `switch_value`, so the options tuple IS
the resolver's acceptance set, and an unrecognised value falls back to
`classic` silently, which is exactly why the Terraform validation exists.
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


def test_both_object_types_declare_the_experience_field():
    """Terraform silently DROPS attributes absent from the object type, so a
    field declared on only one of the two would reach the module for prod and
    vanish for dev (or vice versa) with no error anywhere."""
    body = (MODULE / "variables.tf").read_text()
    for block in _object_type_blocks(body):
        assert re.search(r'experience\s*=\s*optional\(string,\s*""\)', block), (
            'experience must be declared as optional(string, "") on both prod_instance and dev_instances'
        )


def test_main_tf_forwards_experience_per_vm():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"experience\s*=\s*each\.value\.experience", body), (
        "main.tf must forward experience per-VM (each.value), not module-wide"
    )


def test_tpl_emits_the_env_line_only_when_set():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert re.search(
        r'%\{\s*if\s+experience\s*!=\s*""\s*~?\}\s*\nAGNES_INSTANCE_EXPERIENCE=\$\{experience\}\s*\n%\{\s*endif\s*~?\}',
        body,
    ), "tpl must emit AGNES_INSTANCE_EXPERIENCE guarded by a non-empty experience"
    assert body.count("\nAGNES_INSTANCE_EXPERIENCE=") == 1, (
        "AGNES_INSTANCE_EXPERIENCE must be emitted exactly once — a second, "
        "unguarded line would write an empty value and shadow instance.yaml"
    )


# ---------------------------------------------------------------------------
# The Terraform allow-list must track the app's own resolver
# ---------------------------------------------------------------------------


def _tf_allowlists(field: str) -> list[set[str]]:
    """Every `contains([...], ...)` value set `variables.tf` declares for
    `field` — one per validation block (prod_instance + dev_instances), NOT
    unioned: a union would let one block drift while the other still carries
    the full set (verified by mutation — removing a value from the prod
    validation alone must fail the guard below)."""
    tf = (MODULE / "variables.tf").read_text()
    sets = [
        {v.strip().strip('"') for v in m.group(1).split(",") if v.strip()}
        for m in re.finditer(r"contains\(\[([^\]]*)\],\s*[^)]*\.%s\)" % re.escape(field), tf)
    ]
    assert len(sets) == 2, (
        f"expected exactly two contains() validations for {field} (prod_instance + dev_instances), found {len(sets)}"
    )
    return sets


def test_tf_experience_allowlist_matches_the_app():
    """A preset value added to the app but not to `variables.tf` would be
    rejected at plan time on every instance that tried to adopt it; one
    removed from the app but left here would apply cleanly and then silently
    fall back to `classic`. Neither surface can move alone — and both
    validation blocks must carry the identical set."""
    from app.switches import get_switch

    app_allowed = set(get_switch("experience").options)
    for allowed in _tf_allowlists("experience"):
        assert allowed - {""} == app_allowed
