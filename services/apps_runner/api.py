"""apps-runner — the only process holding the Docker socket.

Deliberately dumb: no registry access, no RBAC, no policy. The Agnes app
decides *what* should run; this sidecar only translates to Docker calls.
Bound on the internal compose network only; token-gated.
"""

from __future__ import annotations

import functools
import json
import os
import socket
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException

app = FastAPI(title="agnes-apps-runner", docs_url=None, redoc_url=None)


def _docker():
    import docker

    return docker.from_env()


def _check_token(x_runner_token: str | None) -> None:
    expected = os.environ.get("APPS_RUNNER_TOKEN", "")
    if not expected or x_runner_token != expected:
        raise HTTPException(status_code=401, detail="bad_runner_token")


def _container(name: str):
    import docker.errors

    try:
        return _docker().containers.get(name)
    except docker.errors.NotFound:
        return None


def _docker_errors(fn):
    """Map Docker SDK / transport errors to structured HTTP responses.

    ``docker.errors.ImageNotFound`` -> 400 ``image_not_found`` (bad spec, the
    caller's fault). Anything else that means "couldn't talk to Docker" —
    ``docker.errors.APIError`` (covers ``NotFound`` races too),
    ``docker.errors.DockerException``, or a raw
    ``requests.exceptions.ConnectionError`` from the transport — becomes a
    502 ``docker_error: <message>``. ``HTTPException`` raised deliberately by
    the handler (401/400/404) passes through unchanged.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        import docker.errors
        import requests.exceptions

        try:
            return fn(*args, **kwargs)
        except docker.errors.ImageNotFound as exc:
            raise HTTPException(status_code=400, detail="image_not_found") from exc
        except (docker.errors.APIError, docker.errors.DockerException, requests.exceptions.ConnectionError) as exc:
            raise HTTPException(status_code=502, detail=f"docker_error: {exc}") from exc

    return wrapper


@app.post("/apps/{slug}/up")
@_docker_errors
def up(slug: str, payload: dict = Body(...), x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    spec, config_json = payload["spec"], payload["config_json"]
    prefix = os.environ.get("APPS_RUNNER_IMAGE_PREFIX", "")
    if not prefix or not str(spec["image"]).startswith(prefix + ":"):
        raise HTTPException(status_code=400, detail="image_not_allowed")
    cfg_dir = Path(spec["config_dir"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(config_json, indent=2))
    client = _docker()
    if not client.networks.list(names=[spec["network"]]):
        client.networks.create(spec["network"], driver="bridge")
    old = _container(spec["name"])
    if old is not None:
        old.remove(force=True)
    client.containers.run(
        spec["image"],
        name=spec["name"],
        detach=True,
        labels=spec["labels"],
        network=spec["network"],
        environment=spec["env"],
        mem_limit=spec["mem_limit"],
        nano_cpus=int(float(spec["cpus"]) * 1e9),
        volumes={
            str(cfg_dir): {"bind": "/data", "mode": "rw"},
            spec["cache_volume"]: {"bind": "/home/app/.cache", "mode": "rw"},
        },
        restart_policy={"Name": "unless-stopped"},
    )
    return {"status": "started"}


@app.post("/apps/{slug}/stop")
@_docker_errors
def stop(slug: str, payload: dict = Body(...), x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        return {"status": "absent"}
    if payload.get("mode") == "pause":
        c.pause()
        return {"status": "paused"}
    c.remove(force=True)
    return {"status": "removed"}


@app.post("/apps/{slug}/resume")
@_docker_errors
def resume(slug: str, x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        raise HTTPException(status_code=404, detail="absent")
    c.unpause()
    return {"status": "running"}


@app.get("/apps/{slug}/status")
@_docker_errors
def status(slug: str, x_runner_token: str | None = Header(default=None)):
    """Container status contract — exactly one of:

    ``"running" | "paused" | "stopped" | "absent"``

    Any other Docker-reported state (``exited``, ``created``, ``restarting``,
    ``dead``, ...) for a container that still exists is folded into
    ``"stopped"``; ``ready`` is only ever true for ``"running"``.
    """
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        return {"container": "absent", "ready": False}
    if c.status == "paused":
        state = "paused"
    elif c.status == "running":
        state = "running"
    else:
        state = "stopped"
    ready = False
    if state == "running":
        try:
            with socket.create_connection((f"agnes-dataapp-{slug}", 8888), timeout=2):
                ready = True
        except OSError:
            ready = False
    return {"container": state, "ready": ready}


@app.get("/apps/{slug}/logs")
@_docker_errors
def logs(slug: str, tail: int = 200, x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        raise HTTPException(status_code=404, detail="absent")
    return {"logs": c.logs(tail=tail).decode("utf-8", errors="replace")}


@app.get("/apps")
@_docker_errors
def list_apps(x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    rows = [
        {"name": c.name, "status": c.status}
        for c in _docker().containers.list(all=True)
        if c.name.startswith("agnes-dataapp-")
    ]
    return {"apps": rows}
