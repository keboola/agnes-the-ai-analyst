"""E2E: fresh-volume boot of the Postgres overlay chain.

Regression for the fresh-deployment blocker: on a brand-new ``data``
volume there is no ``system.duckdb``, and the ``data-migrate`` one-shot
used to exit 2 — wedging ``app``/``scheduler`` (both gated on
``service_completed_successfully``) so a fresh Postgres-backend
deployment could never boot compose from scratch.

This test drives the REAL overlay chain (docker-compose.yml +
docker-compose.postgres.yml) with project-scoped fresh volumes and
asserts the boot-critical one-shot chain (postgres healthy → alembic
``migrate`` → ``data-migrate``) completes with exit 0. The test-only
override (tests/e2e/docker-compose.fresh-volume-override.yml) only makes
the chain hermetic — no developer .env, no /data/postgres host bind.

Run with: pytest tests/test_e2e_docker_postgres_fresh.py -m docker -v
Requires: Docker + docker compose (>= 2.24 for the !reset merge tag).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = [
    "docker-compose.yml",
    "docker-compose.postgres.yml",
    "tests/e2e/docker-compose.fresh-volume-override.yml",
]
# Local-only tag: without it the build would retag the operator's pulled
# ghcr.io/...:stable image with locally built content.
TEST_TAG = "fresh-volume-e2e"


def _compose(project: str, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-p", project]
    for f in COMPOSE_FILES:
        cmd += ["-f", str(REPO_ROOT / f)]
    cmd += list(args)
    env = {
        **os.environ,
        "POSTGRES_PASSWORD": "fresh-boot-test-pw",
        "AGNES_TAG": TEST_TAG,
    }
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(REPO_ROOT))


@pytest.mark.timeout(1800)
def test_fresh_volume_boot_completes_data_migrate():
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")

    project = f"agnes-fresh-boot-{os.getpid()}"
    try:
        build = _compose(project, "build", "data-migrate")
        assert build.returncode == 0, f"image build failed:\n{build.stderr[-3000:]}"

        # `run` starts the depends_on chain (postgres → migrate) with the
        # same conditions `up` uses, then returns data-migrate's exit code.
        # Project-scoped volumes are brand new: no system.duckdb exists.
        run = _compose(project, "run", "--rm", "data-migrate")
        assert run.returncode == 0, (
            "data-migrate must exit 0 on a fresh data volume (nothing to "
            f"migrate)\nstdout:\n{run.stdout[-2000:]}\nstderr:\n{run.stderr[-2000:]}"
        )
        assert "nothing to migrate" in run.stdout, run.stdout[-2000:]
    finally:
        _compose(project, "down", "-v", "--remove-orphans", timeout=300)
