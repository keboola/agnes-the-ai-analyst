"""Tests for the Table Registry module."""

import json
from pathlib import Path

import pytest
import yaml

from src.table_registry import ConflictError, TableRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry_path(tmp_path):
    """Return a temp path for the registry JSON."""
    return tmp_path / "table_registry.json"


@pytest.fixture
def registry(registry_path):
    """Create an empty registry."""
    return TableRegistry(registry_path)


@pytest.fixture
def sample_table():
    """Minimal valid table definition."""
    return {
        "id": "in.c-crm.company",
        "name": "company",
        "description": "Customer master data",
        "primary_key": "id",
        "sync_strategy": "full_refresh",
    }


@pytest.fixture
def sample_table_incremental():
    """Incremental table definition."""
    return {
        "id": "in.c-crm.events",
        "name": "events",
        "description": "User events",
        "primary_key": "event_id",
        "sync_strategy": "incremental",
        "incremental_window_days": 14,
        "partition_by": "created_at",
        "partition_granularity": "month",
    }


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------

class TestRegistryCRUD:

    def test_empty_registry(self, registry):
        assert registry.list_tables() == []
        assert registry.version == 0

    def test_register_table(self, registry, sample_table):
        registry.register_table(sample_table, registered_by="admin@test.com")
        tables = registry.list_tables()
        assert len(tables) == 1
        assert tables[0]["id"] == "in.c-crm.company"
        assert tables[0]["registered_by"] == "admin@test.com"
        assert registry.version == 1

    def test_register_duplicate_raises(self, registry, sample_table):
        registry.register_table(sample_table, registered_by="admin@test.com")
        with pytest.raises(ValueError, match="already registered"):
            registry.register_table(sample_table, registered_by="admin@test.com")

    def test_get_table(self, registry, sample_table):
        registry.register_table(sample_table, registered_by="admin@test.com")
        t = registry.get_table("in.c-crm.company")
        assert t is not None
        assert t["name"] == "company"

    def test_get_table_not_found(self, registry):
        assert registry.get_table("nonexistent") is None

    def test_is_registered(self, registry, sample_table):
        assert not registry.is_registered("in.c-crm.company")
        registry.register_table(sample_table, registered_by="admin@test.com")
        assert registry.is_registered("in.c-crm.company")

    def test_unregister_table(self, registry, sample_table):
        registry.register_table(sample_table, registered_by="admin@test.com")
        registry.unregister_table("in.c-crm.company", unregistered_by="admin@test.com")
        assert not registry.is_registered("in.c-crm.company")
        assert registry.list_tables() == []

    def test_unregister_nonexistent_raises(self, registry):
        with pytest.raises(ValueError, match="not registered"):
            registry.unregister_table("nonexistent")

    def test_update_table(self, registry, sample_table):
        registry.register_table(sample_table, registered_by="admin@test.com")
        registry.update_table(
            "in.c-crm.company",
            {"description": "Updated description", "sync_strategy": "incremental"},
            updated_by="admin@test.com",
        )
        t = registry.get_table("in.c-crm.company")
        assert t["description"] == "Updated description"
        assert t["sync_strategy"] == "incremental"

    def test_update_nonexistent_raises(self, registry):
        with pytest.raises(ValueError, match="not registered"):
            registry.update_table("nonexistent", {"description": "x"})


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:

    def test_missing_id_raises(self, registry):
        with pytest.raises(ValueError, match="must include 'id'"):
            registry.register_table(
                {"name": "x", "sync_strategy": "full_refresh", "primary_key": "id"},
                registered_by="admin@test.com",
            )

    def test_missing_name_raises(self, registry):
        with pytest.raises(ValueError, match="must include 'name'"):
            registry.register_table(
                {"id": "x.y.z", "sync_strategy": "full_refresh", "primary_key": "id"},
                registered_by="admin@test.com",
            )

    def test_invalid_sync_strategy_raises(self, registry):
        with pytest.raises(ValueError, match="Invalid sync_strategy"):
            registry.register_table(
                {
                    "id": "x.y.z",
                    "name": "z",
                    "sync_strategy": "magic",
                    "primary_key": "id",
                },
                registered_by="admin@test.com",
            )


# ---------------------------------------------------------------------------
# Optimistic locking
# ---------------------------------------------------------------------------

class TestOptimisticLocking:

    def test_register_with_wrong_version_raises(self, registry, sample_table):
        with pytest.raises(ConflictError, match="Version conflict"):
            registry.register_table(
                sample_table, registered_by="admin@test.com", expected_version=99
            )

    def test_register_with_correct_version(self, registry, sample_table):
        registry.register_table(
            sample_table, registered_by="admin@test.com", expected_version=0
        )
        assert registry.version == 1

    def test_unregister_with_wrong_version_raises(self, registry, sample_table):
        registry.register_table(sample_table, registered_by="admin@test.com")
        with pytest.raises(ConflictError):
            registry.unregister_table(
                "in.c-crm.company", expected_version=0
            )

    def test_update_with_wrong_version_raises(self, registry, sample_table):
        registry.register_table(sample_table, registered_by="admin@test.com")
        with pytest.raises(ConflictError):
            registry.update_table(
                "in.c-crm.company",
                {"description": "x"},
                expected_version=0,
            )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_save_and_reload(self, registry_path, sample_table):
        reg1 = TableRegistry(registry_path)
        reg1.register_table(sample_table, registered_by="admin@test.com")

        # Reload from disk
        reg2 = TableRegistry(registry_path)
        assert len(reg2.list_tables()) == 1
        assert reg2.get_table("in.c-crm.company")["name"] == "company"
        assert reg2.version == 1

    def test_json_format(self, registry_path, sample_table):
        reg = TableRegistry(registry_path)
        reg.register_table(sample_table, registered_by="admin@test.com")

        with open(registry_path) as f:
            data = json.load(f)

        assert "_metadata" in data
        assert "tables" in data
        assert data["_metadata"]["version"] == 1
        assert len(data["tables"]) == 1


# ---------------------------------------------------------------------------
# Folder mapping
# ---------------------------------------------------------------------------

class TestFolderMapping:

    def test_set_and_get(self, registry):
        registry.set_folder_mapping("in.c-crm", "crm")
        assert registry.get_folder_mapping() == {"in.c-crm": "crm"}

    def test_persists(self, registry_path):
        reg1 = TableRegistry(registry_path)
        reg1.set_folder_mapping("in.c-crm", "crm")

        reg2 = TableRegistry(registry_path)
        assert reg2.get_folder_mapping() == {"in.c-crm": "crm"}


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

class TestGeneration:

    def test_generate_data_description_md(self, registry, sample_table, tmp_path):
        registry.register_table(sample_table, registered_by="admin@test.com")
        registry.set_folder_mapping("in.c-crm", "crm")

        output = tmp_path / "data_description.md"
        registry.generate_data_description_md(output)

        content = output.read_text()

        # Check header
        assert "AUTO-GENERATED" in content
        assert "checksum: sha256:" in content

        # Check YAML block is parseable
        yaml_match = __import__("re").search(r"```yaml\n(.*?)```", content, __import__("re").DOTALL)
        assert yaml_match
        yaml_data = yaml.safe_load(yaml_match.group(1))
        assert len(yaml_data["tables"]) == 1
        assert yaml_data["tables"][0]["id"] == "in.c-crm.company"
        assert yaml_data["folder_mapping"] == {"in.c-crm": "crm"}

    def test_generate_includes_incremental_fields(
        self, registry, sample_table_incremental, tmp_path
    ):
        registry.register_table(sample_table_incremental, registered_by="admin@test.com")

        output = tmp_path / "data_description.md"
        registry.generate_data_description_md(output)

        content = output.read_text()
        yaml_match = __import__("re").search(r"```yaml\n(.*?)```", content, __import__("re").DOTALL)
        yaml_data = yaml.safe_load(yaml_match.group(1))
        table = yaml_data["tables"][0]
        assert table["partition_by"] == "created_at"
        assert table["partition_granularity"] == "month"
        assert table["incremental_window_days"] == 14


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

class TestMigration:

    def test_import_from_data_description(self, tmp_path):
        # Create a fake data_description.md
        md_content = """# Data Description

```yaml
folder_mapping:
  in.c-crm: crm

tables:
  - id: in.c-crm.company
    name: company
    description: Companies
    primary_key: id
    sync_strategy: full_refresh

  - id: in.c-crm.contact
    name: contact
    description: Contacts
    primary_key: id
    sync_strategy: incremental
    incremental_window_days: 7
```
"""
        md_path = tmp_path / "data_description.md"
        md_path.write_text(md_content)

        registry_path = tmp_path / "table_registry.json"
        registry = TableRegistry.import_from_data_description(md_path, registry_path)

        assert len(registry.list_tables()) == 2
        assert registry.is_registered("in.c-crm.company")
        assert registry.is_registered("in.c-crm.contact")
        assert registry.get_folder_mapping() == {"in.c-crm": "crm"}

        # Check migrated_from marker
        with open(registry_path) as f:
            data = json.load(f)
        assert "migrated_from" in data["_metadata"]

    def test_import_no_yaml_raises(self, tmp_path):
        md_path = tmp_path / "data_description.md"
        md_path.write_text("# Empty file\nNo YAML here.")

        with pytest.raises(ValueError, match="No YAML blocks"):
            TableRegistry.import_from_data_description(
                md_path, tmp_path / "registry.json"
            )

    def test_import_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            TableRegistry.import_from_data_description(
                tmp_path / "nonexistent.md", tmp_path / "registry.json"
            )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class TestAuditLog:

    def test_register_writes_audit(self, registry, sample_table):
        registry.register_table(sample_table, registered_by="admin@test.com")

        audit_path = registry.registry_path.parent / "registry_audit.log"
        assert audit_path.exists()

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["action"] == "register"
        assert entry["table_id"] == "in.c-crm.company"

    def test_unregister_writes_audit(self, registry, sample_table):
        registry.register_table(sample_table, registered_by="admin@test.com")
        registry.unregister_table("in.c-crm.company", unregistered_by="admin@test.com")

        audit_path = registry.registry_path.parent / "registry_audit.log"
        lines = audit_path.read_text().strip().split("\n")
        last_entry = json.loads(lines[-1])
        assert last_entry["action"] == "unregister"
