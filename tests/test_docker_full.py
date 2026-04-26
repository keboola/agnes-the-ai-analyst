"""Docker E2E tests — requires a running docker-compose stack.

Run with: pytest tests/test_docker_full.py -m docker -v
Assumes docker compose is already up and healthy at DOCKER_TEST_URL.
"""

import os
import time

import httpx
import pytest

pytestmark = pytest.mark.docker

DOCKER_BASE_URL = os.environ.get("DOCKER_TEST_URL", "http://localhost:8000")


def _wait_for_healthy(url: str, timeout: int = 60) -> bool:
    """Poll GET /api/health until 200 or timeout."""
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


@pytest.fixture(scope="module", autouse=True)
def require_docker():
    """Wait for the docker stack to be healthy before running tests."""
    if not _wait_for_healthy(DOCKER_BASE_URL, timeout=60):
        pytest.skip(
            f"Docker stack at {DOCKER_BASE_URL} did not become healthy within 60s. "
            "Start it with: docker compose up"
        )


def test_app_health():
    """Health endpoint returns 200 with status and version fields."""
    resp = httpx.get(f"{DOCKER_BASE_URL}/api/health", timeout=10)
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "version" in data


def test_app_returns_html_on_root():
    """GET / redirects unauthenticated callers — / always 302s to /login or /dashboard."""
    resp = httpx.get(f"{DOCKER_BASE_URL}/", timeout=10, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers.get("location") in ("/login", "/dashboard")


def test_bootstrap_creates_admin():
    """POST /auth/bootstrap creates the first admin user (409 if already done)."""
    resp = httpx.post(
        f"{DOCKER_BASE_URL}/auth/bootstrap",
        json={"email": "admin@docker-test.local", "name": "Docker Admin", "password": "test1234"},
        timeout=10,
    )
    # 200 = created, 409 = already bootstrapped — both are valid
    assert resp.status_code in (200, 409)


def test_trigger_sync():
    """Login then POST /api/sync/trigger returns accepted."""
    # First bootstrap or login to get a token
    bootstrap = httpx.post(
        f"{DOCKER_BASE_URL}/auth/bootstrap",
        json={"email": "admin@docker-test.local", "name": "Docker Admin", "password": "test1234"},
        timeout=10,
    )

    if bootstrap.status_code == 200:
        token = bootstrap.json()["access_token"]
    else:
        # Already bootstrapped — log in
        login = httpx.post(
            f"{DOCKER_BASE_URL}/auth/token",
            json={"email": "admin@docker-test.local", "password": "test1234"},
            timeout=10,
        )
        if login.status_code != 200:
            pytest.skip("Cannot obtain admin token — adjust credentials or bootstrap the stack")
        token = login.json()["access_token"]

    headers = {"Authorization": f"Bearer {token}"}
    resp = httpx.post(f"{DOCKER_BASE_URL}/api/sync/trigger", headers=headers, timeout=15)
    # 200 = started, 202 = accepted/queued
    assert resp.status_code in (200, 202)
