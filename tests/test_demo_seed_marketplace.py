"""Tests for the baked, git-free demo marketplace seed + sync skip guard."""

from __future__ import annotations

import pytest

from src.demo_seed import LOCAL_MARKETPLACE_URL, seed_marketplace


def test_seed_marketplace_idempotent_and_local(system_db):
    seed_marketplace(system_db)
    seed_marketplace(system_db)
    rows = system_db.execute(
        "SELECT url FROM marketplace_registry WHERE url = ?", [LOCAL_MARKETPLACE_URL]
    ).fetchall()
    assert len(rows) == 1
    assert LOCAL_MARKETPLACE_URL.startswith("local:")


def test_sync_spec_skips_local_url(monkeypatch):
    """A ``local:`` URL row must short-circuit before any git invocation."""
    import src.marketplace as marketplace

    def _boom(*args, **kwargs):
        raise AssertionError("git must not be invoked for a local: marketplace")

    monkeypatch.setattr(marketplace, "_run_git", _boom)

    result = marketplace._sync_spec(
        {"id": "demo", "name": "Demo Marketplace", "url": LOCAL_MARKETPLACE_URL}
    )

    assert result["id"] == "demo"
    assert result["action"] == "local"
    # No git ran, so there is no commit SHA to report.
    assert result["commit"] is None
