import pytest
from fastapi.testclient import TestClient


class FakeContainer:
    def __init__(self, name, status="running"):
        self.name, self.status = name, status
        self.removed = self.paused = self.unpaused = False

    def remove(self, force=False):
        self.removed = True

    def pause(self):
        self.paused = True

    def unpause(self):
        self.unpaused = True

    def logs(self, tail=200):
        return b"hello\n"


class FakeVolumes:
    """Separate from `FakeDocker`'s container/network tracking — a real
    Docker client keeps these namespaces independent, and conflating them
    (as a single shared `by_name`/`create()` would) both misfiles the
    cache-volume's fixup container under `by_name` and pollutes
    `networks_created`."""

    def __init__(self):
        self.names: set[str] = set()

    def get(self, name):
        if name not in self.names:
            import docker.errors

            raise docker.errors.NotFound(name)
        return _FakeVolume(name)

    def create(self, name, **kw):
        self.names.add(name)
        return _FakeVolume(name)


class _FakeVolume:
    def __init__(self, name):
        self.name = name


class FakeDocker:
    def __init__(self):
        self.run_calls = []
        self.by_name = {}
        self.networks_created = set()
        self.raise_on_run = None
        self.containers = self
        self.networks = self
        self.volumes = FakeVolumes()

    # containers API
    def run(self, image, **kw):
        if self.raise_on_run is not None:
            raise self.raise_on_run
        self.run_calls.append((image, kw))
        name = kw.get("name")
        if name is None:
            # Anonymous, synchronous fixup container (e.g. the cache-volume
            # chown in `_ensure_cache_volume`) — nothing to track by name.
            return None
        c = FakeContainer(name)
        self.by_name[name] = c
        return c

    def get(self, name):
        if name not in self.by_name:
            import docker.errors

            raise docker.errors.NotFound(name)
        return self.by_name[name]

    def list(self, all=True, filters=None, names=None):
        # containers.list(all=True) vs. networks.list(names=[...]) share
        # this one method, same as the real client aliases both APIs to `self`.
        if names is not None:
            return [n for n in names if n in self.networks_created]
        if filters and "name" in filters:
            wanted = filters["name"]
            return [c for c in self.by_name.values() if c.name in wanted]
        return list(self.by_name.values())

    # networks API (idempotent ensure)
    def create(self, name, **kw):
        self.networks_created.add(name)
        return None


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "tok")
    monkeypatch.setenv("APPS_RUNNER_IMAGE_PREFIX", "keboolapublic.azurecr.io/data-app-python-js")
    from services.apps_runner import api

    fake = FakeDocker()
    monkeypatch.setattr(api, "_docker", lambda: fake)
    return TestClient(api.app), fake, tmp_path


SPEC = lambda tmp: {
    "name": "agnes-dataapp-s",
    "image": "keboolapublic.azurecr.io/data-app-python-js:1.6.2",
    "labels": {"agnes.data-app": "app_1"},
    "network": "agnes-apps",
    "config_dir": str(tmp / "apps" / "s"),
    "cache_volume": "agnes-dataapp-cache-s",
    "mem_limit": "1g",
    "cpus": 1.0,
    "env": {"A": "1"},
}


def test_auth_required(client):
    c, _, tmp = client
    assert c.post("/apps/s/up", json={"spec": SPEC(tmp), "config_json": {}}).status_code == 401


def test_up_writes_config_and_runs(client):
    c, fake, tmp = client
    r = c.post(
        "/apps/s/up", headers={"X-Runner-Token": "tok"}, json={"spec": SPEC(tmp), "config_json": {"dataApp": {}}}
    )
    assert r.status_code == 200
    assert (tmp / "apps" / "s" / "config.json").exists()
    # run_calls[0] is the one-time cache-volume chown fixup (anonymous, no
    # "name" key); the named app container is the last call.
    image, kw = fake.run_calls[-1]
    assert kw["name"] == "agnes-dataapp-s"
    assert kw["detach"] is True
    assert fake.volumes.names == {"agnes-dataapp-cache-s"}


def test_up_rejects_foreign_image(client):
    c, _, tmp = client
    spec = SPEC(tmp) | {"image": "evil/image:1"}
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "tok"}, json={"spec": spec, "config_json": {}})
    assert r.status_code == 400
    assert r.json()["detail"] == "image_not_allowed"


def test_stop_and_status(client):
    c, fake, tmp = client
    c.post("/apps/s/up", headers={"X-Runner-Token": "tok"}, json={"spec": SPEC(tmp), "config_json": {}})
    r = c.post("/apps/s/stop", headers={"X-Runner-Token": "tok"}, json={"mode": "recreate"})
    assert r.status_code == 200
    assert fake.by_name["agnes-dataapp-s"].removed


def test_up_twice_removes_old_container_and_reruns(client):
    c, fake, tmp = client
    c.post("/apps/s/up", headers={"X-Runner-Token": "tok"}, json={"spec": SPEC(tmp), "config_json": {}})
    first = fake.by_name["agnes-dataapp-s"]
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "tok"}, json={"spec": SPEC(tmp), "config_json": {}})
    assert r.status_code == 200
    assert first.removed
    named_runs = [kw for _, kw in fake.run_calls if kw.get("name")]
    assert len(named_runs) == 2
    # the network — and the cache volume + its chown fixup — are created
    # once (idempotent), not once per `up`: 2 named app-container runs + 1
    # anonymous chown fixup = 3 total `run()` calls.
    assert len(fake.run_calls) == 3
    assert fake.networks_created == {"agnes-apps"}
    assert fake.volumes.names == {"agnes-dataapp-cache-s"}


def test_resume_unpauses_container(client):
    c, fake, _ = client
    fake.by_name["agnes-dataapp-s"] = FakeContainer("agnes-dataapp-s", status="paused")
    r = c.post("/apps/s/resume", headers={"X-Runner-Token": "tok"})
    assert r.status_code == 200
    assert r.json() == {"status": "running"}
    assert fake.by_name["agnes-dataapp-s"].unpaused


def test_resume_absent_is_404(client):
    c, _, _ = client
    r = c.post("/apps/s/resume", headers={"X-Runner-Token": "tok"})
    assert r.status_code == 404


def test_logs_returns_decoded_string(client):
    c, fake, _ = client
    fake.by_name["agnes-dataapp-s"] = FakeContainer("agnes-dataapp-s")
    r = c.get("/apps/s/logs", headers={"X-Runner-Token": "tok"})
    assert r.status_code == 200
    assert r.json() == {"logs": "hello\n"}


def test_logs_absent_is_404(client):
    c, _, _ = client
    r = c.get("/apps/s/logs", headers={"X-Runner-Token": "tok"})
    assert r.status_code == 404


def test_list_apps_filters_dataapp_names(client):
    c, fake, _ = client
    fake.by_name["agnes-dataapp-a"] = FakeContainer("agnes-dataapp-a")
    fake.by_name["agnes-dataapp-b"] = FakeContainer("agnes-dataapp-b", status="paused")
    fake.by_name["some-other-container"] = FakeContainer("some-other-container")
    r = c.get("/apps", headers={"X-Runner-Token": "tok"})
    assert r.status_code == 200
    names = {row["name"] for row in r.json()["apps"]}
    assert names == {"agnes-dataapp-a", "agnes-dataapp-b"}


def test_status_paused(client):
    c, fake, _ = client
    fake.by_name["agnes-dataapp-s"] = FakeContainer("agnes-dataapp-s", status="paused")
    r = c.get("/apps/s/status", headers={"X-Runner-Token": "tok"})
    assert r.status_code == 200
    assert r.json() == {"container": "paused", "ready": False}


def test_status_maps_exited_to_stopped(client):
    c, fake, _ = client
    fake.by_name["agnes-dataapp-s"] = FakeContainer("agnes-dataapp-s", status="exited")
    r = c.get("/apps/s/status", headers={"X-Runner-Token": "tok"})
    assert r.status_code == 200
    assert r.json() == {"container": "stopped", "ready": False}


def test_up_maps_image_not_found(client):
    c, fake, tmp = client
    import docker.errors

    fake.raise_on_run = docker.errors.ImageNotFound("no such image")
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "tok"}, json={"spec": SPEC(tmp), "config_json": {}})
    assert r.status_code == 400
    assert r.json()["detail"] == "image_not_found"


def test_up_maps_docker_api_error(client):
    c, fake, tmp = client
    import docker.errors

    fake.raise_on_run = docker.errors.APIError("daemon unavailable")
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "tok"}, json={"spec": SPEC(tmp), "config_json": {}})
    assert r.status_code == 502
    assert r.json()["detail"].startswith("docker_error:")
