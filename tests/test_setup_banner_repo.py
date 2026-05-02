"""Unit tests for SetupBannerRepository."""

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.setup_banner import SetupBannerRepository


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db_path))
    _ensure_schema(c)
    yield c
    c.close()


def test_get_returns_none_on_fresh_install(conn):
    repo = SetupBannerRepository(conn)
    row = repo.get()
    assert row is not None
    assert row["content"] is None  # no banner by default


def test_set_stores_content(conn):
    repo = SetupBannerRepository(conn)
    repo.set("<p>VPN required</p>", updated_by="admin@example.com")
    row = repo.get()
    assert row["content"] == "<p>VPN required</p>"
    assert row["updated_by"] == "admin@example.com"
    assert row["updated_at"] is not None


def test_reset_clears_content(conn):
    repo = SetupBannerRepository(conn)
    repo.set("<p>Note</p>", updated_by="admin@example.com")
    repo.reset(updated_by="admin@example.com")
    row = repo.get()
    assert row["content"] is None


def test_set_overwrites_existing(conn):
    repo = SetupBannerRepository(conn)
    repo.set("first", updated_by="a@example.com")
    repo.set("second", updated_by="b@example.com")
    row = repo.get()
    assert row["content"] == "second"
    assert row["updated_by"] == "b@example.com"
