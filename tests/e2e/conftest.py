"""Shared fixtures for the cloud-chat E2E suite.

The fixtures here are intentionally heavyweight (real docker-compose +
real Chromium) and gated behind env vars so they never run in the
default `pytest` invocation. Without `AGNES_E2E=1` every test that
depends on `docker_e2e_agnes` skips cleanly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# docker-compose fixture (Task E.1)
# ---------------------------------------------------------------------------

_COMPOSE_FILE = Path(__file__).parent / "docker-compose.e2e.yml"
_BASE_URL = "http://localhost:8000"
_HEALTH_PATH = "/healthz"
_HEALTH_TIMEOUT_SECONDS = 120


def _docker_available() -> bool:
    """Quick check that `docker compose` (v2) is on PATH."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def _wait_for_health(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + _HEALTH_PATH, timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
        time.sleep(1.5)
    raise RuntimeError(
        f"agnes container did not become healthy within {timeout}s: {last_err!r}"
    )


@pytest.fixture(scope="session")
def docker_e2e_agnes() -> str:
    """Bring up docker-compose.e2e.yml, yield the base URL, tear down.

    Skips unless `AGNES_E2E=1`. Requires docker compose v2 + an
    ANTHROPIC_API_KEY on the host so the compose file's
    `${ANTHROPIC_API_KEY:?...}` resolves.

    The fixture is session-scoped so multiple E2E tests can share one
    stack — building the image is the expensive step (nsjail compile).
    """
    if not os.environ.get("AGNES_E2E"):
        pytest.skip("E2E env disabled — set AGNES_E2E=1 to run docker-compose suite")
    if not _docker_available():
        pytest.skip("docker compose (v2) not available on PATH")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip(
            "ANTHROPIC_API_KEY not set on host — compose file requires it "
            "(use AGNES_E2E_FAKE_AGENT=1 to flip the runner into echo mode)"
        )

    compose_args = ["docker", "compose", "-f", str(_COMPOSE_FILE)]

    # `up -d --build`: detached so we can poll healthz; --build forces a
    # rebuild when source changes between runs (the image layer caches
    # the dep install, so this is usually fast).
    subprocess.run([*compose_args, "up", "-d", "--build"], check=True)
    try:
        _wait_for_health(_BASE_URL, _HEALTH_TIMEOUT_SECONDS)
        yield _BASE_URL
    finally:
        # `down -v` clears the `agnes_data` named volume so the next
        # session starts from a clean DuckDB.
        subprocess.run(
            [*compose_args, "down", "-v"],
            check=False,
        )
