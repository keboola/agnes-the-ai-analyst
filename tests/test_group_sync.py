"""Unit tests for app.auth.group_sync.fetch_user_groups."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Mock env flag
# ---------------------------------------------------------------------------


class TestMockFlag:
    def test_returns_parsed_list(self, monkeypatch):
        monkeypatch.setenv(
            "GOOGLE_ADMIN_SDK_MOCK_GROUPS",
            "grp_a@groupon.com, grp_b@groupon.com , grp_c@groupon.com",
        )
        from app.auth.group_sync import fetch_user_groups
        assert fetch_user_groups("any@x") == [
            "grp_a@groupon.com",
            "grp_b@groupon.com",
            "grp_c@groupon.com",
        ]

    def test_empty_value_returns_empty_list(self, monkeypatch):
        """Setting the flag to the empty string returns [] — explicit 'no groups'."""
        monkeypatch.setenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", "")
        from app.auth.group_sync import fetch_user_groups
        assert fetch_user_groups("any@x") == []

    def test_single_value_no_comma(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", "solo@groupon.com")
        from app.auth.group_sync import fetch_user_groups
        assert fetch_user_groups("any@x") == ["solo@groupon.com"]

    def test_trailing_commas_are_skipped(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", "a@x, , ,b@x,,")
        from app.auth.group_sync import fetch_user_groups
        assert fetch_user_groups("u@x") == ["a@x", "b@x"]


# ---------------------------------------------------------------------------
# Real path (monkeypatched Google client)
# ---------------------------------------------------------------------------


def _make_service_mock(pages: list[dict]) -> mock.Mock:
    """Build a mock for `service.groups().memberships().searchTransitiveGroups(...).execute()`
    that returns the given pages in order."""
    page_iter = iter(pages)

    def execute_side_effect(*_a, **_kw):
        return next(page_iter)

    search = mock.Mock()
    search.return_value.execute.side_effect = execute_side_effect
    memberships = mock.Mock()
    memberships.return_value.searchTransitiveGroups = search
    groups = mock.Mock()
    groups.return_value.memberships = memberships
    service = mock.Mock()
    service.groups = groups
    return service, search


class TestRealPath:
    def test_success_single_page(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)
        service, search = _make_service_mock(
            [
                {
                    "memberships": [
                        {"groupKey": {"id": "grp_a@groupon.com"}},
                        {"groupKey": {"id": "grp_b@groupon.com"}},
                    ]
                    # no nextPageToken
                }
            ]
        )
        monkeypatch.setattr(
            "google.auth.default",
            lambda scopes=None: (mock.Mock(), "test-project"),
        )
        monkeypatch.setattr(
            "googleapiclient.discovery.build",
            lambda *a, **kw: service,
        )

        from app.auth.group_sync import fetch_user_groups
        result = fetch_user_groups("user@groupon.com")
        assert result == ["grp_a@groupon.com", "grp_b@groupon.com"]

        # CEL query contains email + discussion_forum label filter
        call_kwargs = search.call_args.kwargs
        assert call_kwargs["parent"] == "groups/-"
        assert "member_key_id == 'user@groupon.com'" in call_kwargs["query"]
        assert "discussion_forum" in call_kwargs["query"]

    def test_success_paginated(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)
        service, search = _make_service_mock(
            [
                {
                    "memberships": [{"groupKey": {"id": "page1@x"}}],
                    "nextPageToken": "tok1",
                },
                {
                    "memberships": [{"groupKey": {"id": "page2@x"}}],
                    # terminal
                },
            ]
        )
        monkeypatch.setattr(
            "google.auth.default",
            lambda scopes=None: (mock.Mock(), "test-project"),
        )
        monkeypatch.setattr(
            "googleapiclient.discovery.build",
            lambda *a, **kw: service,
        )

        from app.auth.group_sync import fetch_user_groups
        result = fetch_user_groups("u@x")
        assert result == ["page1@x", "page2@x"]

        # Second call should have pageToken=tok1
        assert search.call_args_list[1].kwargs["pageToken"] == "tok1"

    def test_api_exception_returns_empty(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)

        def raise_boom(*a, **kw):
            raise RuntimeError("boom")

        service = mock.Mock()
        service.groups.return_value.memberships.return_value.searchTransitiveGroups.return_value.execute.side_effect = raise_boom
        monkeypatch.setattr(
            "google.auth.default",
            lambda scopes=None: (mock.Mock(), "test-project"),
        )
        monkeypatch.setattr(
            "googleapiclient.discovery.build",
            lambda *a, **kw: service,
        )

        from app.auth.group_sync import fetch_user_groups
        assert fetch_user_groups("user@x") == []

    def test_client_init_exception_returns_empty(self, monkeypatch):
        """Errors before the API call (ADC, discovery.build) also fail-soft."""
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)

        def boom(*a, **kw):
            raise RuntimeError("no metadata server")

        monkeypatch.setattr("google.auth.default", boom)

        from app.auth.group_sync import fetch_user_groups
        assert fetch_user_groups("user@x") == []

    def test_memberships_without_groupkey_are_skipped(self, monkeypatch):
        """Defensive: a malformed membership missing groupKey.id must not crash."""
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)
        service, _ = _make_service_mock(
            [
                {
                    "memberships": [
                        {"groupKey": {"id": "good@x"}},
                        {"groupKey": {}},  # missing id
                        {},  # missing groupKey
                    ]
                }
            ]
        )
        monkeypatch.setattr(
            "google.auth.default",
            lambda scopes=None: (mock.Mock(), "test-project"),
        )
        monkeypatch.setattr(
            "googleapiclient.discovery.build",
            lambda *a, **kw: service,
        )

        from app.auth.group_sync import fetch_user_groups
        assert fetch_user_groups("u@x") == ["good@x"]

    def test_email_with_quote_is_escaped(self, monkeypatch):
        """A single quote in the email must not break the CEL query."""
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)
        service, search = _make_service_mock([{"memberships": []}])
        monkeypatch.setattr(
            "google.auth.default",
            lambda scopes=None: (mock.Mock(), "test-project"),
        )
        monkeypatch.setattr(
            "googleapiclient.discovery.build",
            lambda *a, **kw: service,
        )

        from app.auth.group_sync import fetch_user_groups
        fetch_user_groups("o'reilly@x")
        assert "\\'" in search.call_args.kwargs["query"]
