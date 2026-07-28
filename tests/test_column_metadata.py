"""Tests for ColumnMetadataRepository."""

import json
import pytest


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db
    conn = get_system_db()
    yield conn
    conn.close()


@pytest.fixture
def repo(db_conn):
    from src.repositories.column_metadata import ColumnMetadataRepository
    return ColumnMetadataRepository(db_conn)


class TestColumnMetadataCreate:
    def test_save_single_column(self, repo):
        result = repo.save("orders", "id", basetype="STRING", description="Order ID")
        assert result["table_id"] == "orders"
        assert result["column_name"] == "id"
        assert result["basetype"] == "STRING"
        assert result["description"] == "Order ID"
        assert result["confidence"] == "manual"
        assert result["source"] == "manual"

    def test_upsert_overwrites(self, repo):
        repo.save("orders", "id", basetype="STRING", description="Old")
        result = repo.save("orders", "id", basetype="INTEGER", description="New", confidence="high")
        assert result["basetype"] == "INTEGER"
        assert result["description"] == "New"
        assert result["confidence"] == "high"
        # Should still be only one row
        rows = repo.list_for_table("orders")
        assert len(rows) == 1


class TestColumnMetadataRead:
    def test_list_for_table_filters_by_table(self, repo):
        repo.save("orders", "id", basetype="STRING")
        repo.save("orders", "total", basetype="NUMERIC")
        repo.save("orders", "status", basetype="STRING")
        repo.save("customers", "email", basetype="STRING")

        orders_cols = repo.list_for_table("orders")
        assert len(orders_cols) == 3
        assert all(c["table_id"] == "orders" for c in orders_cols)

        customer_cols = repo.list_for_table("customers")
        assert len(customer_cols) == 1
        assert customer_cols[0]["column_name"] == "email"

    def test_list_for_table_ordered_by_column_name(self, repo):
        repo.save("orders", "total", basetype="NUMERIC")
        repo.save("orders", "id", basetype="STRING")
        repo.save("orders", "status", basetype="STRING")

        cols = repo.list_for_table("orders")
        names = [c["column_name"] for c in cols]
        assert names == sorted(names)

    def test_get_missing_returns_none(self, repo):
        result = repo.get("orders", "nonexistent")
        assert result is None


class TestColumnMetadataDelete:
    def test_delete_column(self, repo):
        repo.save("orders", "id", basetype="STRING")
        deleted = repo.delete("orders", "id")
        assert deleted is True
        assert repo.get("orders", "id") is None

    def test_delete_missing_returns_false(self, repo):
        result = repo.delete("orders", "does_not_exist")
        assert result is False


class TestColumnMetadataProposal:
    def test_import_proposal_count(self, repo, tmp_path):
        proposal = {
            "tables": {
                "orders": {
                    "columns": {
                        "id": {"basetype": "STRING", "description": "Order ID", "confidence": "high"},
                        "total": {"basetype": "NUMERIC", "description": "Total amount"},
                    }
                },
                "customers": {
                    "columns": {
                        "email": {"basetype": "STRING", "description": "Customer email", "confidence": "medium"},
                    }
                },
            }
        }
        path = tmp_path / "proposal.json"
        path.write_text(json.dumps(proposal))

        count = repo.import_proposal(str(path))
        assert count == 3

    def test_import_proposal_data(self, repo, tmp_path):
        proposal = {
            "tables": {
                "orders": {
                    "columns": {
                        "id": {"basetype": "STRING", "description": "Order ID", "confidence": "high"},
                    }
                }
            }
        }
        path = tmp_path / "proposal.json"
        path.write_text(json.dumps(proposal))

        repo.import_proposal(str(path))

        result = repo.get("orders", "id")
        assert result is not None
        assert result["basetype"] == "STRING"
        assert result["description"] == "Order ID"
        assert result["confidence"] == "high"

    def test_import_sets_source_ai_enrichment(self, repo, tmp_path):
        proposal = {
            "tables": {
                "orders": {
                    "columns": {
                        "id": {"basetype": "STRING"},
                    }
                }
            }
        }
        path = tmp_path / "proposal.json"
        path.write_text(json.dumps(proposal))

        repo.import_proposal(str(path))

        result = repo.get("orders", "id")
        assert result["source"] == "ai_enrichment"
