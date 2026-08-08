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

import os
import re
from pathlib import Path

import pytest

TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl")


def _create_branch(body: str) -> str:
    """Return the `if [ ! -f "$INSTANCE_YAML" ]; then ... fi` block."""
    m = re.search(r'if \[ ! -f "\$INSTANCE_YAML" \]; then(.*?)\nfi\n', body, re.DOTALL)
    assert m, "expected the guarded instance.yaml create branch in the template"
    return m.group(1)


def test_tpl_chmods_instance_yaml():
    body = TPL.read_text()
    # Indented now — it sits inside the uid gate, see
    # `test_the_chmod_is_conditional_on_the_pin_having_taken`.
    assert re.search(r'^\s*chmod 600 "\$INSTANCE_YAML"', body, re.MULTILINE), (
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


def test_an_unreadable_overlay_raises_rather_than_falling_back(tmp_path, monkeypatch):
    """Behavioural, and it fails on the old code for the RIGHT reason.

    ``app/instance_config.py`` wrapped the overlay read in a bare
    ``except Exception`` that logs the file as "corrupt" and falls back to the
    static base config. ``database.backend`` lives in that overlay, so a
    PermissionError there boots an instance whose data is on Postgres onto the
    DuckDB default and starts writing to the wrong store. At 0644 the read
    could not fail this way; at 0600 a uid mismatch is enough.

    The primary assertion is "it did not return", not "it raised type X" — an
    assertion keyed only on the new exception type would fail on the old code
    because the name does not exist yet, which proves nothing about behaviour.
    The type is checked second, on the exception actually caught.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root — mode bits do not deny reads")

    import app.instance_config as ic

    state = tmp_path / "state"
    state.mkdir()
    overlay = state / "instance.yaml"
    overlay.write_text("database:\n  backend: postgres\n", encoding="utf-8")
    overlay.chmod(0o000)

    monkeypatch.setattr("app.secrets._state_dir", lambda: state)
    ic.reset_cache()
    try:
        raised = None
        try:
            cfg = ic.load_instance_config(strict=True)
        except Exception as exc:  # noqa: BLE001 — the type is asserted below
            raised = exc
        assert raised is not None, (
            "load_instance_config() returned instead of refusing: an unreadable overlay "
            f"silently fell back to the base config, whose database.backend is "
            f"{(cfg.get('database') or {}).get('backend')!r} rather than the overlay's"
        )
        assert type(raised).__name__ == "InstanceConfigUnreadable", (
            "the refusal must carry a distinct type so the boot path can tell it apart "
            f"from a soft config problem; got {type(raised).__name__}"
        )
    finally:
        overlay.chmod(0o600)
        ic.reset_cache()


def test_a_malformed_overlay_still_falls_back(tmp_path, monkeypatch):
    """The other half of the split — this one must NOT refuse to start.

    A malformed file is visible to the operator and repairable through the
    editor; refusing to boot on it is a worse trade than continuing on the
    base config. Only an unreadable one is fail-closed.
    """
    import app.instance_config as ic

    state = tmp_path / "state"
    state.mkdir()
    (state / "instance.yaml").write_text("database: [unclosed\n", encoding="utf-8")

    monkeypatch.setattr("app.secrets._state_dir", lambda: state)
    ic.reset_cache()
    try:
        cfg = ic.load_instance_config()
        assert isinstance(cfg, dict)
    finally:
        ic.reset_cache()


def test_the_boot_path_reraises_it_instead_of_logging_it():
    """`app/main.py` wraps the startup load in `except Exception` on purpose —
    a soft config problem must not stop an instance serving. That arm would
    also have swallowed this one, leaving the process up and 500ing every
    `get_value()` consumer while looking healthy. The refusal only exists if
    the boot path lets it through."""
    body = Path("app/main.py").read_text()
    assert "except InstanceConfigUnreadable:" in body, (
        "app/main.py must re-raise InstanceConfigUnreadable — otherwise the refusal "
        "to start is a comment, not a behaviour"
    )
    specific = body.index("except InstanceConfigUnreadable:")
    broad = body.index("logger.warning(f\"Could not load instance config")
    assert specific < broad, "the specific arm must come before the broad one to be reachable"


def test_undecodable_overlay_bytes_fall_back_rather_than_propagating(tmp_path, monkeypatch):
    """The read split must not leak a THIRD failure mode.

    ``Path.read_text()`` raises ``UnicodeDecodeError`` — a ``ValueError``, not
    an ``OSError`` — for bytes that are not valid UTF-8, which is exactly the
    partial-write shape the lenient path was written for. Caught by neither
    arm it propagates, ``_instance_config`` is never assigned, the boot path's
    broad ``except`` logs it, and every later ``get_value()`` re-raises: an
    instance that looks healthy and 500s on everything, which is the failure
    the split exists to prevent.
    """
    import app.instance_config as ic

    state = tmp_path / "state"
    state.mkdir()
    (state / "instance.yaml").write_bytes(b"database:\n  backend: \xff\xfe not utf-8\n")

    monkeypatch.setattr("app.secrets._state_dir", lambda: state)
    ic.reset_cache()
    try:
        cfg = ic.load_instance_config()
        assert isinstance(cfg, dict), "undecodable bytes must degrade to the base config"
    finally:
        ic.reset_cache()


def test_a_refused_rollback_does_not_restart_into_the_transient_backend():
    """`use_pg()` treats `*_in_progress` as Postgres.

    So an overlay left naming the transient does not mean "the backend it was
    already using" — with a duckdb source it points the app at a Postgres the
    failed migration never filled. The restart is withheld on that branch;
    everything else the guard-the-bare-call fix was for still runs.
    """
    body = APPLIER.read_text()
    assert "SKIP_APP_RESTART=1" in body, (
        "the rollback-refused branch must withhold the app+scheduler restart — the "
        "overlay still names a transient that resolves to a different engine than the data"
    )
    gate = body.index('if [ "${SKIP_APP_RESTART:-0}" = "1" ]')
    restart = body.index("RESTART_LOG=$(dc up -d --no-deps --force-recreate app scheduler")
    assert gate < restart, "the gate must precede the restart to have any effect"


def test_an_unreadable_overlay_after_boot_degrades_instead_of_500ing(tmp_path, monkeypatch):
    """The other half of the new contract, and the reason it is gated.

    ``load_instance_config`` is reached from ``get_value()``, i.e. from
    essentially every request path, and ``reset_cache()`` re-runs it on a live
    instance after an admin save. A file that becomes unreadable AFTER a good
    boot — an operator chown, a half-finished manual repair — must not turn
    every request into a 500 where it used to degrade. The danger being
    guarded against is *starting* on the wrong ``database.backend``, and a
    process that already holds a good config is not about to do that.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root — mode bits do not deny reads")

    import app.instance_config as ic

    state = tmp_path / "state"
    state.mkdir()
    overlay = state / "instance.yaml"
    overlay.write_text("database:\n  backend: postgres\n", encoding="utf-8")

    monkeypatch.setattr("app.secrets._state_dir", lambda: state)
    ic.reset_cache()
    try:
        first = ic.load_instance_config(strict=True)
        assert (first.get("database") or {}).get("backend") == "postgres"

        # Now it goes unreadable and an admin save drops the cache. This is
        # the real sequence: `reset_cache()` sets `_instance_config = None`,
        # so the fallback branch cannot lean on it — it has to hold the last
        # good config separately or it hands back the static base with every
        # operator-set section gone, while logging that it kept them.
        overlay.chmod(0o000)
        ic.reset_cache()
        again = ic.load_instance_config()
        assert isinstance(again, dict), "a live instance must keep serving, not raise per request"
        assert (again.get("database") or {}).get("backend") == "postgres", (
            "the overlay's settings must survive — returning the static base here is the "
            "silent-wrong-config outcome the boot refusal exists to prevent, just after boot"
        )

        # And it must be cached, or every request re-reads and re-parses the
        # static YAML and emits another ERROR line.
        assert ic._instance_config is not None, (
            "the fallback must populate the parse-once cache; without it `get_value()` "
            "redoes the whole load per request and floods the log"
        )
    finally:
        overlay.chmod(0o600)
        ic.reset_cache()


def test_neither_restart_path_starts_the_app_on_an_in_progress_backend():
    """Two restart sites, one invariant.

    `use_pg()` counts SIDE_CAR_IN_PROGRESS and CLOUD_IN_PROGRESS as Postgres,
    so an overlay left naming a transient points a duckdb-source instance at a
    database the failed or crashed migration never finished filling. Both the
    post-migration path and the stuck-job recovery bring app+scheduler back up,
    and both had to learn to withhold that when the rollback write was refused
    — fixing one and leaving the other is the shape this review keeps finding.
    """
    body = APPLIER.read_text()
    gates = body.count('SKIP_APP_RESTART=1')
    assert gates >= 2, (
        f"expected both the post-migration and stuck-job-recovery paths to set the gate; found {gates}"
    )
    assert '[ "$recovered_any" -eq 1 ] && [ "${SKIP_APP_RESTART:-0}" != "1" ]' in body, (
        "the stuck-job recovery's own restart must honour the gate — it is a separate "
        "`dc up` from step 4's and does not inherit anything"
    )


def test_the_applier_uid_is_pinned_not_allocated():
    """0600 only works while the applier and the app are the same uid.

    `chown -R agnes-applier /data/state` decides who owns instance.yaml, and
    the applier re-creates the file under its own uid on every rewrite — so at
    0600 the app container (uid 999) can read its own config only while those
    two numbers match. `useradd --system` without `--uid` produces that match
    by allocating the top free id in the system range, which is not the same
    thing as intending it.
    """
    body = TPL.read_text()
    assert "--uid 999" in body, (
        "the state-applier user must pin uid 999 — allocation happens to land there on "
        "today's image, and 0600 turns that coincidence load-bearing"
    )


def test_the_chmod_is_conditional_on_the_pin_having_taken():
    """The mode must not outrun its own precondition.

    If uid 999 was already taken at provisioning time the pin falls through to
    an allocated id, and a 0600 file the app does not own is one it cannot
    read — which, with the fail-closed read this change also introduces, is a
    refusal to start. A full outage in place of the silent degradation 0644
    gave. Where the pin did not take, the mode stays as it was and the reason
    goes to the console.
    """
    body = TPL.read_text()
    assert 'APPLIER_UID=$(id -u agnes-applier' in body, "provisioning must read back the uid it pinned"
    gate = body.index('if [ "$APPLIER_UID" = "999" ]')
    chmod_at = body.index('chmod 600 "$INSTANCE_YAML"')
    assert gate < chmod_at, "the chmod must sit inside the uid gate, not before it"


def test_the_chmod_still_runs_outside_the_create_branch():
    """Moving it below the applier user must not move it INTO the create branch —
    an already-existing 0644 file is the case that needs repairing."""
    body = TPL.read_text()
    assert 'chmod 600 "$INSTANCE_YAML"' not in _create_branch(body)


def test_the_boot_refusal_is_not_defused_by_an_earlier_import(monkeypatch, tmp_path):
    """The guard must not depend on being the first read in the process.

    It was gated on `_loaded_once`, a module flag meant to mean "we are past
    startup". Importing `app.main` loads the config, so that flag was already
    True by the time the startup block ran and the refusal was permanently
    defused — a guard armed only when nothing else imported first is not a
    guard. Strictness is the caller's declaration now, so a prior successful
    load cannot disarm it.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root — mode bits do not deny reads")

    import app.instance_config as ic

    state = tmp_path / "state"
    state.mkdir()
    overlay = state / "instance.yaml"
    overlay.write_text("database:\n  backend: postgres\n", encoding="utf-8")
    monkeypatch.setattr("app.secrets._state_dir", lambda: state)

    ic.reset_cache()
    try:
        ic.load_instance_config()  # a prior successful load, as any import does
        overlay.chmod(0o000)
        ic.reset_cache()
        with pytest.raises(ic.InstanceConfigUnreadable):
            ic.load_instance_config(strict=True)
    finally:
        overlay.chmod(0o600)
        ic.reset_cache()


def test_the_boot_path_drops_the_cache_before_its_strict_read():
    """A strict read that returns the cache inspects nothing.

    Importing `app.main` populates the parse-once cache, so the startup call
    would short-circuit on it and never touch the file — the check would pass
    on an instance whose overlay is unreadable.
    """
    body = Path("app/main.py").read_text()
    i = body.index("load_instance_config(strict=True)")
    window = body[max(0, i - 600):i]
    assert "reset_cache()" in window, (
        "the boot check must drop the cache first; otherwise it validates a config that "
        "was loaded at import time and reads nothing"
    )
