"""download_attachment's atomic publish — bounded temp name, atomic replace.

The temp-file publish (torn-read fix on the download endpoint) must not
change WHICH attachments can be stored: a name near the filesystem's
255-byte NAME_MAX saved fine with the old in-place write, so the temp name
is bounded rather than derived from the full name.

Both writers into the ``attachments/<ISSUE>/`` tree are covered: the
webhook path (``JiraService.download_attachment``) and the backfill
script's sibling downloader — the download endpoint serves whichever of
them wrote last, so neither may publish non-atomically.
"""

import os
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Backfill script's sibling downloader — same tree, same contract
# ---------------------------------------------------------------------------


@pytest.fixture
def backfill(tmp_path):
    from connectors.jira.scripts.backfill import Config as BackfillConfig
    from connectors.jira.scripts.backfill import JiraBackfill

    return JiraBackfill(
        BackfillConfig(
            jira_domain="example.atlassian.net",
            jira_email="bf@example.com",
            jira_api_token="t",
            data_dir=tmp_path,
        )
    )


def test_backfill_long_filename_still_stores(backfill):
    """~250-char basename: storable before the atomic publish, storable after."""
    long_name = "b" * 240 + ".bin"  # 244 chars + '77_' prefix = 247 < 255
    with patch("connectors.jira.scripts.backfill.httpx.Client", _fake_client(b"abc")):
        out = backfill.download_attachment(_attachment(long_name), "PROJ-2")
    assert out is not None
    assert out.read_bytes() == b"abc"
    assert out.name == f"77_{long_name}"


def test_backfill_publishes_via_temp_then_replace(backfill, monkeypatch):
    """The final name only ever appears via os.replace of a fully written,
    bounded-name temp in the same directory — never a direct in-place write
    (a webhook-driven transform in another process can catalogue the path
    mid-backfill, and the download endpoint would fstat a partial size)."""
    observed = []
    real_replace = os.replace

    def recording_replace(src, dst):
        observed.append((Path(src).name, Path(dst).name, Path(src).read_bytes(), Path(dst).exists()))
        return real_replace(src, dst)

    monkeypatch.setattr("connectors.jira.scripts.backfill.os.replace", recording_replace)
    with patch("connectors.jira.scripts.backfill.httpx.Client", _fake_client(b"xyz")):
        out = backfill.download_attachment(_attachment("report.pdf"), "PROJ-2")
    assert out is not None and out.read_bytes() == b"xyz"
    # Full body already in the temp at replace time; final name untouched until then.
    assert observed == [(f".tmp-{os.getpid()}-77_report.pdf", "77_report.pdf", b"xyz", False)]
    leftovers = [p.name for p in out.parent.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_backfill_crash_mid_write_leaves_no_partial_file(backfill):
    """A failure while writing the body must leave nothing under the final
    name (no torn file for the endpoint to serve) and clean up its temp."""

    class _ExplodingClient:
        def __init__(self, *a, **k): ...

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, auth=None):
            class _Resp:
                status_code = 200

                @property
                def content(self):
                    raise OSError("simulated failure mid-write")

            return _Resp()

    with patch("connectors.jira.scripts.backfill.httpx.Client", _ExplodingClient):
        with pytest.raises(OSError, match="mid-write"):
            backfill.download_attachment(_attachment("report.pdf"), "PROJ-3")
    issue_dir = backfill.attachments_dir / "PROJ-3"
    assert list(issue_dir.iterdir()) == []
