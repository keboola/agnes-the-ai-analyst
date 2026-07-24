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
