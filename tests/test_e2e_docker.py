"""E2E Docker tests — spin up containers, test API from outside.

Run with: pytest tests/test_e2e_docker.py -m docker -v
Requires: Docker and docker compose installed.
"""

import os
import subprocess
import time

import pytest

# Skip all tests in this module if docker marker not selected
pytestmark = pytest.mark.docker

COMPOSE_FILE = "docker-compose.test.yml"
BASE_URL = "http://localhost:8000"


def _docker_compose(*args, timeout=60):
    """Run docker compose command."""
    cmd = ["docker", "compose", "-f", COMPOSE_FILE] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _wait_for_health(url, timeout=30):
    """Poll health endpoint until it responds 200."""
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{url}/api/health", timeout=5)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


@pytest.fixture(scope="module")
def docker_env():
    """Start docker compose, yield, then tear down."""
    # Check docker is available
    result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    if result.returncode != 0:
        pytest.skip("Docker not available")

    # Check compose file exists
    if not os.path.exists(COMPOSE_FILE):
        pytest.skip(f"{COMPOSE_FILE} not found")

    # Start services
    _docker_compose("up", "-d", "--build")

    # Wait for health
    if not _wait_for_health(BASE_URL, timeout=60):
        # Capture logs for debugging
        logs = _docker_compose("logs")
        _docker_compose("down", "-v")
        pytest.fail(f"Service did not become healthy.\nLogs:\n{logs.stdout}")

    yield BASE_URL

    # Teardown
    _docker_compose("down", "-v")


class TestDockerHealth:
    def test_health_endpoint(self, docker_env):
        import httpx
        resp = httpx.get(f"{docker_env}/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") in ("ok", "healthy")

    def test_health_has_duckdb(self, docker_env):
        import httpx
        resp = httpx.get(f"{docker_env}/api/health")
        data = resp.json()
        services = data.get("services", {})
        assert "duckdb_state" in services
        assert services["duckdb_state"]["status"] == "ok"


class TestDockerFullFlow:
    def test_register_and_query_flow(self, docker_env):
        import httpx
        url = docker_env

        # Get auth token
        resp = httpx.post(f"{url}/auth/token", json={"email": "admin@test.com"})
        if resp.status_code != 200:
            # Auto-create user first if needed
            pytest.skip("Auth setup required — no admin user in Docker env")

        token = resp.json().get("token", "")
        headers = {"Authorization": f"Bearer {token}"}

        # Register a table
        resp = httpx.post(f"{url}/api/admin/register-table", json={
            "name": "docker_test", "source_type": "keboola", "query_mode": "local",
        }, headers=headers)
        assert resp.status_code in (201, 409)  # 409 if already exists

        # Get registry
        resp = httpx.get(f"{url}/api/admin/registry", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

        # Get manifest
        resp = httpx.get(f"{url}/api/sync/manifest", headers=headers)
        assert resp.status_code == 200
        assert "tables" in resp.json()
        assert "server_time" in resp.json()
