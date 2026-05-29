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


def test_applier_unit_ordered_after_bootstrap():
    """Phase 8.1 follow-up #2 — main applier unit must be ordered
    After= and Require= the bootstrap unit so by the time systemd
    loads the User= directive, the agnes-applier user exists.

    An earlier attempt put the user-creation in ``ExecStartPre=+`` of
    the main unit. Verified live on foundryai-dev-zsrotyr 2026-05-29:
    systemd validates ``User=`` at unit LOAD time, not at ExecStartPre
    run time — the unit refused to start with
    ``Failed to determine user credentials: No such process`` before
    any ExecStartPre had a chance to fire."""
    from pathlib import Path

    unit = Path("scripts/ops/agnes-state-applier.service").read_text()
    assert "After=" in unit and "agnes-state-applier-bootstrap.service" in unit, \
        "main unit must order After=agnes-state-applier-bootstrap.service"
    assert "Requires=agnes-state-applier-bootstrap.service" in unit, \
        "main unit must Requires= the bootstrap unit (hard dep)"


def test_bootstrap_unit_creates_user_and_state_dir():
    """Phase 8.1 follow-up #2 — the dedicated bootstrap unit runs as
    root (no User=) and creates the agnes-applier user + chowns
    /data/state. Customer infras that don't ship matching
    provisioning logic (e.g. forks of the OSS customer-instance
    module, the Groupon FoundryAI infra repo) get the bootstrap for
    free via the systemd unit ordering."""
    from pathlib import Path

    unit = Path("scripts/ops/agnes-state-applier-bootstrap.service").read_text()
    # Bootstrap unit must NOT specify User= as a directive (it has to
    # be root to create the agnes-applier user). The grep ignores
    # commented-out "User=" mentions in the docstring.
    directives = [line for line in unit.splitlines()
                  if line.strip() and not line.strip().startswith("#")]
    user_directives = [line for line in directives if line.startswith("User=")]
    assert not user_directives or user_directives == ["User=root"], \
        f"bootstrap unit must run as root (no User= directive); got {user_directives}"
    assert "Type=oneshot" in unit, "bootstrap unit must be oneshot"
    assert "RemainAfterExit=yes" in unit, \
        "bootstrap unit must RemainAfterExit so Requires= satisfies on re-trigger"
    assert "useradd --system" in unit and "agnes-applier" in unit, \
        "bootstrap unit must contain useradd for agnes-applier"
    assert "usermod -aG docker agnes-applier" in unit, \
        "bootstrap unit must add agnes-applier to docker group"
    assert "chown -R agnes-applier:agnes-applier /data/state" in unit, \
        "bootstrap unit must chown /data/state to agnes-applier"
    assert "Before=" in unit and "agnes-state-applier.service" in unit, \
        "bootstrap unit must order Before=agnes-state-applier.service"


def test_startup_script_provisions_agnes_applier_user():
    """Phase 8.1 — startup script creates the agnes-applier user
    idempotently and adds it to the docker group."""
    from pathlib import Path

    tpl = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text()
    assert "useradd --system" in tpl and "agnes-applier" in tpl
    assert "usermod -aG docker agnes-applier" in tpl
    assert "chown -R agnes-applier:agnes-applier /data/state" in tpl
