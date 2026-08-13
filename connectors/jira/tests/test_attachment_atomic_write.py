"""download_attachment's atomic publish — bounded temp name, atomic replace.

The temp-file publish (torn-read fix on the download endpoint) must not
change WHICH attachments can be stored: a name near the filesystem's
255-byte NAME_MAX saved fine with the old in-place write, so the temp name
is bounded rather than derived from the full name.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from connectors.jira.service import Config, JiraService


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "JIRA_DATA_DIR", tmp_path)
    s = JiraService.__new__(JiraService)
    s.domain = "example.atlassian.net"
    s.email = "svc@example.com"
    s.api_token = "t"
    s.data_dir = tmp_path
    s.attachments_dir = tmp_path / "attachments"
    return s


def _fake_client(body: bytes):
    class _Client:
        def __init__(self, *a, **k): ...
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, auth=None):
            return SimpleNamespace(status_code=200, content=body)
    return _Client


def _attachment(filename: str) -> dict:
    return {
        "id": "77",
        "filename": filename,
        "size": 3,
        "content": "https://example.atlassian.net/rest/api/3/attachment/content/77",
    }


def test_long_filename_still_stores(svc):
    """~250-char basename: storable before the atomic publish, storable after."""
    long_name = "a" * 240 + ".bin"  # 244 chars + '77_' prefix = 247 < 255
    with patch("connectors.jira.service.httpx.Client", _fake_client(b"abc")):
        out = svc.download_attachment(_attachment(long_name), "PROJ-1")
    assert out is not None
    assert out.read_bytes() == b"abc"
    assert out.name == f"77_{long_name}"


def test_publish_is_atomic_and_leaves_no_temp(svc):
    with patch("connectors.jira.service.httpx.Client", _fake_client(b"xyz")):
        out = svc.download_attachment(_attachment("report.pdf"), "PROJ-1")
    assert out is not None and out.read_bytes() == b"xyz"
    leftovers = [p.name for p in out.parent.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []
