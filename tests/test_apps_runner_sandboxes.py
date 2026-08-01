"""apps-runner `/sandboxes/*` API — chat-sandbox Docker operations.

The sidecar is the only process holding the Docker socket; the chat gateway
reaches it over this token-gated HTTP surface (see
`app/chat/sandbox_runner_client.py`). These tests mock the Docker SDK at the
sidecar's own `_docker()` seam — the same boundary `tests/test_apps_runner.py`
uses for `/apps/*` — so the real handler code runs against a fake daemon.
"""

import base64
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

NAME = "agnes-chatsbx-chat1-abcd1234"


class FakeSock:
    def __init__(self):
        self.written = b""
        self.closed = False

    def sendall(self, data):
        self.written += data

    def close(self):
        self.closed = True


class FakeSandboxContainer:
    def __init__(self, name, status="running", attrs=None):
        self.name, self.status = name, status
        self.attrs = attrs or {"State": {"ExitCode": 0}, "Created": "2026-08-01T10:00:00Z"}
        self.removed = self.paused = self.unpaused = False
        self.stopped_with = None
        self.attach_chunks = [(b'{"type":"runner_ready"}\n', None), (None, b"stderr-line\n")]
        self.archives = {}
        self.put_calls = []
        self.sock = FakeSock()
        self.labels = {"agnes.chat-sandbox": "1", "agnes.chat-session": "chat1"}

    # --- lifecycle -----------------------------------------------------
    def remove(self, force=False):
        self.removed = True

    def stop(self, timeout=10):
        self.stopped_with = timeout

    def pause(self):
        self.paused = True
        self.status = "paused"

    def unpause(self):
        self.unpaused = True
        self.status = "running"

    # --- streams -------------------------------------------------------
    def attach(self, **kw):
        assert kw.get("demux") is True
        assert kw.get("stream") is True
        self.attach_kwargs = kw
        return iter(self.attach_chunks)

    def attach_socket(self, params=None):
        self.attach_params = params
        return self.sock

    # --- files ---------------------------------------------------------
    def put_archive(self, path, data):
        self.put_calls.append((path, data))
        return True

    def get_archive(self, path):
        import docker.errors

        if path not in self.archives:
            raise docker.errors.NotFound(path)
        return iter([self.archives[path]]), {"name": path}


def _tar_bytes(members: dict[str, bytes], dirs: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for d in dirs:
            info = tarfile.TarInfo(name=d)
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeImages:
    def __init__(self):
        self.present = {"agnes-chat-sandbox:dev"}

    def get(self, name):
        import docker.errors

        if name not in self.present:
            raise docker.errors.ImageNotFound(name)
        return object()


class FakeDocker:
    def __init__(self):
        self.run_calls = []
        self.by_name = {}
        self.networks_created = []
        self.images = FakeImages()
        self.pinged = False
        self.containers = self
        self.networks = self

    # containers API
    def run(self, image, **kw):
        self.run_calls.append((image, kw))
        c = FakeSandboxContainer(kw["name"])
        self.by_name[kw["name"]] = c
        return c

    def get(self, name):
        if name not in self.by_name:
            import docker.errors

            raise docker.errors.NotFound(name)
        return self.by_name[name]

    def list(self, all=True, filters=None, names=None):
        if names is not None:
            return [n for n in names if n in {net for net, _ in self.networks_created}]
        if filters and "label" in filters:
            want = filters["label"]
            want = [want] if isinstance(want, str) else list(want)
            out = []
            for c in self.by_name.values():
                have = {f"{k}={v}" for k, v in c.labels.items()}
                if not [lbl for lbl in want if lbl not in have]:
                    out.append(c)
            return out
        return list(self.by_name.values())

    def ping(self):
        self.pinged = True
        return True

    # networks API
    def create(self, name, **kw):
        self.networks_created.append((name, kw))


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "tok")
    monkeypatch.setenv("CHAT_SANDBOX_IMAGE_PREFIX", "agnes-chat-sandbox")
    from services.apps_runner import api

    fake = FakeDocker()
    monkeypatch.setattr(api, "_docker", lambda: fake)
    return TestClient(api.app), fake, tmp_path


def SPEC(tmp, **over):
    spec = {
        "name": NAME,
        "image": "agnes-chat-sandbox:dev",
        "labels": {"agnes.chat-sandbox": "1", "agnes.chat-session": "chat1"},
        "network": "agnes-apps",
        "internal_network": False,
        "env": {"AGNES_SESSION_ID": "chat1", "HOME": "/home/user"},
        "cmd": ["python3", "/work/runner.py", "--session-id", "chat1"],
        "working_dir": "/work",
        "user": "999:999",
        "mem_limit": "2g",
        "cpus": 1.0,
        "pids_limit": 512,
        "mounts": [{"source": str(tmp / "sessions" / "chat1"), "target": "/work", "mode": "rw"}],
    }
    spec.update(over)
    return spec


def _up(c, tmp, **over):
    return c.post(f"/sandboxes/{NAME}/up", headers={"X-Runner-Token": "tok"}, json={"spec": SPEC(tmp, **over)})


# --- token gating ---------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", f"/sandboxes/{NAME}/up", {}),
        ("POST", f"/sandboxes/{NAME}/pause", None),
        ("POST", f"/sandboxes/{NAME}/resume", None),
        ("POST", f"/sandboxes/{NAME}/rm", None),
        ("POST", f"/sandboxes/{NAME}/stdin", {}),
        ("POST", f"/sandboxes/{NAME}/files", {}),
        ("GET", f"/sandboxes/{NAME}/status", None),
        ("GET", f"/sandboxes/{NAME}/stream", None),
        ("GET", f"/sandboxes/{NAME}/files?path=/work", None),
        ("GET", "/sandboxes", None),
        ("GET", "/sandboxes/probe", None),
    ],
)
def test_sandbox_routes_require_token(client, method, path, body):
    """Every /sandboxes/* route 401s without the token — no route skips the guard.

    Bodies are minimal valid JSON so FastAPI's request validation passes and the
    handler's own ``_guard``/``_check_token`` is what produces the 401.
    """
    c, _, _tmp = client
    if body is not None:
        r = c.request(method, path, json=body)
    else:
        r = c.request(method, path)
    assert r.status_code == 401


def test_sandbox_token_fails_closed_when_unset(client, monkeypatch):
    """Empty APPS_RUNNER_TOKEN rejects everything (never a silent bypass)."""
    c, _, tmp = client
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "")
    r = c.post(f"/sandboxes/{NAME}/up", headers={"X-Runner-Token": ""}, json={"spec": SPEC(tmp)})
    assert r.status_code == 401


# --- up: allowlists, hardening, mounts ------------------------------------


def test_up_rejects_image_outside_prefix(client):
    c, _, tmp = client
    r = _up(c, tmp, image="evil/image:1")
    assert r.status_code == 400
    assert r.json()["detail"] == "image_not_allowed"


def test_up_rejects_foreign_container_name(client):
    """The sidecar's chat API can only ever address agnes-chatsbx-* containers,
    so a compromised caller can't reach a data app (or any other container)."""
    c, _, tmp = client
    r = c.post(
        "/sandboxes/agnes-dataapp-s/up",
        headers={"X-Runner-Token": "tok"},
        json={"spec": SPEC(tmp, name="agnes-dataapp-s")},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "bad_sandbox_name"


def test_up_rejects_spec_name_mismatch(client):
    c, _, tmp = client
    r = c.post(
        f"/sandboxes/{NAME}/up",
        headers={"X-Runner-Token": "tok"},
        json={"spec": SPEC(tmp, name="agnes-chatsbx-other-1")},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "bad_sandbox_name"


def test_up_runs_hardened_container(client):
    c, fake, tmp = client
    r = _up(c, tmp)
    assert r.status_code == 200, r.text
    image, kw = fake.run_calls[-1]
    assert image == "agnes-chat-sandbox:dev"
    assert kw["name"] == NAME
    assert kw["detach"] is True
    assert kw["stdin_open"] is True
    assert kw["tty"] is False
    assert kw["command"] == ["python3", "/work/runner.py", "--session-id", "chat1"]
    assert kw["working_dir"] == "/work"
    assert kw["user"] == "999:999"
    assert kw["labels"]["agnes.chat-sandbox"] == "1"
    assert kw["network"] == "agnes-apps"
    assert kw["mem_limit"] == "2g"
    assert kw["nano_cpus"] == 1_000_000_000
    assert kw["pids_limit"] == 512
    assert kw["cap_drop"] == ["ALL"]
    assert kw["security_opt"] == ["no-new-privileges:true"]
    assert kw["extra_hosts"] == {"host.docker.internal": "host-gateway"}
    # A crashed runner must never be auto-restarted by Docker — the manager
    # owns respawn (crash_count / restore-context).
    assert "restart_policy" not in kw or kw["restart_policy"] is None
    assert kw["volumes"] == {str(tmp / "sessions" / "chat1"): {"bind": "/work", "mode": "rw"}}


def test_up_replaces_an_existing_container_of_the_same_name(client):
    c, fake, tmp = client
    _up(c, tmp)
    first = fake.by_name[NAME]
    assert _up(c, tmp).status_code == 200
    assert first.removed is True
    assert len(fake.run_calls) == 2


def test_up_creates_the_bridge_network_when_missing(client):
    c, fake, tmp = client
    _up(c, tmp)
    assert ("agnes-apps", {"driver": "bridge"}) in [
        (n, {"driver": kw.get("driver")}) for n, kw in fake.networks_created
    ]


def test_up_internal_network_is_created_internal(client):
    c, fake, tmp = client
    r = _up(c, tmp, internal_network=True, network="agnes-chat-internal")
    assert r.status_code == 200
    created = {n: kw for n, kw in fake.networks_created}
    assert created["agnes-chat-internal"]["internal"] is True
    _, kw = fake.run_calls[-1]
    assert kw["network"] == "agnes-chat-internal"


@pytest.mark.parametrize(
    "source",
    ["relative/path", "/data/../etc", "/var/run", "/var/run/docker.sock", "/", "/etc/shadow"],
)
def test_up_rejects_unsafe_mount_sources(client, tmp_path, source):
    c, _, tmp = client
    r = _up(c, tmp, mounts=[{"source": source, "target": "/work", "mode": "rw"}])
    assert r.status_code == 400
    assert r.json()["detail"] == "bad_mount"


def test_up_rejects_too_many_mounts(client):
    c, _, tmp = client
    mounts = [{"source": f"{tmp}/a{i}", "target": f"/m{i}", "mode": "rw"} for i in range(5)]
    r = _up(c, tmp, mounts=mounts)
    assert r.status_code == 400
    assert r.json()["detail"] == "bad_mount"


def test_up_resolves_bind_sources_via_dind(client, monkeypatch):
    """A gateway inside a container computes bind sources in ITS namespace;
    the daemon resolves them in the host's. Same translation `/apps/*` uses."""
    c, fake, tmp = client
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "runner123")
    fake.by_name["runner123"] = FakeSandboxContainer(
        "runner123",
        attrs={"Mounts": [{"Destination": str(tmp), "Source": "/var/lib/docker/volumes/proj_data/_data"}]},
    )
    r = _up(c, tmp)
    assert r.status_code == 200
    _, kw = fake.run_calls[-1]
    assert "/var/lib/docker/volumes/proj_data/_data/sessions/chat1" in kw["volumes"]
    # The in-container target is untouched — only the source is translated.
    assert next(iter(kw["volumes"].values()))["bind"] == "/work"


# --- streams --------------------------------------------------------------


def test_stream_demuxes_stdout_and_stderr(client):
    c, _fake, tmp = client
    _up(c, tmp)
    r = c.get(f"/sandboxes/{NAME}/stream", headers={"X-Runner-Token": "tok"})
    assert r.status_code == 200
    frames = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert frames[0]["stream"] == "stdout"
    assert base64.b64decode(frames[0]["data"]) == b'{"type":"runner_ready"}\n'
    assert frames[1]["stream"] == "stderr"
    assert base64.b64decode(frames[1]["data"]) == b"stderr-line\n"


def test_stream_replays_only_when_asked(client):
    """The first attach after create must replay (the runner can emit
    `runner_ready` before the gateway attaches); a post-unpause reattach must
    not, or every frame since session start is delivered twice."""
    c, fake, tmp = client
    _up(c, tmp)
    cont = fake.by_name[NAME]

    c.get(f"/sandboxes/{NAME}/stream", headers={"X-Runner-Token": "tok"})
    assert cont.attach_kwargs["logs"] is False

    c.get(f"/sandboxes/{NAME}/stream", headers={"X-Runner-Token": "tok"}, params={"replay": "true"})
    assert cont.attach_kwargs["logs"] is True


def test_stream_absent_container_is_404(client):
    c, _, _ = client
    assert c.get(f"/sandboxes/{NAME}/stream", headers={"X-Runner-Token": "tok"}).status_code == 404


def test_stdin_writes_bytes_to_the_attach_socket(client):
    c, fake, tmp = client
    _up(c, tmp)
    payload = base64.b64encode(b'{"type":"user_msg","text":"hi"}\n').decode()
    r = c.post(
        f"/sandboxes/{NAME}/stdin",
        headers={"X-Runner-Token": "tok"},
        json={"data_b64": payload},
    )
    assert r.status_code == 200
    cont = fake.by_name[NAME]
    assert cont.sock.written == b'{"type":"user_msg","text":"hi"}\n'
    assert cont.attach_params == {"stdin": 1, "stream": 1}


def test_stdin_absent_container_is_404(client):
    c, _, _ = client
    r = c.post(
        f"/sandboxes/{NAME}/stdin",
        headers={"X-Runner-Token": "tok"},
        json={"data_b64": base64.b64encode(b"x").decode()},
    )
    assert r.status_code == 404


# --- files ----------------------------------------------------------------


def test_write_file_puts_a_tar_rooted_at_slash(client):
    """Rooting the archive at `/` lets tar create the intermediate dirs
    (`/tmp/agnes-cli/`) that put_archive would otherwise require to exist."""
    c, fake, tmp = client
    _up(c, tmp)
    r = c.post(
        f"/sandboxes/{NAME}/files",
        headers={"X-Runner-Token": "tok"},
        json={"path": "/tmp/agnes-cli/agnes.whl", "content_b64": base64.b64encode(b"WHEEL").decode()},
    )
    assert r.status_code == 200
    path, blob = fake.by_name[NAME].put_calls[-1]
    assert path == "/"
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        assert tar.getnames() == ["tmp/agnes-cli/agnes.whl"]
        assert tar.extractfile("tmp/agnes-cli/agnes.whl").read() == b"WHEEL"


def test_read_file_returns_base64_content(client):
    c, fake, tmp = client
    _up(c, tmp)
    cont = fake.by_name[NAME]
    cont.archives["/work/outputs/report.csv"] = _tar_bytes({"report.csv": b"a,b\n1,2\n"})
    r = c.get(
        f"/sandboxes/{NAME}/files",
        headers={"X-Runner-Token": "tok"},
        params={"path": "/work/outputs/report.csv", "op": "read"},
    )
    assert r.status_code == 200
    assert base64.b64decode(r.json()["content_b64"]) == b"a,b\n1,2\n"


def test_list_dir_returns_entries(client):
    c, fake, tmp = client
    _up(c, tmp)
    cont = fake.by_name[NAME]
    cont.archives["/work/outputs"] = _tar_bytes(
        {"outputs/a.csv": b"x", "outputs/nested/b.csv": b"y"},
        dirs=("outputs/", "outputs/nested/"),
    )
    r = c.get(
        f"/sandboxes/{NAME}/files",
        headers={"X-Runner-Token": "tok"},
        params={"path": "/work/outputs", "op": "list"},
    )
    assert r.status_code == 200
    entries = {e["name"]: e for e in r.json()["entries"]}
    assert entries["a.csv"]["type"] == "FILE"
    assert entries["a.csv"]["path"] == "/work/outputs/a.csv"
    assert entries["nested"]["type"] == "DIR"
    # Only the top level — nested children are not flattened into the listing.
    assert "b.csv" not in entries


def test_read_missing_path_is_404(client):
    c, _fake, tmp = client
    _up(c, tmp)
    r = c.get(
        f"/sandboxes/{NAME}/files",
        headers={"X-Runner-Token": "tok"},
        params={"path": "/work/outputs/nope", "op": "read"},
    )
    assert r.status_code == 404


# --- lifecycle ------------------------------------------------------------


def test_pause_resume_rm_and_status(client):
    c, fake, tmp = client
    _up(c, tmp)
    cont = fake.by_name[NAME]

    assert c.get(f"/sandboxes/{NAME}/status", headers={"X-Runner-Token": "tok"}).json()["container"] == "running"

    assert c.post(f"/sandboxes/{NAME}/pause", headers={"X-Runner-Token": "tok"}).json() == {"status": "paused"}
    assert cont.paused is True
    assert c.get(f"/sandboxes/{NAME}/status", headers={"X-Runner-Token": "tok"}).json()["container"] == "paused"

    assert c.post(f"/sandboxes/{NAME}/resume", headers={"X-Runner-Token": "tok"}).json() == {"status": "running"}
    assert cont.unpaused is True

    assert c.post(f"/sandboxes/{NAME}/rm", headers={"X-Runner-Token": "tok"}).json() == {"status": "removed"}
    assert cont.removed is True


def test_status_absent_and_exit_code(client):
    c, fake, tmp = client
    r = c.get(f"/sandboxes/{NAME}/status", headers={"X-Runner-Token": "tok"})
    assert r.json() == {"container": "absent", "exit_code": None}
    _up(c, tmp)
    fake.by_name[NAME].status = "exited"
    fake.by_name[NAME].attrs = {"State": {"ExitCode": 3}}
    r = c.get(f"/sandboxes/{NAME}/status", headers={"X-Runner-Token": "tok"})
    assert r.json() == {"container": "stopped", "exit_code": 3}


def test_pause_absent_is_404(client):
    c, _, _ = client
    assert c.post(f"/sandboxes/{NAME}/pause", headers={"X-Runner-Token": "tok"}).status_code == 404


def test_resume_absent_is_404(client):
    c, _, _ = client
    assert c.post(f"/sandboxes/{NAME}/resume", headers={"X-Runner-Token": "tok"}).status_code == 404


def test_rm_with_grace_stops_before_removing(client):
    c, fake, tmp = client
    _up(c, tmp)
    cont = fake.by_name[NAME]
    r = c.post(f"/sandboxes/{NAME}/rm", headers={"X-Runner-Token": "tok"}, json={"grace_sec": 5})
    assert r.status_code == 200
    assert cont.stopped_with == 5
    assert cont.removed is True


def test_rm_absent_is_idempotent(client):
    c, _, _ = client
    r = c.post(f"/sandboxes/{NAME}/rm", headers={"X-Runner-Token": "tok"})
    assert r.status_code == 200
    assert r.json() == {"status": "absent"}


def test_list_sandboxes_filters_by_ownership_label(client):
    c, fake, tmp = client
    _up(c, tmp)
    other = FakeSandboxContainer("agnes-dataapp-s")
    other.labels = {"agnes.data-app": "app_1"}
    fake.by_name["agnes-dataapp-s"] = other
    r = c.get("/sandboxes", headers={"X-Runner-Token": "tok"})
    assert r.status_code == 200
    rows = r.json()["sandboxes"]
    assert [row["name"] for row in rows] == [NAME]
    assert rows[0]["chat_id"] == "chat1"
    assert rows[0]["status"] == "running"
    assert rows[0]["age_seconds"] >= 0


def test_probe_reports_daemon_and_image(client):
    c, fake, _ = client
    r = c.get("/sandboxes/probe", headers={"X-Runner-Token": "tok"}, params={"image": "agnes-chat-sandbox:dev"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "daemon": True, "image": True, "detail": "docker sandbox runner ready"}
    assert fake.pinged is True


def test_probe_reports_missing_image(client):
    c, _, _ = client
    r = c.get("/sandboxes/probe", headers={"X-Runner-Token": "tok"}, params={"image": "agnes-chat-sandbox:nope"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["daemon"] is True and body["image"] is False
    assert "agnes-chat-sandbox:nope" in body["detail"]


def test_probe_reports_unreachable_daemon(client, monkeypatch):
    c, fake, _ = client

    def _boom():
        import docker.errors

        raise docker.errors.DockerException("cannot connect")

    fake.ping = _boom
    r = c.get("/sandboxes/probe", headers={"X-Runner-Token": "tok"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["daemon"] is False
    assert "cannot connect" in body["detail"]


def test_up_maps_docker_api_error(client, tmp_path):
    c, fake, tmp = client

    def _boom(image, **kw):
        import docker.errors

        raise docker.errors.APIError("daemon unavailable")

    fake.run = _boom
    r = _up(c, tmp)
    assert r.status_code == 502
    assert r.json()["detail"].startswith("docker_error:")
