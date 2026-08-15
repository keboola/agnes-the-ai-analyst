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
COMPOSE_PROD = Path(__file__).resolve().parents[1] / "docker-compose.prod.yml"
COMPOSE_HOST_MOUNT = Path(__file__).resolve().parents[1] / "docker-compose.host-mount.yml"
CA_PATH = "/etc/ssl/certs/ca-certificates.crt"


class _OverrideLoader(yaml.SafeLoader):
    """SafeLoader that tolerates the Compose-spec ``!override`` merge tag.

    ``docker-compose.host-mount.yml`` tags every ``volumes:`` list with
    ``!override`` (replace the base list instead of appending to it), which
    plain ``yaml.safe_load`` refuses to construct. The tag carries no meaning
    for these guards — they only inspect the sequence it decorates.
    """


_OverrideLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node, deep=True))


def _load(path):
    return yaml.load(path.read_text(), Loader=_OverrideLoader)["services"]


def _service(name):
    return yaml.safe_load(COMPOSE.read_text())["services"][name]


def test_every_build_service_has_prod_image_override():
    """Every ``build: .`` service must get an ``image:`` override in
    docker-compose.prod.yml. Production/Terraform VMs extract only the compose
    files + host scripts into /opt/agnes (no Dockerfile / source on disk), so a
    ``build:`` service with no prod image override makes ``docker compose up``
    try to build from an empty dir and abort the whole stack's boot — which is
    exactly how enabling the ``apps`` profile bricked VM boot before this guard
    (apps-runner had no override)."""
    base = yaml.safe_load(COMPOSE.read_text())["services"]
    prod = yaml.safe_load(COMPOSE_PROD.read_text())["services"]
    build_services = {n for n, s in base.items() if isinstance(s, dict) and "build" in s}
    missing = [n for n in build_services if "image" not in (prod.get(n) or {})]
    assert not missing, (
        f"build: services with no image override in docker-compose.prod.yml: {sorted(missing)} "
        "— on a source-less prod VM these force a build and abort boot"
    )


def test_every_data_volume_service_has_host_mount_override():
    """Every service mounting the ``data`` named volume must get a
    ``volumes: !override`` entry in docker-compose.host-mount.yml.

    The overlay swaps the ``data`` named volume for a direct ``/data`` host
    bind — per service, because ``!override`` replaces one service's list.
    A service the overlay forgets keeps the named volume, so its ``/data`` is
    a DIFFERENT filesystem from every other container's, silently.

    That is not theoretical: ``apps-runner`` was missing here since it was
    added, so the sidecar wrote each data app's ``config.json`` into the
    named volume while the app container read ``/data`` off the host. The
    runtime container still worked (``_resolve_host_path`` in
    ``services/apps_runner/api.py`` maps the sidecar's own path back to a
    daemon-visible one), which is exactly why it went unnoticed — but
    ``app.api.data_apps._rmtree_config_dir`` deletes
    ``${DATA_DIR}/apps/<slug>`` from the APP's filesystem, hit
    ``FileNotFoundError``, and swallowed it. The config.json it promises to
    remove — carrying a service JWT and git credentials in plaintext — was
    never deleted on any host-mount deployment.

    Same bug class as ``test_every_build_service_has_prod_image_override``
    above: a service added to the base file, forgotten in an overlay.
    """
    base = _load(COMPOSE)
    overlay = _load(COMPOSE_HOST_MOUNT)

    def _mounts_data_volume(service):
        return any(isinstance(v, str) and v.split(":")[0] == "data" for v in (service.get("volumes") or []))

    def _binds_host_data(service):
        return any(isinstance(v, str) and v.split(":")[0] == "/data" for v in (service.get("volumes") or []))

    expected = {n for n, s in base.items() if isinstance(s, dict) and _mounts_data_volume(s)}
    missing = sorted(n for n in expected if not _binds_host_data(overlay.get(n) or {}))
    assert not missing, (
        f"services mounting the `data` named volume with no /data bind in "
        f"docker-compose.host-mount.yml: {missing} — on a host-mount deployment their "
        "/data is a separate filesystem from every other container's"
    )


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


def test_services_others_wait_on_declare_a_start_period():
    """A dependency gate is only as good as the boot window it allows.

    ``depends_on: {X: {condition: service_healthy}}`` makes compose refuse to
    start the dependent when X reports unhealthy. Without ``start_period`` the
    very first probes fire against a container that is still booting and count
    toward ``retries``, so a slow-but-fine boot is indistinguishable from a
    broken one — compose aborts with "dependency failed to start" and the
    dependents are created and never run. ``restart:`` does not rescue those:
    a container that never started is not a container that stopped.

    The failure is silent by construction. The deploy succeeds, the gating
    service is healthy moments later, and the dependents are simply absent —
    which on this stack means no scheduled sync and no background jobs until
    somebody notices a number that stopped moving.

    Asserted over every gate rather than one service so a new one cannot be
    added without a boot window.
    """
    services = yaml.safe_load(COMPOSE.read_text())["services"]

    gated = set()
    for spec in services.values():
        depends = (spec or {}).get("depends_on")
        if not isinstance(depends, dict):
            continue  # list form carries no condition, so no health gate
        gated |= {dep for dep, cfg in depends.items() if (cfg or {}).get("condition") == "service_healthy"}

    assert gated, "no service_healthy gates found — guard would assert nothing"

    missing = [
        name
        for name in sorted(gated)
        if name in services and not (services[name].get("healthcheck") or {}).get("start_period")
    ]
    assert not missing, (
        f"services gating others via service_healthy but declaring no healthcheck start_period: {missing}. "
        "Their dependents will be created and never started whenever boot outruns interval x retries."
    )


def test_egress_proxy_declares_a_liveness_probe():
    """A dead egress proxy must not stay dead and unnoticed (#1250).

    The proxy exited (code 3) and stayed down with nothing surfacing it.
    Security-wise that is fail-CLOSED, not fail-open — ``agnes-apps-internal``
    is ``internal: true`` and is the sandboxes' only network, so a dead proxy
    means no egress rather than unfiltered egress. What was missing is the
    signal: every sandbox silently loses outbound access while the service
    reports nothing, and ``restart: unless-stopped`` cannot rescue a process
    that exits on a config error rather than crashing.

    The probe is a TCP connect against the listener, NOT an HTTP request: the
    proxy speaks raw CONNECT/absolute-form and serves no health path, so a
    request probe would mark a perfectly healthy proxy unhealthy. It reads the
    port from ``EGRESS_LISTEN`` so the probe cannot drift away from the bind.
    """
    svc = yaml.safe_load(COMPOSE.read_text())["services"]["egress-proxy"]

    health = svc.get("healthcheck") or {}
    assert health, "egress-proxy declares no healthcheck — a dead proxy silently kills sandbox egress"

    probe = " ".join(health.get("test") or [])
    assert "EGRESS_LISTEN" in probe, (
        "the liveness probe must derive its port from EGRESS_LISTEN, else it drifts from the bind"
    )
    assert "http" not in probe.lower(), (
        "the proxy serves no health path — an HTTP probe reports a healthy proxy as unhealthy"
    )
    assert health.get("start_period"), "without start_period the first probes race the proxy's own boot"
