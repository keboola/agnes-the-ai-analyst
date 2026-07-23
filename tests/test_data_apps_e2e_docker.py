"""E2E: real Docker run of the upstream `data-app-python-js` runtime image.

Drives the REAL `services/apps_runner/api.py` FastAPI app in-process via
`TestClient`, wired to the REAL Docker SDK (`docker.from_env()`) — no fakes,
unlike `tests/test_apps_runner.py`. Everything else in the platform (the
registry, `agnes app` CLI, MCP tools, ingress proxy) is exercised against a
fake runner elsewhere; this test is the one place that proves the runtime
contract (spec §2 / §13) actually boots a real container.

Fixture app: a minimal Flask app + the fixed `keboola-config/` contract
(nginx :8888 -> 127.0.0.1:5000, supervisord starts `uv run flask`, `setup.sh`
runs `uv sync`). The runtime image's entrypoint clones `dataApp.git.repository`
from *inside* the container — rather than standing up a git server reachable
from the bridge network, this test reuses the runner's own `config_dir` bind
mount (already `/data` in the container in production) to smuggle in a bare
git repo at `/data/repo.git`, cloned via `file://`. No extra infrastructure,
no changes to the production mount contract.

Flow: `up()` -> poll `status()` until the container is `"running"` (timeout
300s) -> poll the container's published port until it answers 200 -> assert
200 -> `stop(mode="recreate")` -> `up()` again -> poll running + 200 again
(the wake path).

Note on `status()`'s own `ready` flag: the sidecar's readiness probe connects
to the container by its Docker-network DNS name (`agnes-dataapp-<slug>:8888`)
— that only resolves from *inside* the `agnes-apps` bridge network (which is
how the sidecar itself runs in production, per `docker-compose.yml`). This
test drives `api.py` in-process on the host instead (per the brief), so the
host can never resolve that name; polling the published port directly is the
equivalent, and stronger, signal for an end-to-end test — it proves the app
is actually reachable and serving, not just that a TCP handshake to a name
only the runner itself can resolve succeeds.

Gated behind Docker + an explicit opt-in — never runs in CI or the default
local suite (``pytest.ini``'s default addopts already excludes the `docker`
marker; the env-var gate is a second, explicit belt-and-suspenders opt-in for
humans invoking this file directly):

    AGNES_DATA_APPS_E2E=1 .venv/bin/pytest tests/test_data_apps_e2e_docker.py -m docker -q --timeout=600

Requires: Docker running locally, and network access to pull the public,
anonymously-pullable `keboolapublic.azurecr.io/data-app-python-js` image
(first run only — subsequent runs reuse the local image cache).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not os.environ.get("AGNES_DATA_APPS_E2E"),
        reason="set AGNES_DATA_APPS_E2E=1 to run (needs real Docker + a runtime-image pull)",
    ),
]

RUNTIME_IMAGE = "keboolapublic.azurecr.io/data-app-python-js:1.6.2_python-3.13_node-24"
IMAGE_PREFIX = "keboolapublic.azurecr.io/data-app-python-js"
SLUG = "e2e-flask"
CONTAINER_NAME = f"agnes-dataapp-{SLUG}"
NETWORK = "agnes-apps-e2e-test"
READY_TIMEOUT_S = 300
# pid-offset to reduce (not eliminate) collisions with a stray leftover
# process/container from a previous interrupted run on the same host.
MAPPED_PORT = 18000 + (os.getpid() % 1000)


def _write_fixture_app(repo_dir: Path) -> None:
    """Minimal Flask app + the fixed `keboola-config/` app-repo contract
    (spec §2): nginx routes :8888 -> the app's port, supervisord starts it,
    `setup.sh` installs deps."""
    (repo_dir / "app.py").write_text(
        "from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.get('/')\ndef index():\n    return 'ok'\n"
    )
    (repo_dir / "pyproject.toml").write_text(
        '[project]\nname = "e2e-fixture-app"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = ["flask"]\n'
    )
    cfg = repo_dir / "keboola-config"
    (cfg / "nginx" / "sites").mkdir(parents=True)
    (cfg / "supervisord" / "services").mkdir(parents=True)
    (cfg / "nginx" / "sites" / "default.conf").write_text(
        "server {\n"
        "    listen 8888;\n"
        "    location / {\n"
        "        proxy_pass http://127.0.0.1:5000;\n"
        "        proxy_set_header Host $host;\n"
        "    }\n"
        "}\n"
    )
    (cfg / "supervisord" / "services" / "app.conf").write_text(
        "[program:app]\n"
        "command=uv run flask --app app run --host 127.0.0.1 --port 5000\n"
        "directory=/app\n"
        "autostart=true\n"
        "autorestart=true\n"
    )
    setup_sh = cfg / "setup.sh"
    setup_sh.write_text("#!/bin/sh\nset -e\nuv sync\n")
    setup_sh.chmod(0o755)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _build_fixture_repo(tmp_path: Path) -> Path:
    """Commits the fixture app into a work tree, then bare-clones it into
    `cfg_dir/repo.git` — the path the runner bind-mounts to `/data` in the
    container, so the entrypoint's `git clone file:///data/repo.git` can
    reach it without any network git server."""
    work = tmp_path / "work"
    work.mkdir()
    _write_fixture_app(work)
    _git("init", "-b", "main", cwd=work)
    _git("add", ".", cwd=work)
    _git("-c", "user.email=e2e@test.local", "-c", "user.name=e2e", "commit", "-m", "init", cwd=work)

    cfg_dir = tmp_path / "cfgdir"
    cfg_dir.mkdir()
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(cfg_dir / "repo.git")],
        check=True,
        capture_output=True,
    )
    return cfg_dir


def _poll_container_running(client: TestClient, headers: dict, slug: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        r = client.get(f"/apps/{slug}/status", headers=headers)
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("container") == "running":
            return last
        time.sleep(2)
    pytest.fail(f"container never reached 'running' within {timeout}s; last status={last}")


def _poll_http_200(port: int, timeout: int) -> httpx.Response:
    """Poll the published port until it answers 200 — the app itself (uv
    sync + supervisord + nginx + flask) needs a few seconds to come up even
    after the container is `running`."""
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://localhost:{port}/", timeout=5)
            if resp.status_code == 200:
                return resp
        except httpx.TransportError as exc:
            last_error = exc
        time.sleep(2)
    pytest.fail(f"app never answered 200 on :{port} within {timeout}s (last error: {last_error})")


@pytest.fixture
def docker_available():
    if subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode != 0:
        pytest.skip("Docker not available")


def test_flask_fixture_app_boots_and_wakes(tmp_path, monkeypatch, docker_available):
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "e2e-test-token")
    monkeypatch.setenv("APPS_RUNNER_IMAGE_PREFIX", IMAGE_PREFIX)
    from services.apps_runner import api

    cfg_dir = _build_fixture_repo(tmp_path)

    spec = {
        "name": CONTAINER_NAME,
        "image": RUNTIME_IMAGE,
        "labels": {"agnes.data-app": SLUG},
        "network": NETWORK,
        "config_dir": str(cfg_dir),
        "cache_volume": f"agnes-dataapp-cache-{SLUG}-e2e",
        "mem_limit": "1g",
        "cpus": 1.0,
        "env": {},
        # test-only escape hatch — see up()'s docstring in services/apps_runner/api.py
        "ports": {"8888/tcp": MAPPED_PORT},
    }
    config_json = {"dataApp": {"git": {"repository": "file:///data/repo.git", "branch": "main"}, "secrets": {}}}

    client = TestClient(api.app)
    headers = {"X-Runner-Token": "e2e-test-token"}

    try:
        r = client.post(f"/apps/{SLUG}/up", headers=headers, json={"spec": spec, "config_json": config_json})
        assert r.status_code == 200, r.text

        _poll_container_running(client, headers, SLUG, READY_TIMEOUT_S)
        resp = _poll_http_200(MAPPED_PORT, READY_TIMEOUT_S)
        assert resp.status_code == 200

        # Wake path: stop (recreate == full removal), then deploy again.
        r = client.post(f"/apps/{SLUG}/stop", headers=headers, json={"mode": "recreate"})
        assert r.status_code == 200
        assert client.get(f"/apps/{SLUG}/status", headers=headers).json()["container"] == "absent"

        r = client.post(f"/apps/{SLUG}/up", headers=headers, json={"spec": spec, "config_json": config_json})
        assert r.status_code == 200, r.text

        _poll_container_running(client, headers, SLUG, READY_TIMEOUT_S)
        resp = _poll_http_200(MAPPED_PORT, READY_TIMEOUT_S)
        assert resp.status_code == 200
    finally:
        client.post(f"/apps/{SLUG}/stop", headers=headers, json={"mode": "recreate"})
