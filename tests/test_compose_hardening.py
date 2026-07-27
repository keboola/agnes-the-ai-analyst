"""Compose hardening: explicit CA bundle + file-descriptor headroom.

The DuckDB bigquery extension ships a statically linked libcurl whose
CA-bundle discovery is fragile under pressure (intermittent
``CURL error 77`` on new TLS handshakes while warm connections keep
working). Pinning ``CURL_CA_BUNDLE``/``SSL_CERT_FILE`` to the image's
CA bundle removes the discovery step entirely. The default 1024-fd
soft limit is likewise too small for a server juggling ~100 remote
tables, marketplace git clones, and parquet handles — fd exhaustion
during bursts makes libcurl's CA-file ``fopen`` fail with the same
error code.
"""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
CA_PATH = "/etc/ssl/certs/ca-certificates.crt"


def _service(name):
    return yaml.safe_load(COMPOSE.read_text())["services"][name]


def _env_dict(service):
    env = service.get("environment", [])
    if isinstance(env, dict):
        return {k: str(v) for k, v in env.items()}
    return {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env if "=" in e}


def test_app_pins_curl_ca_bundle():
    env = _env_dict(_service("app"))
    assert env.get("CURL_CA_BUNDLE") == CA_PATH
    assert env.get("SSL_CERT_FILE") == CA_PATH


def test_app_raises_nofile_ulimit():
    ulimits = _service("app").get("ulimits", {})
    nofile = ulimits.get("nofile")
    assert nofile is not None, "app service must raise the 1024-fd default"
    soft = nofile.get("soft") if isinstance(nofile, dict) else nofile
    assert int(soft) >= 65536


def test_scheduler_pins_curl_ca_bundle():
    env = _env_dict(_service("scheduler"))
    assert env.get("CURL_CA_BUNDLE") == CA_PATH
    assert env.get("SSL_CERT_FILE") == CA_PATH


def test_scheduler_raises_nofile_ulimit():
    ulimits = _service("scheduler").get("ulimits", {})
    nofile = ulimits.get("nofile")
    assert nofile is not None
    soft = nofile.get("soft") if isinstance(nofile, dict) else nofile
    assert int(soft) >= 65536


def test_apps_runner_can_reach_docker_socket():
    """apps-runner runs as the image's non-root uid 999 but bind-mounts the
    root:docker-owned docker socket. Without membership in the socket's group
    every up()/stop() fails the daemon handshake with PermissionError(13) ->
    502 runner_unavailable (found in a live E2E). It must add the host docker
    gid as a supplementary group, sourced from the DOCKER_GID env var so the
    per-host gid stays configurable, and stay uid 999 (not root) so the
    config.json it writes under /data stays owner-deletable by the app."""
    svc = _service("apps-runner")
    group_add = [str(g) for g in svc.get("group_add", [])]
    assert any("DOCKER_GID" in g for g in group_add), (
        "apps-runner must add ${DOCKER_GID} via group_add to reach the docker socket "
        f"as uid 999; got group_add={group_add!r}"
    )
    # Never pinned to root — root-owned config dirs would break app-side cleanup.
    assert str(svc.get("user", "")) not in ("0", "0:0", "root")
