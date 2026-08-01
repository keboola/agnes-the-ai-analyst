"""apps-runner — chat-sandbox (`/sandboxes/*`) half of the Docker socket API.

Same posture as `/apps/*` (see ``services/apps_runner/api.py``): the sidecar is
the only process holding the Docker socket, it holds no policy, and every route
is token-gated with ``X-Runner-Token`` (fail-closed). The Agnes gateway decides
*what* to run — image, mounts, limits, argv — this module only translates that
into Docker calls. Its caller is ``app/chat/sandbox_runner_client.py``.

Two narrowings on top of the `/apps/*` posture, because these containers run
agent-authored code with ``permission_mode=bypassPermissions`` inside:

- **Name confinement.** Every route only addresses containers matching
  ``agnes-chatsbx-*`` (``_SANDBOX_NAME_RE``), so this API can never touch a data
  app, a compose service, or an operator's own container.
- **Mount confinement.** Bind sources must be absolute, ``..``-free, and outside
  the system prefixes in ``_DENY_MOUNT_PREFIXES`` (notably ``/var/run``, home of
  the Docker socket), capped at ``_MAX_MOUNTS``. Sources are translated through
  ``_resolve_host_path`` exactly like the `/apps/*` config-dir mount.

Attach transport: chunked NDJSON on ``GET /sandboxes/{name}/stream`` (one frame
per demultiplexed Docker chunk, payload base64 so byte-exactness survives) plus
one ``POST /sandboxes/{name}/stdin`` per host→runner JSON line. WebSocket was
the other candidate; NDJSON+POST won because this sidecar is a *sync* FastAPI
app (the Docker SDK's attach socket is blocking, and Starlette already runs sync
generators on a worker thread), stdin frames are low-rate JSON lines, and
reattach after ``docker unpause`` is just a new GET. The contract — byte-exact
JSONL both ways, reattachable — is the same either way.
"""

from __future__ import annotations

import base64
import functools
import io
import json
import os
import re
import tarfile
from datetime import UTC, datetime
from pathlib import PurePosixPath

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])

#: Container-name prefix every chat sandbox carries. Mirrors the `/apps/*`
#: ``agnes-dataapp-`` convention; enforced on every route (see module docstring).
SANDBOX_NAME_PREFIX = "agnes-chatsbx-"
_SANDBOX_NAME_RE = re.compile(rf"^{re.escape(SANDBOX_NAME_PREFIX)}[A-Za-z0-9][A-Za-z0-9._-]{{0,80}}$")

#: Ownership label the gateway stamps on every sandbox (orphan reconciliation).
OWNER_LABEL = "agnes.chat-sandbox"
SESSION_LABEL = "agnes.chat-session"

#: A chat sandbox needs exactly two binds (session dir + the user's workspace);
#: anything beyond that is a malformed or hostile spec.
_MAX_MOUNTS = 2
_DENY_MOUNT_PREFIXES = ("/var/run", "/var/lib/docker", "/etc", "/proc", "/sys", "/dev", "/boot", "/root")

#: Defensive ceiling on a single ``op=read`` response so a runaway file in the
#: outputs dir can't OOM the sidecar. Well above the artifact-harvest cap
#: (``chat.agent_api_artifact_max_bytes``, 25 MB default) that gates real reads.
_MAX_FILE_READ_BYTES = 64 * 1024 * 1024


def _api():
    """The `/apps/*` module, imported lazily.

    Lazy because ``api`` imports *this* module at its bottom to mount the
    router; a module-level back-import would make the cycle order-dependent.
    Going through the module object (rather than importing the helpers by
    value) also means tests that monkeypatch ``api._docker`` — the established
    sidecar seam — cover these handlers too.
    """
    from services.apps_runner import api as _mod

    return _mod


def _docker_errors(fn):
    """Delegate to ``api._docker_errors`` at call time (single source of truth
    for ImageNotFound → 400 / APIError → 502 mapping, no import-time cycle),
    wrapping ``fn`` once on first use rather than on every request."""

    wrapped = None

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        nonlocal wrapped
        if wrapped is None:
            wrapped = _api()._docker_errors(fn)
        return wrapped(*args, **kwargs)

    return wrapper


def _guard(name: str, x_runner_token: str | None):
    """Token + name gate every route runs first. Returns the Docker client."""
    _api()._check_token(x_runner_token)
    if not _SANDBOX_NAME_RE.match(name or ""):
        raise HTTPException(status_code=400, detail="bad_sandbox_name")
    return _api()._docker()


def _require_container(name: str):
    c = _api()._container(name)
    if c is None:
        raise HTTPException(status_code=404, detail="absent")
    return c


def _safe_abs_path(path: str) -> str:
    """An absolute, ``..``-free POSIX path, or 400."""
    if not path or not path.startswith("/") or ".." in PurePosixPath(path).parts:
        raise HTTPException(status_code=400, detail="bad_path")
    return path


def _validate_mounts(mounts) -> dict:
    """Turn the spec's mount list into docker-py's ``volumes`` dict.

    Sources are host-namespace-translated (``_resolve_host_path``) because a
    containerized gateway computes them in *its* mount namespace, while the
    daemon resolves bind sources in the host's.
    """
    if not isinstance(mounts, list) or len(mounts) > _MAX_MOUNTS:
        raise HTTPException(status_code=400, detail="bad_mount")
    volumes: dict = {}
    for m in mounts:
        if not isinstance(m, dict):
            raise HTTPException(status_code=400, detail="bad_mount")
        source = str(m.get("source") or "")
        target = str(m.get("target") or "")
        mode = str(m.get("mode") or "rw")
        if mode not in ("rw", "ro"):
            raise HTTPException(status_code=400, detail="bad_mount")
        for p in (source, target):
            if not p.startswith("/") or p == "/" or ".." in PurePosixPath(p).parts:
                raise HTTPException(status_code=400, detail="bad_mount")
        if any(source == d or source.startswith(d + "/") for d in _DENY_MOUNT_PREFIXES):
            raise HTTPException(status_code=400, detail="bad_mount")
        volumes[_api()._resolve_host_path(source)] = {"bind": target, "mode": mode}
    return volumes


def _state(container) -> str:
    """``running`` | ``paused`` | ``stopped`` — same folding as `/apps/*`."""
    if container.status == "paused":
        return "paused"
    if container.status == "running":
        return "running"
    return "stopped"


def _age_seconds(container) -> float:
    """Container age from ``attrs["Created"]``; 0.0 when unparseable.

    0.0 is the safe default: the orphan sweep skips young containers, so a
    parse failure can never make it reap a sandbox that is still being wired up.
    """
    raw = str((container.attrs or {}).get("Created") or "")
    if not raw:
        return 0.0
    try:
        cleaned = raw.replace("Z", "+00:00")
        if "." in cleaned:
            head, _, tail = cleaned.partition(".")
            frac = "".join(ch for ch in tail if ch.isdigit())[:6]
            offset = tail[len(frac) :].lstrip("0123456789")
            cleaned = f"{head}.{frac}{offset}"
        created = datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - created).total_seconds())


# ---------------------------------------------------------------------------
# probe (declared first: a literal path segment, never a {name})
# ---------------------------------------------------------------------------


@router.get("/probe")
def sandbox_probe(image: str = "", x_runner_token: str | None = Header(default=None)):
    """Readiness probe for the docker chat provider: daemon reachable + image
    present. Never raises on a Docker failure — the classified ``{ok, detail}``
    is what the boot gate and the admin test-connections surface render."""
    _api()._check_token(x_runner_token)
    import docker.errors

    daemon = False
    image_present = False
    detail = ""
    try:
        _api()._docker().ping()
        daemon = True
    except Exception as exc:  # noqa: BLE001 — classify, never raise to the caller
        detail = f"docker daemon unreachable: {exc}"
    if daemon:
        if not image:
            detail = "no sandbox image configured (chat.docker_image)"
        else:
            try:
                _api()._docker().images.get(image)
                image_present = True
                detail = "docker sandbox runner ready"
            except docker.errors.ImageNotFound:
                detail = f"sandbox image {image} not present on the Docker host — build it first"
            except Exception as exc:  # noqa: BLE001
                detail = f"docker image lookup failed: {exc}"
    return {"ok": daemon and image_present, "daemon": daemon, "image": image_present, "detail": detail}


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


@router.post("/{name}/up")
@_docker_errors
def sandbox_up(name: str, payload: dict = Body(...), x_runner_token: str | None = Header(default=None)):
    """Create + start a chat sandbox container from ``payload["spec"]``.

    Deliberately NO restart policy: a runner that dies must stay dead so
    ChatManager owns the respawn decision (crash counting, restore-context).
    Replaces any container of the same name — the gateway's names are
    deterministic per chat, so this doubles as leftover cleanup.
    """
    client = _guard(name, x_runner_token)
    spec = payload["spec"]
    if str(spec.get("name") or "") != name:
        raise HTTPException(status_code=400, detail="bad_sandbox_name")
    prefix = os.environ.get("CHAT_SANDBOX_IMAGE_PREFIX", "")
    image = str(spec.get("image") or "")
    if not prefix or not image.startswith(prefix + ":"):
        raise HTTPException(status_code=400, detail="image_not_allowed")
    volumes = _validate_mounts(spec.get("mounts") or [])

    network = str(spec.get("network") or "")
    if network and not client.networks.list(names=[network]):
        net_kwargs: dict = {"driver": "bridge"}
        if spec.get("internal_network"):
            # `internal` bridges have no route off the host: the sandbox can
            # only reach containers attached to the same network.
            net_kwargs["internal"] = True
        client.networks.create(network, **net_kwargs)

    old = _api()._container(name)
    if old is not None:
        old.remove(force=True)

    pids_limit = int(spec.get("pids_limit") or 0)
    client.containers.run(
        image,
        name=name,
        detach=True,
        # Keeps the container's stdin open across attach/detach cycles so the
        # gateway can push `user_msg` / `ticket_push` frames at any time
        # (StdinOnce stays false — docker only closes stdin on detach when it
        # is set).
        stdin_open=True,
        tty=False,
        command=spec.get("cmd") or None,
        working_dir=str(spec.get("working_dir") or "") or None,
        environment=spec.get("env") or {},
        labels=spec.get("labels") or {},
        network=network or None,
        volumes=volumes,
        mem_limit=spec.get("mem_limit") or None,
        nano_cpus=int(float(spec.get("cpus") or 1.0) * 1e9),
        pids_limit=pids_limit or None,
        user=str(spec.get("user") or "") or None,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        # Linux needs the explicit mapping for `host.docker.internal` to
        # resolve; bare-host operators point AGNES_INTERNAL_URL at it.
        extra_hosts={"host.docker.internal": "host-gateway"},
    )
    return {"status": "started"}


@router.post("/{name}/pause")
@_docker_errors
def sandbox_pause(name: str, x_runner_token: str | None = Header(default=None)):
    _guard(name, x_runner_token)
    _require_container(name).pause()
    return {"status": "paused"}


@router.post("/{name}/resume")
@_docker_errors
def sandbox_resume(name: str, x_runner_token: str | None = Header(default=None)):
    _guard(name, x_runner_token)
    _require_container(name).unpause()
    return {"status": "running"}


@router.post("/{name}/rm")
@_docker_errors
def sandbox_rm(
    name: str,
    payload: dict | None = Body(default=None),
    x_runner_token: str | None = Header(default=None),
):
    """Remove the sandbox. Idempotent: an already-absent one is a success.

    ``payload["grace_sec"] > 0`` stops the container first (SIGTERM, then
    SIGKILL after the grace) so a runner that installed a handler can flush;
    the default force-removes immediately.
    """
    _guard(name, x_runner_token)
    c = _api()._container(name)
    if c is None:
        return {"status": "absent"}
    grace = float((payload or {}).get("grace_sec") or 0)
    if grace > 0:
        try:
            c.stop(timeout=int(grace) or 1)
        except Exception:  # noqa: BLE001, S110 — a stop failure must not block removal
            pass
    c.remove(force=True)
    return {"status": "removed"}


@router.get("/{name}/status")
@_docker_errors
def sandbox_status(name: str, x_runner_token: str | None = Header(default=None)):
    """``running`` | ``paused`` | ``stopped`` | ``absent`` (+ exit code once
    stopped, which the provider's ``wait()`` reports to ChatManager)."""
    _guard(name, x_runner_token)
    c = _api()._container(name)
    if c is None:
        return {"container": "absent", "exit_code": None}
    state = _state(c)
    exit_code = None
    if state == "stopped":
        raw = ((c.attrs or {}).get("State") or {}).get("ExitCode")
        exit_code = int(raw) if raw is not None else None
    return {"container": state, "exit_code": exit_code}


@router.get("")
@_docker_errors
def list_sandboxes(x_runner_token: str | None = Header(default=None)):
    """Every container carrying the ownership label — the input to the
    gateway's orphan reconciliation sweep."""
    _api()._check_token(x_runner_token)
    rows = []
    for c in _api()._docker().containers.list(all=True, filters={"label": f"{OWNER_LABEL}=1"}):
        labels = c.labels or {}
        rows.append(
            {
                "name": c.name,
                "status": _state(c),
                "chat_id": labels.get(SESSION_LABEL, ""),
                "age_seconds": _age_seconds(c),
            }
        )
    return {"sandboxes": rows}


# ---------------------------------------------------------------------------
# streams
# ---------------------------------------------------------------------------


@router.get("/{name}/stream")
@_docker_errors
def sandbox_stream(
    name: str,
    replay: bool = False,
    x_runner_token: str | None = Header(default=None),
):
    """Attach to the container and stream demultiplexed output as NDJSON.

    One line per Docker chunk: ``{"stream": "stdout"|"stderr", "data": <b64>}``.

    ``replay`` decides whether Docker first re-sends what the container already
    wrote. The gateway sets it on the FIRST attach after create — the runner
    emits ``runner_ready`` (and can emit whole frames) in the milliseconds
    between start and attach, and without replay those frames are gone. On a
    post-``unpause`` reattach it must stay off, or every frame since session
    start would be delivered a second time.

    Iteration errors end the stream (the response has already started, so there
    is no status code left to change).
    """
    _guard(name, x_runner_token)
    container = _require_container(name)
    chunks = container.attach(stdout=True, stderr=True, stream=True, logs=replay, demux=True)

    def _frames():
        try:
            for out, err in chunks:
                if out:
                    yield json.dumps({"stream": "stdout", "data": base64.b64encode(out).decode()}) + "\n"
                if err:
                    yield json.dumps({"stream": "stderr", "data": base64.b64encode(err).decode()}) + "\n"
        except Exception:  # noqa: BLE001 — container gone / daemon blip ends the stream
            return

    return StreamingResponse(_frames(), media_type="application/x-ndjson")


@router.post("/{name}/stdin")
@_docker_errors
def sandbox_stdin(name: str, payload: dict = Body(...), x_runner_token: str | None = Header(default=None)):
    """Write one frame to the container's stdin.

    A fresh attach socket per frame (rather than a pinned one) keeps the
    sidecar stateless; safe because ``up`` creates the container with
    ``stdin_open=True`` and StdinOnce false, so detaching never EOFs the
    runner's stdin.
    """
    _guard(name, x_runner_token)
    container = _require_container(name)
    data = base64.b64decode(payload.get("data_b64") or "")
    sock = container.attach_socket(params={"stdin": 1, "stream": 1})
    try:
        raw = getattr(sock, "_sock", sock)
        if hasattr(raw, "sendall"):
            raw.sendall(data)
        else:  # pragma: no cover — SDK transports that hand back a file object
            raw.write(data)
            raw.flush()
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001, S110 — best-effort close; the write already landed
            pass
    return {"status": "ok", "bytes": len(data)}


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


@router.post("/{name}/files")
@_docker_errors
def sandbox_write_file(name: str, payload: dict = Body(...), x_runner_token: str | None = Header(default=None)):
    """Write one file into the container (agnes CLI wheel, restore-context).

    The archive is rooted at ``/`` with the path relative, so tar creates any
    missing intermediate directories (``/tmp/agnes-cli/``) — ``put_archive``
    against a non-existent directory would otherwise fail.
    """
    _guard(name, x_runner_token)
    container = _require_container(name)
    path = _safe_abs_path(str(payload.get("path") or ""))
    data = base64.b64decode(payload.get("content_b64") or "")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=path.lstrip("/"))
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    container.put_archive("/", buf.getvalue())
    return {"status": "written", "bytes": len(data)}


@router.get("/{name}/files")
@_docker_errors
def sandbox_read_files(
    name: str,
    path: str,
    op: str = "list",
    x_runner_token: str | None = Header(default=None),
):
    """``op=list`` → the directory's immediate entries; ``op=read`` → one file.

    Both ride ``get_archive`` (a tar of the path) so no in-container exec is
    needed — the sandbox stays a single-process container.
    """
    import docker.errors

    _guard(name, x_runner_token)
    container = _require_container(name)
    path = _safe_abs_path(path).rstrip("/") or "/"
    try:
        bits, _stat = container.get_archive(path)
    except docker.errors.NotFound as exc:
        raise HTTPException(status_code=404, detail="not_found") from exc

    blob = bytearray()
    for chunk in bits:
        blob.extend(chunk)
        if len(blob) > _MAX_FILE_READ_BYTES:
            raise HTTPException(status_code=413, detail="file_too_large")

    root = PurePosixPath(path).name
    with tarfile.open(fileobj=io.BytesIO(bytes(blob))) as tar:
        if op == "read":
            for member in tar.getmembers():
                if member.isfile():
                    fh = tar.extractfile(member)
                    content = fh.read() if fh is not None else b""
                    return {"content_b64": base64.b64encode(content).decode()}
            raise HTTPException(status_code=404, detail="not_found")
        entries = []
        for member in tar.getmembers():
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != root or len(parts) != 2:
                continue
            entries.append(
                {
                    "name": parts[1],
                    "path": f"{path}/{parts[1]}",
                    "type": "DIR" if member.isdir() else "FILE",
                }
            )
    return {"entries": entries}
