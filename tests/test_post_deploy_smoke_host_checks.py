"""Pytest wrapper for the post-deploy smoke-test bash harness.

The actual test logic lives in ``tests/test_post_deploy_smoke_host_checks.sh``
(same pattern as ``tests/test_auto_upgrade_role_split.sh`` /
``tests/test_db_backup_pg_canary.sh``): it fakes ``curl`` on PATH, sandboxes
the ``$AGNES_OPT_DIR``/``$STATE_DIR`` paths
``scripts/ops/post-deploy-smoke-test.sh`` reads, and drives seven scenarios —
laptop mode (host checks + doctor SKIPped), the sidecar-Postgres
COMPOSE_FILE drift FAIL (with SCHEDULER_API_TOKEN bearer fallback asserted
off the transcript), a fully healthy sidecar+TLS VM, the empty-certs-dir
TLS-predicate FAIL, the doctor verdict mapping (ok/error/warning/info →
PASS/FAIL/WARN/INFO + exit code), a missing CLI wheel, and the
db-state-target.flag disagreement WARN. This wrapper just makes it part of
the ``pytest tests/`` run so CI enforces it automatically.

Like its siblings it prefers a bash >= 4 interpreter when one is
discoverable and skips otherwise, so an unpatched macOS toolchain doesn't
block a local run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path("tests/test_post_deploy_smoke_host_checks.sh")


def _find_bash4() -> str | None:
    candidates = []
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for extra in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash"):
        if extra not in candidates and Path(extra).exists():
            candidates.append(extra)
    for cand in candidates:
        try:
            probe = subprocess.run(
                [cand, "-c", 'echo "${BASH_VERSINFO[0]}"'],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            continue
        if probe.returncode == 0 and probe.stdout.strip().isdigit() and int(probe.stdout.strip()) >= 4:
            return cand
    return None


def test_post_deploy_smoke_host_checks_harness():
    bash = _find_bash4()
    if bash is None:
        pytest.skip("no bash >= 4 found on this host (brew install bash)")
    result = subprocess.run(
        [bash, str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0, f"harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "ALL SCENARIOS PASSED" in result.stdout
