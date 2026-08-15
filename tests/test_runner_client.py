import httpx
import pytest

from src.data_apps.runner_client import RunnerClient, RunnerError, RunnerUnavailable


def _client(handler):
    return RunnerClient(base_url="http://runner", token="tok", transport=httpx.MockTransport(handler))


def test_up_sends_token_and_payload():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("x-runner-token")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"status": "started"})

    c = _client(handler)
    assert c.up("s", {"name": "n"}, {"dataApp": {}}) == {"status": "started"}
    assert seen["auth"] == "tok"
    assert seen["url"].endswith("/apps/s/up")


def test_unavailable_raises():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(RunnerUnavailable):
        _client(handler).status("s")


def test_sidecar_400_with_json_detail():
    def handler(request):
        return httpx.Response(400, json={"detail": "image_not_allowed"})

    c = _client(handler)
    with pytest.raises(RunnerError) as exc_info:
        c.up("s", {}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "image_not_allowed"


def test_sidecar_502_with_text_fallback():
    def handler(request):
        return httpx.Response(502, text="Bad Gateway")

    c = _client(handler)
    with pytest.raises(RunnerError) as exc_info:
        c.status("s")
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Bad Gateway"


def test_stop_url_construction():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["json"] = request.content
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    c.stop("myapp", mode="pause")
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/apps/myapp/stop")


def test_resume_url_construction():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    c.resume("myapp")
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/apps/myapp/resume")


def test_logs_url_construction():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"logs": "output"})

    c = _client(handler)
    result = c.logs("myapp", tail=500)
    assert seen["method"] == "GET"
    assert seen["url"].endswith("/apps/myapp/logs?tail=500")
    assert result == "output"


def _capture_timeouts(fn):
    """Run ``fn`` with ``httpx.Client`` replaced by a recorder, returning the
    ``timeout=`` each construction was handed. The real constructor is bound
    BEFORE patching — reaching for ``httpx.Client`` inside the stub would find
    the stub itself."""
    import src.data_apps.runner_client as mod

    real_client = httpx.Client
    seen = []

    def _recorder(**kwargs):
        seen.append(kwargs.get("timeout"))
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        return real_client(**kwargs)

    mod.httpx.Client = _recorder
    try:
        fn()
    finally:
        mod.httpx.Client = real_client
    return seen


def test_up_gets_a_pull_sized_timeout_the_other_calls_do_not():
    """`up` is the only call that can trigger a cold image pull.

    The runtime image is ~1.3 GB; on a VM that has never run a data app the
    daemon fetches it inside this one request. A 60 s budget aborts that pull
    mid-stream, docker-py's retried `create` then raises ImageNotFound, and
    the deploy fails with a message about an image that is merely slow to
    arrive. The read budget for `up` must be minutes, not seconds — while the
    cheap calls (status/stop/logs) stay short so a genuinely wedged sidecar
    is still detected quickly.
    """
    c = RunnerClient(base_url="http://runner", token="tok")
    up_timeout, status_timeout = _capture_timeouts(lambda: (c.up("s", {}, {}), c.status("s")))

    assert up_timeout >= 300, f"up must survive a cold 1.3 GB pull; got {up_timeout}s"
    assert status_timeout < up_timeout, "cheap calls must not inherit the pull-sized budget"


def test_up_timeout_is_operator_tunable(monkeypatch):
    """Link speed varies per deployment, so the budget cannot be a constant
    only we can change."""
    monkeypatch.setenv("APPS_RUNNER_UP_TIMEOUT", "900")
    c = RunnerClient(base_url="http://runner", token="tok")
    assert _capture_timeouts(lambda: c.up("s", {}, {}))[0] == 900
