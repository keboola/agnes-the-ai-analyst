"""Static contract for the Studio toggle's Terraform → startup-script plumbing.

Pins the three-part infra contract so a rename or dropped template argument
can't silently break the deployment toggle (same lightweight read-the-template
pattern as ``test_startup_vault_key.py``):

* ``variables.tf`` declares ``studio_enabled`` as a bool defaulting to true;
* ``main.tf`` forwards it into ``templatefile(...)``;
* ``startup-script.sh.tpl`` emits ``AGNES_STUDIO_ENABLED=false`` into the app
  ``.env`` ONLY when the variable is false (enabled instances keep a clean
  ``.env`` and the app-side default-on applies).
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")


def test_variables_tf_declares_bool_default_true():
    body = (MODULE / "variables.tf").read_text()
    m = re.search(r'variable\s+"studio_enabled"\s*\{(.*?)\}', body, re.DOTALL)
    assert m, "variables.tf must declare studio_enabled"
    block = m.group(1)
    assert re.search(r"type\s*=\s*bool", block)
    assert re.search(r"default\s*=\s*true", block)


def test_main_tf_forwards_into_templatefile():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"studio_enabled\s*=\s*var\.studio_enabled", body)


def test_tpl_emits_env_only_when_disabled():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    m = re.search(
        r"%\{\s*if\s+!studio_enabled\s*~?\}\s*\nAGNES_STUDIO_ENABLED=false\s*\n%\{\s*endif\s*~?\}",
        body,
    )
    assert m, "tpl must emit AGNES_STUDIO_ENABLED=false guarded by !studio_enabled"
    # No unconditional emission anywhere else.
    assert body.count("AGNES_STUDIO_ENABLED") == 1
