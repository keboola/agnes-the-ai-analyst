"""Contract: the module's startup script leaves instance.yaml owner-only.

`/data/state/instance.yaml` holds the Postgres URL with its password inline,
plus whatever credentials an operator put in a connector overlay, and it sits
on the data volume that several non-root containers mount. Created by root
under the default umask it lands world-readable (0644), while the equivalent
`/opt/agnes/.env` is 0600 — the asymmetry this guard exists to prevent.

The chmod must sit OUTSIDE the `if [ ! -f "$INSTANCE_YAML" ]` create branch:
an existing file is exactly the case that needs repairing, since deployments
provisioned before this line — or written by an app-side surface that predates
its own chmod — already carry the loose mode.
"""

import re
from pathlib import Path

TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl")


def _create_branch(body: str) -> str:
    """Return the `if [ ! -f "$INSTANCE_YAML" ]; then ... fi` block."""
    m = re.search(r'if \[ ! -f "\$INSTANCE_YAML" \]; then(.*?)\nfi\n', body, re.DOTALL)
    assert m, "expected the guarded instance.yaml create branch in the template"
    return m.group(1)


def test_tpl_chmods_instance_yaml():
    body = TPL.read_text()
    assert re.search(r'^chmod 600 "\$INSTANCE_YAML"', body, re.MULTILINE), (
        "startup-script.sh.tpl must chmod 600 $INSTANCE_YAML — root's umask "
        "otherwise leaves the DB password world-readable on the data volume"
    )


def test_chmod_is_outside_the_create_branch():
    body = TPL.read_text()
    assert 'chmod 600 "$INSTANCE_YAML"' not in _create_branch(body), (
        "the chmod must run on every boot, not only when the file is created "
        "— an already-existing 0644 file is the case that needs repairing"
    )


def test_create_branch_chowns_to_the_app_uid():
    """The create path hands a fresh instance.yaml to the app's uid.

    This is not on its own what decides the final owner — see
    ``test_recursive_state_chown_is_the_line_that_decides_the_owner`` — but a
    freshly created file must not be left owned by root, or the app cannot
    read it at 0600 even on the first boot.
    """
    body = TPL.read_text()
    assert 'chown 999:999 "$INSTANCE_YAML"' in body, (
        "the instance.yaml create branch must chown the file to uid 999 — "
        "root-owned at 0600 is unreadable by the app container"
    )


def test_recursive_state_chown_is_the_line_that_decides_the_owner():
    """0600 makes ownership load-bearing, and this is the line that sets it.

    ``chown -R agnes-applier:agnes-applier /data/state`` runs *after* the
    create branch's ``chown 999:999``, so it — not that line — determines who
    owns instance.yaml, and the applier re-creates the file under its own uid
    on every rewrite anyway. At 0600 the app container (uid 999) can therefore
    read the file only while ``agnes-applier`` resolves to uid 999, which
    ``useradd --system`` produces by allocation rather than by pin.

    The guard exists so a provisioning change to this line has to confront
    that dependency instead of silently locking the app out of its own
    config. Pinning the applier's uid is tracked separately; it is a fleet
    provisioning change, not a test fixture.
    """
    body = TPL.read_text()
    assert "chown -R agnes-applier:agnes-applier /data/state" in body, (
        "the recursive /data/state chown is what finally owns instance.yaml; "
        "if it moved or changed target, the uid-999 read assumption that 0600 "
        "depends on has to be re-established explicitly"
    )
    create_at = body.index('chown 999:999 "$INSTANCE_YAML"')
    recursive_at = body.index("chown -R agnes-applier:agnes-applier /data/state")
    assert recursive_at > create_at, (
        "the recursive chown is expected to run after the create branch — if "
        "that order flipped, the create branch would be the deciding line and "
        "this guard is pointed at the wrong one"
    )


APPLIER = Path("scripts/ops/agnes-state-applier.sh")


def test_applier_does_not_swallow_an_unreadable_instance_yaml():
    """A read failure must abort, not rebuild the file from an empty base.

    ``write_instance_yaml`` merges the new ``database:`` block into whatever
    the file already holds precisely so a backend switch keeps the operator's
    other sections. At 0644 the read always succeeded. At 0600 it can fail on
    a uid mismatch, and a blanket ``except Exception: existing = {}`` would
    turn that into a rewrite containing only ``database:`` — silently
    destroying every operator-set section, which is the failure the merge was
    introduced to prevent.
    """
    body = APPLIER.read_text()
    assert "except OSError" in body, (
        "write_instance_yaml must catch OSError separately and abort — a file "
        "it cannot read must not be rewritten from an empty base"
    )
    assert "except Exception:\n        existing = {}" not in body, (
        "the bare except around the instance.yaml read is what turns a PermissionError into a silent config wipe"
    )


def test_applier_propagates_a_refused_rewrite_to_its_caller():
    """Aborting only helps if the caller can tell the write did not happen."""
    body = APPLIER.read_text()
    assert 'return "$rc"' in body, (
        "write_instance_yaml must return the writer's exit status; a bare "
        "`return` discards it and the state machine logs a backend flip that "
        "never reached disk"
    )
    assert "if write_instance_yaml " in body, (
        "the post-migration success path must branch on the rewrite actually succeeding before it logs the flip"
    )
