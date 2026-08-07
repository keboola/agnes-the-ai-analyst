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


def test_every_write_instance_yaml_call_site_is_guarded():
    """A refused rewrite must never abort the applier mid-run.

    The applier runs under ``set -euo pipefail`` with ``trap '__rollback'
    ERR``. Once ``write_instance_yaml`` can exit non-zero, a *bare* call
    terminates the script at that point — and the two places it is called
    during a migration are followed by the lifecycle-flag handling and by
    step 4, which brings app+scheduler back up. Aborting there takes the
    instance offline and leaves it there: ``_recover_stuck_jobs`` only
    repairs jobs still in status ``running``, and a failed migration's job
    is already ``failed``.

    So every call site must either consume the status (``if …``) or discard
    it explicitly (``|| true``). A bare call is the bug.
    """
    lines = APPLIER.read_text().splitlines()
    bare = [
        (n, line.strip())
        for n, line in enumerate(lines, 1)
        # the call, not the definition, a comment, or a log line mentioning it
        if "write_instance_yaml " in line
        and not line.strip().startswith("#")
        and "write_instance_yaml() {" not in line
        and "logger" not in line
        and "echo" not in line
        and not line.strip().startswith("if ")
        and "|| true" not in line
    ]
    assert not bare, (
        "unguarded write_instance_yaml call(s) — under `set -e` + the ERR trap "
        f"a refused rewrite aborts the applier before app+scheduler restart: {bare}"
    )


# ---------------------------------------------------------------------------
# Every writer of the overlay, not just the ones a review happened to find
# ---------------------------------------------------------------------------

_OVERLAY_WRITERS = [
    ("app/api/admin.py", "server-config editor + the narrow overlay writer"),
    ("app/api/initial_workspace.py", "_write_section / _drop_section"),
    ("src/db_state_machine.py", "write_backend_state"),
    ("scripts/ops/agnes-state-applier.sh", "the applier's embedded PyYAML writer"),
]


def _replace_calls_without_a_preceding_chmod(body: str) -> list[tuple[int, str]]:
    """Lines doing an ``os.replace`` onto the overlay with no ``os.chmod`` of
    the temp in the four lines above it."""
    lines = body.splitlines()
    offenders = []
    for n, line in enumerate(lines):
        if "os.replace(" not in line:
            continue
        window = "\n".join(lines[max(0, n - 6) : n])
        if "os.chmod(" not in window:
            offenders.append((n + 1, line.strip()))
    return offenders


def test_every_overlay_writer_chmods_the_temp_before_the_rename():
    """0600 is only worth anything if EVERY writer applies it.

    The overlay is rewritten from four places. A writer that misses the chmod
    does not merely leave one save world-readable — ``os.replace`` carries the
    temp file's mode onto the destination, so it relaxes the file
    permanently, including on instances a previous save or the startup script
    had already repaired. Two of the four were missed on the first pass here
    (`initial_workspace`), which is exactly why this guard enumerates writers
    instead of naming the ones a review happened to catch.

    The chmod must precede the rename. Chmodding the destination afterwards
    leaves the real path observable at the umask default for the window
    between the two calls, on a file holding the database url with its
    password inline.
    """
    from pathlib import Path as _P

    failures = {}
    for rel, what in _OVERLAY_WRITERS:
        offenders = _replace_calls_without_a_preceding_chmod(_P(rel).read_text())
        if offenders:
            failures[f"{rel} ({what})"] = offenders
    assert not failures, f"overlay writers renaming without a preceding chmod: {failures}"


def test_success_path_does_not_overwrite_the_migrator_terminal_status():
    """A completed migration must not be reported as a failure.

    The applier's post-migration ``write_instance_yaml`` is NOT the backend
    flip — ``scripts/db_state_migrator.py`` calls ``write_backend_state``
    immediately before ``mark_success``, so by the time the applier sees
    ``FINAL_STATUS=success`` the overlay already names the target. What runs
    here normalizes ``database.url`` from the migrator's pinned-IP form to the
    canonical hostname. Losing that leaves a working instance on the right
    backend, so stamping the job ``failed`` over the migrator's own terminal
    status would report a migration that moved every row as a failure.
    """
    body = APPLIER.read_text()
    start = body.index('if [ "$FINAL_STATUS" = "success" ]')
    end = body.index("\nelse\n", start)
    success_branch = body[start:end]
    assert 'update_job "$PENDING_JOB" "failed"' not in success_branch, (
        "the success branch must not mark the job failed — the migrator already "
        "wrote the terminal status and already flipped the backend"
    )


def test_the_app_refuses_to_boot_on_an_unreadable_overlay():
    """Same bug class as the applier's, on the read the app itself does.

    ``app/instance_config.py`` wrapped the overlay read in a bare
    ``except Exception`` that logs "corrupt — falling back to static base
    config". ``database.backend`` lives in that overlay, so a PermissionError
    landing there boots an instance whose data is on Postgres onto the DuckDB
    default and starts writing to the wrong store. At 0644 the read could not
    fail this way; at 0600 a uid mismatch is enough.
    """
    body = Path("app/instance_config.py").read_text()
    assert "except OSError as exc:" in body, (
        "the overlay read must separate an unreadable file from a malformed one"
    )
    read_at = body.index("overlay_path.read_text()")
    window = body[read_at : read_at + 1500]
    assert "raise RuntimeError(" in window, (
        "an unreadable overlay must refuse to start rather than silently fall back to "
        "a base config that can name a different database.backend"
    )
