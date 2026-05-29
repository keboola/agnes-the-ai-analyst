"""Phase 8.1 — static checks for the agnes-state-applier systemd unit
and the customer-instance startup-script template.

These tests do NOT exercise the unit live (systemd is not available in CI).
They assert that the committed files contain the expected security-relevant
strings so that a review diff catches any accidental regression.
"""


def test_applier_unit_runs_as_non_root():
    """Phase 8.1 — agnes-state-applier.service must run as
    User=agnes-applier in Group=docker, not as root."""
    from pathlib import Path

    unit = Path("scripts/ops/agnes-state-applier.service").read_text()
    assert "User=agnes-applier" in unit, \
        "unit must specify User=agnes-applier (Phase 8.1)"
    assert "Group=docker" in unit, \
        "unit must specify Group=docker (Phase 8.1)"


def test_applier_unit_self_bootstraps_user_and_state_dir():
    """Phase 8.1 follow-up — the unit's ``ExecStartPre`` directives
    create the agnes-applier user, add it to the docker group, and
    chown /data/state, so customer infras that don't ship matching
    provisioning logic (e.g. forks of the OSS customer-instance
    module, the Groupon FoundryAI infra repo) still get a working
    state machine on first deploy. The ``+`` prefix runs each line
    as root regardless of ``User=agnes-applier``."""
    from pathlib import Path

    unit = Path("scripts/ops/agnes-state-applier.service").read_text()
    assert "ExecStartPre=+" in unit, \
        "self-bootstrap ExecStartPre directives missing (Phase 8.1 follow-up)"
    assert "useradd --system" in unit and "agnes-applier" in unit, \
        "agnes-applier useradd missing from ExecStartPre"
    assert "usermod -aG docker agnes-applier" in unit, \
        "docker group assignment missing from ExecStartPre"
    assert "chown -R agnes-applier:agnes-applier /data/state" in unit, \
        "/data/state chown missing from ExecStartPre"


def test_startup_script_provisions_agnes_applier_user():
    """Phase 8.1 — startup script creates the agnes-applier user
    idempotently and adds it to the docker group."""
    from pathlib import Path

    tpl = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text()
    assert "useradd --system" in tpl and "agnes-applier" in tpl
    assert "usermod -aG docker agnes-applier" in tpl
    assert "chown -R agnes-applier:agnes-applier /data/state" in tpl
