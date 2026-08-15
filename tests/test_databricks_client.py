"""Unit tests for connectors/databricks/client.py — fake session, no network."""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional

import pytest

from connectors.databricks.client import (
    ArrowResult,
    DatabricksApiError,
    DatabricksStatementClient,
    DatabricksStatementTimeoutError,
    validate_workspace_host,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Duck-typed requests.Session: scripted (method, path) → response queue."""

    def __init__(self):
        self.headers: Dict[str, str] = {}
        self.calls: List[Dict[str, Any]] = []
        self._script: List[FakeResponse] = []

    def mount(self, *_args, **_kwargs):
        pass

    def queue(self, *responses: FakeResponse):
        self._script.extend(responses)

    def request(self, method, url, timeout=None, json=None):
        self.calls.append({"method": method, "url": url, "json": json})
        if not self._script:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self._script.pop(0)

    def post(self, url, timeout=None, **kwargs):
        self.calls.append({"method": "POST", "url": url})
        return FakeResponse(200, {})

    def raise_for_status(self):  # pragma: no cover - not used
        pass


def _client(session: FakeSession, **kwargs) -> DatabricksStatementClient:
    return DatabricksStatementClient(
        host="https://dbc-test.cloud.databricks.com",
        token="tok-123",
        warehouse_id="wh-1",
        session=session,
        poll_interval_s=0.0,
        **kwargs,
    )


def _succeeded(payload_extra: Optional[dict] = None) -> dict:
    doc = {
        "statement_id": "st-1",
        "status": {"state": "SUCCEEDED"},
        "manifest": {"schema": {"columns": [{"name": "a"}, {"name": "b"}]}, "truncated": False},
        "result": {"data_array": [["1", "x"], ["2", "y"]]},
    }
    if payload_extra:
        doc.update(payload_extra)
    return doc


class TestHostValidation:
    def test_bare_host_upgraded_to_https(self):
        assert validate_workspace_host("adb-1.azuredatabricks.net") == "https://adb-1.azuredatabricks.net"

    def test_trailing_slash_stripped(self):
        assert validate_workspace_host("https://x.cloud.databricks.com/") == "https://x.cloud.databricks.com"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "http://x.cloud.databricks.com",
            "https://user:pw@x.cloud.databricks.com",
            "https://x.cloud.databricks.com/api",
            "https://x.cloud.databricks.com?a=1",
        ],
    )
    def test_rejects_non_https_userinfo_paths(self, bad):
        with pytest.raises(ValueError):
            validate_workspace_host(bad)


class TestExecuteRows:
    def test_bearer_header_set_and_rows_returned(self):
        session = FakeSession()
        session.queue(FakeResponse(200, _succeeded()))
        client = _client(session)
        columns, rows = client.execute_rows("SELECT 1")
        assert session.headers["Authorization"] == "Bearer tok-123"
        assert columns == ["a", "b"]
        assert rows == [["1", "x"], ["2", "y"]]
        body = session.calls[0]["json"]
        assert body["warehouse_id"] == "wh-1"
        assert body["disposition"] == "INLINE"
        assert body["format"] == "JSON_ARRAY"

    def test_polls_until_terminal(self):
        session = FakeSession()
        session.queue(
            FakeResponse(200, {"statement_id": "st-1", "status": {"state": "PENDING"}}),
            FakeResponse(200, {"statement_id": "st-1", "status": {"state": "RUNNING"}}),
            FakeResponse(200, _succeeded()),
        )
        client = _client(session)
        _cols, rows = client.execute_rows("SELECT 1")
        assert len(rows) == 2
        # 1 submit + 2 polls
        assert len(session.calls) == 3
        assert session.calls[1]["method"] == "GET"

    def test_failed_statement_raises_with_code(self):
        session = FakeSession()
        session.queue(
            FakeResponse(
                200,
                {
                    "statement_id": "st-1",
                    "status": {"state": "FAILED", "error": {"error_code": "BAD_REQUEST", "message": "boom"}},
                },
            )
        )
        with pytest.raises(DatabricksApiError) as exc_info:
            _client(session).execute_rows("SELECT 1")
        assert exc_info.value.code == "BAD_REQUEST"
        assert "boom" in str(exc_info.value)

    def test_http_error_carries_status(self):
        session = FakeSession()
        session.queue(FakeResponse(403, {"message": "denied", "error_code": "PERMISSION_DENIED"}))
        with pytest.raises(DatabricksApiError) as exc_info:
            _client(session).execute_rows("SELECT 1")
        assert exc_info.value.status == 403
        assert exc_info.value.code == "PERMISSION_DENIED"

    def test_truncated_inline_result_raises(self):
        session = FakeSession()
        doc = _succeeded()
        doc["manifest"]["truncated"] = True
        session.queue(FakeResponse(200, doc))
        with pytest.raises(DatabricksApiError) as exc_info:
            _client(session).execute_rows("SELECT 1")
        assert exc_info.value.code == "inline_result_truncated"

    def test_timeout_cancels_statement(self):
        session = FakeSession()
        session.queue(
            FakeResponse(200, {"statement_id": "st-9", "status": {"state": "PENDING"}}),
        )
        client = _client(session)
        with pytest.raises(DatabricksStatementTimeoutError):
            # Microscopic positive deadline: already expired by the time the
            # first PENDING poll-check runs (0/negative would mean "no deadline").
            client.execute_rows("SELECT slow()", timeout_s=1e-9)
        cancel_calls = [c for c in session.calls if c["url"].endswith("/st-9/cancel")]
        assert cancel_calls, "cancel endpoint was not called on deadline"


class TestArrowResult:
    def _arrow_stream_bytes(self) -> bytes:
        pa = pytest.importorskip("pyarrow")
        import pyarrow.ipc as ipc

        batch = pa.record_batch({"n": pa.array([1, 2, 3])})
        sink = io.BytesIO()
        with ipc.new_stream(sink, batch.schema) as writer:
            writer.write_batch(batch)
        return sink.getvalue()

    def test_iter_batches_fetches_presigned_link_without_auth(self, monkeypatch):
        stream = self._arrow_stream_bytes()
        seen: Dict[str, Any] = {}

        def fake_get(url, timeout=None, **kwargs):
            seen["url"] = url
            seen["kwargs"] = kwargs
            resp = FakeResponse(200)
            resp.content = stream
            resp.raise_for_status = lambda: None
            return resp

        monkeypatch.setattr("connectors.databricks.client.requests.get", fake_get)
        session = FakeSession()
        client = _client(session)
        doc = {
            "statement_id": "st-2",
            "status": {"state": "SUCCEEDED"},
            "manifest": {
                "truncated": False,
                "total_row_count": 3,
                "total_byte_count": len(stream),
                "total_chunk_count": 1,
                "schema": {"columns": [{"name": "n", "type_name": "LONG"}]},
            },
            "result": {"external_links": [{"chunk_index": 0, "external_link": "https://storage.example.com/x?sig=1"}]},
        }
        result = ArrowResult(client, doc)
        batches = list(result.iter_batches())
        assert sum(b.num_rows for b in batches) == 3
        assert seen["url"].startswith("https://storage.example.com/")
        # The presigned URL is the credential — the workspace bearer token
        # must not ride along (it would leak to the storage host).
        assert "headers" not in seen["kwargs"] or "Authorization" not in (seen["kwargs"].get("headers") or {})

    def test_refuses_non_https_external_link(self):
        session = FakeSession()
        client = _client(session)
        with pytest.raises(DatabricksApiError) as exc_info:
            client._download_external_link("http://storage.example.com/x")
        assert exc_info.value.code == "insecure_external_link"

    def test_missing_chunk_link_raises(self):
        session = FakeSession()
        session.queue(FakeResponse(200, {"external_links": []}))
        client = _client(session)
        doc = {
            "statement_id": "st-3",
            "status": {"state": "SUCCEEDED"},
            "manifest": {"total_chunk_count": 1, "schema": {"columns": []}},
            "result": {},
        }
        result = ArrowResult(client, doc)
        with pytest.raises(DatabricksApiError) as exc_info:
            list(result.iter_batches())
        assert exc_info.value.code == "missing_chunk_link"

    def test_byte_limit_passed_through(self):
        session = FakeSession()
        doc = {
            "statement_id": "st-4",
            "status": {"state": "SUCCEEDED"},
            "manifest": {"truncated": True, "total_chunk_count": 0, "schema": {"columns": []}},
            "result": {},
        }
        session.queue(FakeResponse(200, doc))
        client = _client(session)
        result = client.execute_to_arrow_batches("SELECT big()", byte_limit=1024)
        assert session.calls[0]["json"]["byte_limit"] == 1024
        assert session.calls[0]["json"]["disposition"] == "EXTERNAL_LINKS"
        assert session.calls[0]["json"]["format"] == "ARROW_STREAM"
        assert result.truncated is True
