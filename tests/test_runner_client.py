import httpx
import pytest

from src.data_apps.runner_client import RunnerClient, RunnerUnavailable


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
