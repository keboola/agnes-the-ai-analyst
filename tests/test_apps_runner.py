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


class FakeDocker:
    def __init__(self):
        self.run_calls = []
        self.by_name = {}
        self.containers = self
        self.networks = self
        self.volumes = self

    # containers API
    def run(self, image, **kw):
        self.run_calls.append((image, kw))
        c = FakeContainer(kw["name"])
        self.by_name[kw["name"]] = c
        return c

    def get(self, name):
        if name not in self.by_name:
            import docker.errors

            raise docker.errors.NotFound(name)
        return self.by_name[name]

    def list(self, all=True, filters=None):
        return list(self.by_name.values())

    # networks / volumes API (idempotent ensure)
    def create(self, name, **kw):
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
    image, kw = fake.run_calls[0]
    assert kw["name"] == "agnes-dataapp-s"
    assert kw["detach"] is True


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
