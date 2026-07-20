from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.health_probes import ReadinessState, readiness, register_readiness_check, router


def make_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_healthz_always_alive():
    assert make_client().get("/healthz").json() == {"status": "alive"}


def test_hysteresis_three_fails_two_recoveries():
    st = ReadinessState()
    assert st.is_ready()
    st.record_canary(False)
    st.record_canary(False)
    assert st.is_ready(), "two failures must not flip (hysteresis)"
    st.record_canary(False)
    assert not st.is_ready(), "third consecutive failure flips to not-ready"
    st.record_canary(True)
    assert not st.is_ready(), "one success must not recover"
    st.record_canary(True)
    assert st.is_ready(), "two consecutive successes recover"


def test_readyz_reflects_singleton(monkeypatch):
    client = make_client()
    for _ in range(3):
        readiness.record_canary(False)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"
    for _ in range(2):
        readiness.record_canary(True)
    assert client.get("/readyz").status_code == 200


def test_extra_check_gates_readyz():
    client = make_client()
    flag = {"ok": True}
    register_readiness_check("t_extra", lambda: flag["ok"])
    try:
        assert client.get("/readyz").status_code == 200
        flag["ok"] = False
        r = client.get("/readyz")
        assert r.status_code == 503
        assert "t_extra" in str(r.json()["failed_checks"])
    finally:
        from app.api import health_probes

        health_probes._extra_checks.pop("t_extra", None)
