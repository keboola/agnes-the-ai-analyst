import re

from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient

from app.logging_config import request_id_var
from app.middleware.request_id import RequestIdMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo-rid")
    def echo_rid():
        return {"rid": request_id_var.get()}

    return app


def test_assigns_id_when_missing():
    client = TestClient(_make_app())
    resp = client.get("/echo-rid")
    assert resp.status_code == 200
    rid = resp.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{12}", rid)
    assert resp.json()["rid"] == rid


def test_passes_through_provided_id():
    client = TestClient(_make_app())
    resp = client.get("/echo-rid", headers={"X-Request-ID": "given-1234"})
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == "given-1234"
    assert resp.json()["rid"] == "given-1234"


def test_request_id_resets_after_request():
    client = TestClient(_make_app())
    client.get("/echo-rid")
    assert request_id_var.get() is None


def test_request_id_resets_after_exception():
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    client = TestClient(_make_app())
    client_boom = TestClient(app, raise_server_exceptions=False)
    resp = client_boom.get("/boom")
    assert resp.status_code == 500
    assert request_id_var.get() is None
    # Subsequent normal request still works (ContextVar not stuck)
    ok = client.get("/echo-rid")
    assert ok.status_code == 200


def test_sanitizes_log_forging_chars():
    client = TestClient(_make_app())
    resp = client.get("/echo-rid", headers={"X-Request-ID": "abc\r\nFAKE: pwned"})
    assert resp.status_code == 200
    rid = resp.headers["x-request-id"]
    assert "\n" not in rid and "\r" not in rid and " " not in rid
    assert rid.startswith("abcFAKEpwned")


def test_truncates_oversized_id():
    client = TestClient(_make_app())
    resp = client.get("/echo-rid", headers={"X-Request-ID": "a" * 200})
    assert resp.status_code == 200
    assert len(resp.headers["x-request-id"]) == 64


def test_background_task_sees_request_id():
    captured: dict[str, str | None] = {}

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    def _bg_task():
        captured["rid"] = request_id_var.get()

    @app.get("/with-bg")
    def with_bg(bg: BackgroundTasks):
        bg.add_task(_bg_task)
        return {"rid": request_id_var.get()}

    client = TestClient(app)
    resp = client.get("/with-bg", headers={"X-Request-ID": "bg-test-id"})
    assert resp.status_code == 200
    assert resp.json()["rid"] == "bg-test-id"
    assert captured["rid"] == "bg-test-id"
