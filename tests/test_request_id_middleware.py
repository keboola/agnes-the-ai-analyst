import re

from fastapi import FastAPI
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
