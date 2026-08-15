"""The data-app runtime image must be pre-pulled, not fetched mid-deploy.

The runtime image (~1.3 GB) is NOT a compose service, so neither the startup
script's `docker compose pull` nor the auto-upgrade tick's ever touched it.
The first deploy on a host therefore fetched the whole thing synchronously
inside the apps-runner's `containers.run`. docker-py caps every daemon call at
60 s by default; when that expired the daemon tore the pull down and the
retried `create` raised ImageNotFound — so the deploy failed reporting a
missing image that was in fact still downloading, and the app surfaced it as
`runner_unavailable` while a perfectly healthy sidecar sat there.

Both hosting paths must pre-pull:

* the startup script, so a fresh VM is warm before anyone can click Deploy;
* `agnes-auto-upgrade.sh`, so a `runtime_image` bump lands on the next tick
  rather than waiting for a VM recreate — and so a host whose image was
  pruned re-warms on its own.

Guards on the shell sources, because there is no way to assert this from the
Python side: the bug lives entirely in what the deploy hosts have on disk.
"""

from pathlib import Path

TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl")
UPGRADE = Path("scripts/ops/agnes-auto-upgrade.sh")


def test_startup_script_prepulls_the_runtime_image():
    tpl = TPL.read_text()
    assert 'docker pull "$AGNES_DATA_APPS_RUNTIME_IMAGE"' in tpl, (
        "the startup script must pre-pull the data-app runtime image; without it the "
        "first deploy on a fresh VM pulls 1.3 GB inside the runner request and fails"
    )


def test_startup_prepull_is_gated_on_data_apps_being_enabled():
    """VMs without data apps must not spend boot time (or disk) on it."""
    tpl = TPL.read_text()
    idx = tpl.index('docker pull "$AGNES_DATA_APPS_RUNTIME_IMAGE"')
    preceding = tpl[:idx]
    opened = preceding.rindex("%{ if data_apps_enabled ~}")
    # No `%{ endif ~}` may close that guard between it and the pull.
    assert "%{ endif ~}" not in preceding[opened:], (
        "the runtime-image pre-pull must sit inside a `data_apps_enabled` guard"
    )


def test_startup_prepull_cannot_fail_the_boot():
    """A registry hiccup must degrade to a slow first deploy, not a dead VM —
    the startup script runs under `set -e`."""
    tpl = TPL.read_text()
    line = next(ln for ln in tpl.splitlines() if 'docker pull "$AGNES_DATA_APPS_RUNTIME_IMAGE"' in ln)
    assert "||" in line, f"pre-pull must be best-effort under set -e; got: {line.strip()}"


def test_auto_upgrade_prepulls_the_runtime_image():
    up = UPGRADE.read_text()
    assert "AGNES_DATA_APPS_RUNTIME_IMAGE" in up, (
        "agnes-auto-upgrade must read the runtime image from .env so it can pre-pull it"
    )
    assert 'docker pull "$DATA_APPS_RUNTIME_IMAGE"' in up, (
        "agnes-auto-upgrade must pre-pull the data-app runtime image on each tick"
    )


def test_auto_upgrade_prepull_is_gated_and_best_effort():
    up = UPGRADE.read_text()
    line = next(ln for ln in up.splitlines() if 'docker pull "$DATA_APPS_RUNTIME_IMAGE"' in ln)
    assert line.rstrip().endswith("\\") or "||" in line, (
        f"pre-pull must not abort the upgrade tick under set -e; got: {line.strip()}"
    )
    # Gated on the same flag that decides whether the sidecar runs at all.
    idx = up.index('docker pull "$DATA_APPS_RUNTIME_IMAGE"')
    window = up[max(0, idx - 400) : idx]
    assert "DATA_APPS_ENABLED" in window, "the pre-pull must be gated on AGNES_DATA_APPS_ENABLED"
