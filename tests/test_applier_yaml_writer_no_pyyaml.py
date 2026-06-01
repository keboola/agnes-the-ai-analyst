"""H4-NEW — write_instance_yaml works even when PyYAML is unavailable
on the host.

Static contract test: asserts that agnes-state-applier.sh contains BOTH a
PyYAML availability probe AND a pure-bash fallback writer, so that the
function never silently fires the ERR trap on hosts missing python3-yaml.

The shim-subprocess approach was considered but is too fragile in CI because
the python3 heredoc is fed via stdin redirection; a PATH-shimmed interpreter
that patches ``sys.modules`` inside ``exec(sys.stdin.read())`` does not
intercept the top-level ``import yaml`` before exec(). A static structural
assertion is equivalent to the H2-NEW pattern already established in this
test suite and is the recommended fallback per the task spec.
"""
from pathlib import Path


APPLIER = Path("scripts/ops/agnes-state-applier.sh")
STARTUP = Path("infra/modules/customer-instance/startup-script.sh.tpl")


def test_write_instance_yaml_has_pyyaml_probe() -> None:
    """write_instance_yaml must probe for PyYAML before using it,
    rather than calling ``import yaml`` unconditionally inside the
    heredoc.  A missing PyYAML must NOT raise an unhandled error that
    fires the ERR trap.
    """
    text = APPLIER.read_text()
    assert "python3 -c 'import yaml' 2>/dev/null" in text, (
        "write_instance_yaml must guard the PyYAML path with "
        "``python3 -c 'import yaml' 2>/dev/null`` so an absent "
        "python3-yaml package does not trigger the ERR trap (H4-NEW)"
    )


def test_write_instance_yaml_has_bash_fallback() -> None:
    """write_instance_yaml must contain a pure-bash fallback writer
    that emits the database section using echo/printf, so the function
    is self-contained even when PyYAML is absent.
    """
    text = APPLIER.read_text()
    # The bash fallback must emit the 'database:' key literally — no
    # python interpreter involved.
    assert 'echo "database:"' in text, (
        "write_instance_yaml must contain a bash fallback that writes "
        "'database:' via echo (H4-NEW)"
    )
    # It must also emit the backend value.
    assert '"  backend: ${backend}"' in text or '"  backend: $backend"' in text or \
           "\"  backend: ${backend}\"" in text or "\"  backend: $backend\"" in text, (
        "write_instance_yaml bash fallback must write the backend value (H4-NEW)"
    )


def test_write_instance_yaml_bash_fallback_has_warn_log() -> None:
    """The bash fallback must log a WARN message explaining that PyYAML
    is absent and that non-database keys will be dropped, so operators
    know to install python3-yaml on the host.
    """
    text = APPLIER.read_text()
    assert "WARN" in text and "write_instance_yaml" in text and "PyYAML" in text, (
        "write_instance_yaml bash fallback must emit a WARN log mentioning "
        "that PyYAML is not installed (H4-NEW)"
    )


def test_startup_script_installs_python3_yaml() -> None:
    """The customer-instance startup-script must apt-install python3-yaml
    so that the PyYAML path in write_instance_yaml is taken on all
    freshly-provisioned VMs, and the bash fallback is a defensive-only path.
    """
    text = STARTUP.read_text()
    assert "python3-yaml" in text, (
        "startup-script.sh.tpl must apt-install python3-yaml so the "
        "pure-bash fallback in write_instance_yaml is rarely hit (H4-NEW)"
    )
