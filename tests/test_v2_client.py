# tests/test_v2_client.py
import json
import pyarrow as pa
import pytest
from unittest.mock import MagicMock, patch

from cli.v2_client import (
    api_get_json,
    api_post_arrow,
    api_post_json,
    V2ClientError,
)


def _fake_response(*, status=200, json_body=None, arrow_body=None, content_type=None):
    resp = MagicMock()
    resp.status_code = status
    if json_body is not None:
        resp.json.return_value = json_body
        resp.text = json.dumps(json_body)
        resp.content = resp.text.encode()
    if arrow_body is not None:
        resp.content = arrow_body
    if content_type:
        resp.headers = {"content-type": content_type}
    else:
        resp.headers = {}
    return resp


class TestApiGetJson:
    def test_200_returns_parsed_json(self):
        with patch("cli.v2_client.requests.get") as m:
            m.return_value = _fake_response(json_body={"hello": "world"})
            assert api_get_json("/api/v2/catalog") == {"hello": "world"}

    def test_4xx_raises_v2clienterror(self):
        with patch("cli.v2_client.requests.get") as m:
            m.return_value = _fake_response(status=403, json_body={"detail": "nope"})
            with pytest.raises(V2ClientError) as e:
                api_get_json("/api/v2/catalog")
            assert e.value.status_code == 403


class TestApiPostArrow:
    def test_returns_arrow_table(self):
        from app.api.v2_arrow import arrow_table_to_ipc_bytes
        ipc = arrow_table_to_ipc_bytes(pa.table({"x": [1, 2, 3]}))
        with patch("cli.v2_client.requests.post") as m:
            m.return_value = _fake_response(
                arrow_body=ipc,
                content_type="application/vnd.apache.arrow.stream",
            )
            got = api_post_arrow("/api/v2/scan", {"table_id": "x"})
        assert got.num_rows == 3
        assert got.column_names == ["x"]
