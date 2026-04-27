"""Tests for cli.commands.sync._fetch_and_write_rules and _item_to_md."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli.commands.sync import _fetch_and_write_rules, _item_to_md


def _make_resp(mandatory=None, approved=None, status_code=200, raise_exc=None):
    """Build a mock httpx Response-like object for api_get."""
    resp = MagicMock()
    resp.status_code = status_code
    if raise_exc:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "mandatory": mandatory or [],
        "approved": approved or [],
        "token_estimate": 0,
        "token_budget": 6000,
    }
    return resp


class TestItemToMd:
    def test_renders_title_and_content(self):
        md = _item_to_md({"id": "x", "title": "T", "content": "C"})
        assert "# T" in md
        assert "C" in md

    def test_renders_domain_and_category(self):
        md = _item_to_md({"id": "x", "title": "T", "content": "C", "domain": "finance", "category": "policy"})
        assert "finance" in md
        assert "policy" in md

    def test_missing_title_falls_back(self):
        md = _item_to_md({"id": "x", "content": "C"})
        assert "Untitled" in md


class TestFetchAndWriteRules:
    def _bundle(self, mandatory=None, approved=None):
        return _make_resp(mandatory=mandatory, approved=approved)

    def test_writes_mandatory_item_file(self, tmp_path):
        mandatory = [{"id": "km001", "title": "Rule", "content": "Body", "domain": None, "category": None}]
        with patch("cli.commands.sync.api_get", return_value=self._bundle(mandatory=mandatory)):
            _fetch_and_write_rules(tmp_path)
        assert (tmp_path / ".claude" / "rules" / "km_km001.md").exists()

    def test_writes_approved_bundle_file(self, tmp_path):
        approved = [{"id": "a1", "title": "App Rule", "content": "Body", "domain": None, "category": None}]
        with patch("cli.commands.sync.api_get", return_value=self._bundle(approved=approved)):
            _fetch_and_write_rules(tmp_path)
        assert (tmp_path / ".claude" / "rules" / "km_approved.md").exists()

    def test_prunes_stale_mandatory_files(self, tmp_path):
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        stale = rules_dir / "km_old_item.md"
        stale.write_text("stale content")

        with patch("cli.commands.sync.api_get", return_value=self._bundle()):
            _fetch_and_write_rules(tmp_path)
        assert not stale.exists()

    def test_prunes_stale_approved_file_when_none_qualifies(self, tmp_path):
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        stale = rules_dir / "km_approved.md"
        stale.write_text("old approved")

        with patch("cli.commands.sync.api_get", return_value=self._bundle(approved=[])):
            _fetch_and_write_rules(tmp_path)
        assert not stale.exists()

    def test_best_effort_on_network_error(self, tmp_path):
        """Sync must continue (no exception raised) if the bundle endpoint is unreachable."""
        with patch("cli.commands.sync.api_get", side_effect=Exception("connection refused")):
            _fetch_and_write_rules(tmp_path)  # must not raise

    def test_best_effort_on_http_error(self, tmp_path):
        """Sync must continue even when raise_for_status() raises."""
        import httpx
        resp = _make_resp(raise_exc=httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock()))
        with patch("cli.commands.sync.api_get", return_value=resp):
            _fetch_and_write_rules(tmp_path)  # must not raise

    def test_unsafe_id_is_skipped(self, tmp_path):
        """Mandatory item with path-traversal id must be skipped — no file written."""
        malicious = [{"id": "../../../etc/passwd", "title": "Bad", "content": "X"}]
        with patch("cli.commands.sync.api_get", return_value=self._bundle(mandatory=malicious)):
            _fetch_and_write_rules(tmp_path)
        rules_dir = tmp_path / ".claude" / "rules"
        # No km_ file should exist (the malicious id was rejected)
        written = list(rules_dir.glob("km_*.md")) if rules_dir.exists() else []
        assert written == []
