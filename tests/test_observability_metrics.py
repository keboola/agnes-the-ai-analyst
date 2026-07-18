"""Tests for the Prometheus `/metrics` endpoint (three-plane wave 2D, task 1).

Named distinctly from `tests/test_metrics.py` — that pre-existing file covers
the unrelated business `MetricRepository` (`metric_definitions` table), not
this Prometheus scrape endpoint.

`app.observability.metrics` holds process-wide singletons (a dedicated
`CollectorRegistry` + the `Counter`/`Histogram` registered on it) — like the
module import itself, they persist for the life of the test process across
every `create_app()` call. Tests therefore assert *deltas* around a request
(or "this label never appears"/"at most one new label appears"), never
absolute counter values, since other tests in the same session may already
have recorded requests against the same series.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    from app.main import create_app

    app = create_app()
    return TestClient(app)


def _samples(metric_name):
    """Yield every prometheus_client Sample for `metric_name` off the dedicated REGISTRY."""
    from app.observability.metrics import REGISTRY

    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == metric_name:
                yield sample


def _counter_value(metric_name, **labels):
    for sample in _samples(metric_name):
        if all(sample.labels.get(k) == v for k, v in labels.items()):
            return sample.value
    return None


def _path_templates(metric_name):
    return {sample.labels.get("path_template") for sample in _samples(metric_name)}


class TestMetricsEndpoint:
    def test_metrics_returns_200_text_plain_with_http_requests_total(self, app_client):
        r = app_client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        assert "agnes_http_requests_total" in r.text

    def test_metrics_endpoint_is_unauthenticated(self, app_client):
        # No Authorization header at all — /metrics must not 401/403 like the
        # /api/* surface does (it's a scrape endpoint, like /healthz).
        r = app_client.get("/metrics")
        assert r.status_code == 200

    def test_request_increments_counter(self, app_client):
        from app.observability.metrics import replica_id

        before = (
            _counter_value(
                "agnes_http_requests_total",
                method="GET",
                path_template="/healthz",
                status="200",
                replica=replica_id(),
            )
            or 0.0
        )

        r = app_client.get("/healthz")
        assert r.status_code == 200

        after = _counter_value(
            "agnes_http_requests_total",
            method="GET",
            path_template="/healthz",
            status="200",
            replica=replica_id(),
        )
        assert after == before + 1.0

    def test_role_and_replica_labels_present(self, app_client):
        from app.observability.metrics import replica_id

        app_client.get("/healthz")
        sample = next(
            s
            for s in _samples("agnes_http_requests_total")
            if s.labels.get("path_template") == "/healthz" and s.labels.get("method") == "GET"
        )
        assert sample.labels.get("replica") == replica_id()
        assert sample.labels.get("role")  # non-empty

    def test_scraping_metrics_itself_is_not_recorded(self, app_client):
        # Skip-recording rule: /metrics must not show up as a path_template
        # label on its own series (would grow unboundedly under scraping).
        app_client.get("/metrics")
        app_client.get("/metrics")
        assert "/metrics" not in _path_templates("agnes_http_requests_total")

    def test_path_template_used_not_raw_path(self, app_client):
        # /cli/wheel/{wheel_name} always 404s in a fresh test DATA_DIR (no
        # wheel on disk) regardless of the filename requested — hitting it
        # with two very different filenames must collapse onto ONE
        # path_template label ("/cli/wheel/{wheel_name}"), not two distinct
        # raw-path labels. That's the cardinality guard the brief calls out.
        before = _path_templates("agnes_http_requests_total")

        r1 = app_client.get("/cli/wheel/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.whl")
        r2 = app_client.get("/cli/wheel/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.whl")
        assert r1.status_code == 404
        assert r2.status_code == 404

        after = _path_templates("agnes_http_requests_total")
        new_labels = after - before
        assert new_labels == {"/cli/wheel/{wheel_name}"}

        value = _counter_value(
            "agnes_http_requests_total",
            method="GET",
            path_template="/cli/wheel/{wheel_name}",
            status="404",
        )
        assert value is not None and value >= 2.0

    def test_unmatched_paths_collapse_not_leak_raw_path(self, app_client):
        # Whatever the app does with a totally bogus, high-entropy path (route
        # it to the catch-all template or the UNMATCHED_PATH constant), the
        # raw path itself must never become a label value, and two unrelated
        # bogus paths must not create two new distinct labels.
        before = _path_templates("agnes_http_requests_total")

        bogus_1 = "/totally-bogus-path-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        bogus_2 = "/totally-bogus-path-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        app_client.get(bogus_1)
        app_client.get(bogus_2)

        after = _path_templates("agnes_http_requests_total")
        assert bogus_1 not in after
        assert bogus_2 not in after
        assert len(after - before) <= 1

    def test_observe_http_helper(self):
        from app.observability.metrics import observe_http, replica_id

        before = (
            _counter_value(
                "agnes_http_requests_total",
                method="GET",
                path_template="/unit-test-path",
                status="200",
                replica=replica_id(),
            )
            or 0.0
        )
        observe_http("GET", "/unit-test-path", 200, 0.01)
        after = _counter_value(
            "agnes_http_requests_total",
            method="GET",
            path_template="/unit-test-path",
            status="200",
            replica=replica_id(),
        )
        assert after == before + 1.0

        duration_count = None
        for sample in _samples("agnes_http_request_duration_seconds_count"):
            if sample.labels.get("path_template") == "/unit-test-path":
                duration_count = sample.value
        assert duration_count is not None and duration_count >= 1.0

    def test_replica_id_is_hostname_colon_pid(self):
        import os
        import socket

        from app.observability.metrics import replica_id

        assert replica_id() == f"{socket.gethostname()}:{os.getpid()}"
        # Stable across repeated calls within the same process.
        assert replica_id() == replica_id()

    def test_registry_is_dedicated_not_the_global_default(self):
        from prometheus_client import REGISTRY as _GLOBAL_REGISTRY

        from app.observability.metrics import REGISTRY

        assert REGISTRY is not _GLOBAL_REGISTRY

    def test_500_error_recorded_when_exception_propagates(self, tmp_path, monkeypatch):
        """Verify that HTTP 500 metric is recorded even when an exception propagates
        through the middleware (rather than returning a response object).

        The outermost _observe_http_metrics middleware must record the 500 metric
        before re-raising, so that exceptions in inner middleware/handlers don't
        skip observability.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")

        # Create a minimal app with just the middleware we care about, plus a
        # test route that raises.
        app = FastAPI()

        # Import and attach the same middleware from create_app.
        import time as _time

        from app.observability.metrics import METRICS_PATH, UNMATCHED_PATH, observe_http

        @app.middleware("http")
        async def _observe_http_metrics(request, call_next):
            if request.url.path == METRICS_PATH:
                return await call_next(request)
            start = _time.monotonic()
            try:
                response = await call_next(request)
                duration = _time.monotonic() - start
                route = request.scope.get("route")
                path_template = route.path if route is not None else UNMATCHED_PATH
                observe_http(request.method, path_template, response.status_code, duration)
                return response
            except Exception:
                duration = _time.monotonic() - start
                route = request.scope.get("route")
                path_template = route.path if route is not None else UNMATCHED_PATH
                observe_http(request.method, path_template, 500, duration)
                raise

        @app.get("/test-error")
        def test_error_route():
            raise RuntimeError("Intentional test error")

        client = TestClient(app, raise_server_exceptions=False)

        from app.observability.metrics import replica_id

        before = (
            _counter_value(
                "agnes_http_requests_total",
                method="GET",
                path_template="/test-error",
                status="500",
                replica=replica_id(),
            )
            or 0.0
        )

        # Make request to the error route — should get a 500 response.
        response = client.get("/test-error")
        assert response.status_code == 500

        # Verify the 500 metric was recorded.
        after = _counter_value(
            "agnes_http_requests_total",
            method="GET",
            path_template="/test-error",
            status="500",
            replica=replica_id(),
        )
        assert after == before + 1.0
