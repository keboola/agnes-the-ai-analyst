"""Pytest wrapper for the Postgres pg_dump + restore-canary bash harness.

The actual test logic lives in ``tests/test_db_backup_pg_canary.sh`` (same
pattern as ``tests/test_state_applier_host_script.sh``): it fakes `docker`,
`logger`, and `curl` on PATH, sandboxes the paths
``infra/modules/customer-instance/files/agnes-db-backup.sh`` reads/writes,
and drives four scenarios (DuckDB backend, healthy side-car, failed
restore-canary, persisted-but-not-running side-car) asserting the exact
docker command lines the backend-detection + dump-construction logic
produces. This wrapper just makes it part of the ``pytest tests/`` run so CI
enforces it automatically instead of requiring a manual invocation.

``agnes-db-backup.sh`` uses ``${STAGE^^}`` (bash >=4 parameter expansion),
which macOS's stock ``/bin/bash`` (3.2, last GPLv2 release) cannot parse —
a pre-existing property of the script, irrelevant on the Debian/Ubuntu GCE
images it actually runs on (bash >=4 by default) and on any CI runner. If no
bash >=4 is discoverable on PATH, skip rather than fail so local runs on an
unpatched macOS toolchain don't block on an environment gap; install a
newer bash (e.g. ``brew install bash``) to exercise it locally.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path("tests/test_db_backup_pg_canary.sh")


def _find_bash4() -> str | None:
    candidates = []
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for extra in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash"):
        if extra not in candidates and Path(extra).exists():
            candidates.append(extra)
    for candidate in candidates:
        try:
            out = subprocess.run(
                [candidate, "-c", "echo ${BASH_VERSINFO[0]}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0 and out.stdout.strip().isdigit() and int(out.stdout.strip()) >= 4:
            return candidate
    return None


def test_pg_backend_detection_and_dump_construction():
    bash4 = _find_bash4()
    if bash4 is None:
        pytest.skip(
            "no bash >=4 found on PATH — agnes-db-backup.sh's ${STAGE^^} needs "
            "it; install one (e.g. `brew install bash` on macOS) to run this "
            "harness locally. CI runners ship bash >=4 by default."
        )

    proc = subprocess.run(
        [bash4, str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"pg_dump backend-detection/canary harness failed "
        f"(bash={bash4}):\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "OK" in proc.stdout.splitlines()[-1]
