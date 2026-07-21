"""Phase 8.1 — static checks for the agnes-state-applier systemd unit
and the customer-instance startup-script template.

These tests do NOT exercise the unit live (systemd is not available in CI).
They assert that the committed files contain the expected security-relevant
strings so that a review diff catches any accidental regression.
"""


def test_applier_unit_runs_as_non_root_with_docker_as_supplementary():
    """Phase 8.1 — agnes-state-applier.service must run as
    User=agnes-applier with docker as a SUPPLEMENTARY group, not as
    the primary Group=docker.

    ``Group=docker`` (the original Phase 8.1 wiring) replaces the
    process's primary group entirely, leaving the applier with
    egid=docker only. systemd does NOT add the user's other groups
    from /etc/group as supplementary unless explicitly listed.
    Verified live on foundryai-dev-zsrotyr 2026-06-01: that wiring
    blocked the applier from reading /opt/agnes/.env
    (group=agnes-applier, mode 0640).

    ``SupplementaryGroups=docker`` keeps primary group as
    agnes-applier and adds docker on top, so both file accesses
    (.env, /data/state) and docker socket access work.
    """
    from pathlib import Path

    unit = Path("scripts/ops/agnes-state-applier.service").read_text()
    assert "User=agnes-applier" in unit, "unit must specify User=agnes-applier (Phase 8.1)"
    assert "SupplementaryGroups=docker" in unit, (
        "unit must use SupplementaryGroups=docker (not Group=docker) "
        "to preserve agnes-applier as primary group (Phase 8.1 follow-up #3)"
    )
    # Hard guard against regression: no ``Group=docker`` directive
    # (matching the directive form, not the doc-string mention).
    directives = [line for line in unit.splitlines() if line.strip() and not line.strip().startswith("#")]
    group_directives = [line for line in directives if line.startswith("Group=")]
    assert not group_directives, f"unit must NOT specify Group= as a directive; got {group_directives}"


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
    assert "After=" in unit and "agnes-state-applier-bootstrap.service" in unit, (
        "main unit must order After=agnes-state-applier-bootstrap.service"
    )
    assert "Requires=agnes-state-applier-bootstrap.service" in unit, (
        "main unit must Requires= the bootstrap unit (hard dep)"
    )


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
    directives = [line for line in unit.splitlines() if line.strip() and not line.strip().startswith("#")]
    user_directives = [line for line in directives if line.startswith("User=")]
    assert not user_directives or user_directives == ["User=root"], (
        f"bootstrap unit must run as root (no User= directive); got {user_directives}"
    )
    assert "Type=oneshot" in unit, "bootstrap unit must be oneshot"
    assert "RemainAfterExit=yes" in unit, "bootstrap unit must RemainAfterExit so Requires= satisfies on re-trigger"
    assert "useradd --system" in unit and "agnes-applier" in unit, (
        "bootstrap unit must contain useradd for agnes-applier"
    )
    assert "usermod -aG docker agnes-applier" in unit, "bootstrap unit must add agnes-applier to docker group"
    assert "chown -R agnes-applier:agnes-applier /data/state" in unit, (
        "bootstrap unit must chown /data/state to agnes-applier"
    )
    assert "chown agnes-applier:agnes-applier /opt/agnes/.env" in unit, (
        "bootstrap unit must chown /opt/agnes/.env to agnes-applier so "
        "docker compose's Go file loader can open it (Phase 8.1 follow-up #3)"
    )
    assert "Before=" in unit and "agnes-state-applier.service" in unit, (
        "bootstrap unit must order Before=agnes-state-applier.service"
    )


def test_startup_script_provisions_agnes_applier_user():
    """Phase 8.1 — startup script creates the agnes-applier user
    idempotently and adds it to the docker group."""
    from pathlib import Path

    tpl = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text()
    assert "useradd --system" in tpl and "agnes-applier" in tpl
    assert "usermod -aG docker agnes-applier" in tpl
    assert "chown -R agnes-applier:agnes-applier /data/state" in tpl


def test_bootstrap_unit_chowns_data_postgres_to_70_70():
    """B4-NEW tightening — chown 70:70 /data/postgres belongs in the
    root-running bootstrap unit, not in the agnes-applier's ExecStart
    (where it failed under set -e on every fresh VM).

    Must be recursive (``-R``): this unit reruns on every boot, and
    /data/postgres is on the persistent data disk — it survives a
    boot-disk-only VM recreate with its existing PGDATA contents
    already on disk. A non-recursive chown only fixed the top-level
    directory, leaving nested files (verified live: global/pg_filenode.map)
    owned by whatever uid the *previous* instance's postgres container
    mapped to 70, so a fresh container on the recreated VM hit
    "Permission denied" reading its own data directory. Observed live
    on agnes-dev 2026-07-21 after a VM delete+insert."""
    from pathlib import Path

    unit = Path("scripts/ops/agnes-state-applier-bootstrap.service").read_text()
    assert "chown -R 70:70 /data/postgres" in unit, (
        "bootstrap unit must chown -R 70:70 /data/postgres (recursive — "
        "see docstring for the VM-recreate permission-denied incident)."
    )


def test_startup_script_creates_data_postgres_owned_70_70():
    """Defence in depth: the customer-instance startup-script also
    creates /data/postgres with the right ownership at provision time."""
    from pathlib import Path

    tpl = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text()
    # Accepts chown 70:70 (plain or -R — recursive since the 2026-07-21 fix
    # that repairs dirs damaged by the old blanket `chown -R 999 /data`) or
    # install -o 70 -g 70 -d.
    assert (
        "chown 70:70 /data/postgres" in tpl
        or "chown -R 70:70 /data/postgres" in tpl
        or "install -d -o 70 -g 70" in tpl
        and "/data/postgres" in tpl
    ), "startup-script.sh.tpl must create /data/postgres owned 70:70"


def test_applier_does_not_chown_data_postgres_in_exec():
    """The applier's main ExecStart (non-root) must NOT attempt
    chown 70:70 /data/postgres — that's the bootstrap unit's job.
    Pre-B4-NEW tightening, the chown burned a noisy log line every
    tick even when ownership was already correct."""
    from pathlib import Path

    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    # The script may still STAT the dir, but it must not call chown.
    # Allow a comment mentioning chown for context.
    code_lines = [line for line in script.splitlines() if line.strip() and not line.strip().startswith("#")]
    chown_lines = [line for line in code_lines if "chown" in line and "/data/postgres" in line]
    assert not chown_lines, (
        "applier script must not invoke `chown` on /data/postgres "
        "(bootstrap unit does it as root); found:\n  " + "\n  ".join(chown_lines)
    )


def test_startup_script_chowns_env_to_agnes_applier():
    """B3-NEW + reviewer's recommendation: startup-script.sh.tpl must
    chown /opt/agnes/.env to agnes-applier:agnes-applier IMMEDIATELY
    after writing it. The bootstrap unit's ExecStart re-asserts this
    on every boot, but the first boot has a window between .env
    landing and the unit firing — during which a same-host attacker
    or a misconfigured cloud-init step could observe root-owned
    plaintext creds (mode 0600 root is fine for confidentiality but
    breaks the non-root applier's first run before the bootstrap
    unit runs)."""
    from pathlib import Path

    tpl = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text()
    # Look for the post-write chown. Accepts either form:
    #   chown agnes-applier:agnes-applier /opt/agnes/.env
    #   install -o agnes-applier -g agnes-applier ... /opt/agnes/.env
    assert (
        "chown agnes-applier:agnes-applier /opt/agnes/.env" in tpl
        or "install -o agnes-applier" in tpl
        and "/opt/agnes/.env" in tpl
    ), (
        "startup-script.sh.tpl must chown /opt/agnes/.env to "
        "agnes-applier IMMEDIATELY after writing it (B3-NEW + "
        "reviewer's recommendation — don't rely on the bootstrap "
        "unit's later run to fix ownership)."
    )


def test_dockerfile_ships_every_startup_installed_ops_unit():
    """Regression: the customer-instance startup-script ``install``s ops units
    from ``$APP_DIR`` — which is populated by ``docker cp`` of the image's
    ``/opt/agnes-host/``. Every unit the startup-script installs MUST therefore
    be shipped by the Dockerfile into ``/opt/agnes-host/``, or a fresh VM boot
    dies under ``set -e`` with ``install: cannot stat '.../<unit>'``.

    Caught in the wild: ``agnes-state-applier-bootstrap.service`` was added
    both as a committed unit and to the startup-script's install list, but
    never added to the Dockerfile COPY list — so it shipped in no image and
    every fresh postgres-path VM failed to boot."""
    import re
    from pathlib import Path

    tpl = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text()
    dockerfile = Path("Dockerfile").read_text()
    installed = set(
        re.findall(
            r'install[^\n]*"\$APP_DIR/(agnes-state-applier[^"]+)"',
            tpl,
        )
    )
    # Sanity for the test itself — the bootstrap unit is the known case.
    assert "agnes-state-applier-bootstrap.service" in installed, (
        "expected the startup-script to install the bootstrap unit from $APP_DIR; did the install path change?"
    )
    for unit in sorted(installed):
        assert f"scripts/ops/{unit}" in dockerfile, (
            f"Dockerfile must COPY scripts/ops/{unit} into /opt/agnes-host/ — "
            f"the startup-script installs it from $APP_DIR, so an image that "
            f"doesn't ship it breaks every fresh VM boot."
        )


def test_startup_script_selects_compose_overlay_by_backend():
    """Regression (bug #3): the startup-script must pick the docker-compose
    overlay set from the persisted ``instance.yaml`` backend, NOT bake the
    Postgres side-car overlay into the ``.env`` ``COMPOSE_FILE`` line
    unconditionally.

    Caught in the wild: on a `backend: cloud` VM, a reboot re-engaged the
    side-car overlay and ran the one-shot ``migrate`` service against the
    side-car Postgres, which failed (`failed to resolve host 'postgres'`) and
    blocked ``app``/``scheduler`` startup via ``depends_on``. The startup-script
    must mirror agnes-state-applier.sh: side-car overlay only for
    ``backend=side_car``; duckdb and cloud run the baseline."""
    from pathlib import Path

    tpl = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text()
    assert "COMPOSE_FILE=$COMPOSE_FILE_VALUE" in tpl, (
        "startup-script must write COMPOSE_FILE from the computed per-backend value, not a hardcoded overlay list"
    )
    assert "PERSISTED_BACKEND" in tpl and '= "side_car"' in tpl, (
        "startup-script must read the persisted backend and gate the side-car overlay on backend=side_car"
    )
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.postgres.yml" not in tpl, (
        "the Postgres side-car overlay must not be baked into the COMPOSE_FILE "
        ".env line unconditionally — it belongs only in the side_car branch"
    )


def test_compose_postgres_migrate_services_carry_prebuilt_image():
    """Regression: the ``migrate`` and ``data-migrate`` services in the
    postgres overlay must declare an ``image:`` (the pulled GHCR image), not
    only ``build: .``. Production VMs are sourceless — a build-only service
    makes ``docker compose up`` fail with ``failed to read dockerfile`` and
    breaks the side-car/cloud boot path. Mirrors the app/scheduler
    build+image split."""
    import yaml
    from pathlib import Path

    compose = yaml.safe_load(Path("docker-compose.postgres.yml").read_text())
    for svc in ("migrate", "data-migrate"):
        spec = compose["services"][svc]
        assert "image" in spec and "agnes-the-ai-analyst" in spec["image"], (
            f"{svc} must declare a prebuilt image: so sourceless prod VMs use "
            f"the pulled image instead of attempting a build (got "
            f"image={spec.get('image')!r})"
        )
