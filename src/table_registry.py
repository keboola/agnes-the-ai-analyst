"""
Table Registry - Central source of truth for registered tables.

Manages table registrations in a JSON file. Generates data_description.md
as a read-only output for downstream consumers (config.py, profiler.py, webapp).

Supports:
- CRUD operations on registered tables
- Folder mapping (bucket -> folder name)
- Atomic persistence (tempfile + os.replace)
- Optimistic locking (version field)
- Audit logging
- One-time migration from existing data_description.md
- Generation of data_description.md with checksum header
"""

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# Default registry location
_DEFAULT_REGISTRY_DIR = Path(
    os.environ.get("REGISTRY_DIR", "/data/src_data/metadata")
)
_REGISTRY_FILENAME = "table_registry.json"


def _now_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically using tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.chmod(tmp_path, 0o660)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _audit_log(registry_path: Path, action: str, details: dict) -> None:
    """Append entry to registry audit log."""
    audit_path = registry_path.parent / "registry_audit.log"
    try:
        entry = {
            "timestamp": _now_iso(),
            "action": action,
            **details,
        }
        with open(audit_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.warning(f"Could not write audit log: {e}")


class TableRegistry:
    """Manages table registrations. Source of truth for what gets synced."""

    def __init__(self, registry_path: Path):
        self.registry_path = registry_path
        self._data = self._load()

    @classmethod
    def default(cls) -> "TableRegistry":
        """Create registry at the default location."""
        return cls(_DEFAULT_REGISTRY_DIR / _REGISTRY_FILENAME)

    # ── Persistence ──────────────────────────────────────────────────

    def _load(self) -> dict:
        """Load registry from disk. Returns empty structure if not found."""
        if self.registry_path.exists():
            try:
                with open(self.registry_path) as f:
                    data = json.load(f)
                logger.info(
                    f"Registry loaded: {len(data.get('tables', []))} tables"
                )
                return data
            except Exception as e:
                logger.error(f"Error loading registry: {e}")
        return self._empty_registry()

    def _save(self) -> None:
        """Save registry to disk atomically."""
        self._data["_metadata"]["updated_at"] = _now_iso()
        self._data["_metadata"]["version"] = self.version + 1
        _atomic_write_json(self.registry_path, self._data)
        logger.debug("Registry saved (version %d)", self.version)

    @staticmethod
    def _empty_registry() -> dict:
        now = _now_iso()
        return {
            "_metadata": {
                "version": 0,
                "created_at": now,
                "updated_at": now,
            },
            "folder_mapping": {},
            "tables": [],
        }

    # ── Properties ───────────────────────────────────────────────────

    @property
    def version(self) -> int:
        return self._data.get("_metadata", {}).get("version", 0)

    # ── Core CRUD ────────────────────────────────────────────────────

    def list_tables(self) -> list[dict]:
        """Return all registered tables."""
        return list(self._data.get("tables", []))

    def get_table(self, table_id: str) -> Optional[dict]:
        """Get a single table by ID."""
        for t in self._data.get("tables", []):
            if t["id"] == table_id:
                return dict(t)
        return None

    def is_registered(self, table_id: str) -> bool:
        return any(t["id"] == table_id for t in self._data.get("tables", []))

    def register_table(
        self,
        table_def: dict,
        registered_by: str,
        expected_version: Optional[int] = None,
    ) -> None:
        """Register a new table.

        Args:
            table_def: Table definition dict (must contain id, name, sync_strategy, primary_key).
            registered_by: Email of the admin who registered the table.
            expected_version: If provided, reject if registry version doesn't match (optimistic lock).

        Raises:
            ValueError: If table already registered or validation fails.
            ConflictError: If expected_version doesn't match.
        """
        if expected_version is not None and expected_version != self.version:
            raise ConflictError(
                f"Version conflict: expected {expected_version}, current {self.version}"
            )

        table_id = table_def.get("id", "")
        if not table_id:
            raise ValueError("Table definition must include 'id'")

        if self.is_registered(table_id):
            raise ValueError(f"Table '{table_id}' is already registered")

        # Validate required fields
        for field in ("name", "sync_strategy", "primary_key"):
            if not table_def.get(field):
                raise ValueError(f"Table definition must include '{field}'")

        # Validate sync_strategy
        valid_strategies = ("full_refresh", "incremental", "partitioned")
        if table_def["sync_strategy"] not in valid_strategies:
            raise ValueError(
                f"Invalid sync_strategy '{table_def['sync_strategy']}'. "
                f"Allowed: {', '.join(valid_strategies)}"
            )

        # Build full record
        record = {
            "id": table_id,
            "name": table_def["name"],
            "description": table_def.get("description", ""),
            "primary_key": table_def["primary_key"],
            "sync_strategy": table_def["sync_strategy"],
            "incremental_window_days": table_def.get("incremental_window_days"),
            "partition_by": table_def.get("partition_by"),
            "partition_granularity": table_def.get("partition_granularity"),
            "foreign_keys": table_def.get("foreign_keys", []),
            "where_filters": table_def.get("where_filters", []),
            "folder": table_def.get("folder"),
            "dataset": table_def.get("dataset"),
            "initial_load_chunk_days": table_def.get("initial_load_chunk_days", 30),
            "registered_at": _now_iso(),
            "registered_by": registered_by,
            "source_metadata": table_def.get("source_metadata", {}),
        }

        self._data["tables"].append(record)
        self._save()

        _audit_log(self.registry_path, "register", {
            "table_id": table_id,
            "by": registered_by,
        })

    def unregister_table(
        self,
        table_id: str,
        unregistered_by: str = "",
        expected_version: Optional[int] = None,
    ) -> None:
        """Remove a table from the registry.

        Raises:
            ValueError: If table not found.
            ConflictError: If expected_version doesn't match.
        """
        if expected_version is not None and expected_version != self.version:
            raise ConflictError(
                f"Version conflict: expected {expected_version}, current {self.version}"
            )

        tables = self._data.get("tables", [])
        new_tables = [t for t in tables if t["id"] != table_id]

        if len(new_tables) == len(tables):
            raise ValueError(f"Table '{table_id}' is not registered")

        self._data["tables"] = new_tables
        self._save()

        _audit_log(self.registry_path, "unregister", {
            "table_id": table_id,
            "by": unregistered_by,
        })

    def update_table(
        self,
        table_id: str,
        updates: dict,
        updated_by: str = "",
        expected_version: Optional[int] = None,
    ) -> None:
        """Update table configuration.

        Raises:
            ValueError: If table not found.
            ConflictError: If expected_version doesn't match.
        """
        if expected_version is not None and expected_version != self.version:
            raise ConflictError(
                f"Version conflict: expected {expected_version}, current {self.version}"
            )

        # Fields that can be updated
        allowed_fields = {
            "description", "primary_key", "sync_strategy",
            "incremental_window_days", "partition_by", "partition_granularity",
            "foreign_keys", "where_filters", "folder", "dataset",
            "initial_load_chunk_days",
        }

        for t in self._data.get("tables", []):
            if t["id"] == table_id:
                for key, value in updates.items():
                    if key in allowed_fields:
                        t[key] = value
                self._save()
                _audit_log(self.registry_path, "update", {
                    "table_id": table_id,
                    "fields": list(updates.keys()),
                    "by": updated_by,
                })
                return

        raise ValueError(f"Table '{table_id}' is not registered")

    # ── Folder mapping ───────────────────────────────────────────────

    def get_folder_mapping(self) -> dict[str, str]:
        return dict(self._data.get("folder_mapping", {}))

    def set_folder_mapping(self, bucket_id: str, folder: str) -> None:
        self._data.setdefault("folder_mapping", {})[bucket_id] = folder
        self._save()

    # ── Generation ───────────────────────────────────────────────────

    def generate_data_description_md(self, output_path: Path) -> None:
        """Regenerate data_description.md from registry.

        The generated file is read-only and includes a checksum header.
        Existing readers (config.py, profiler.py) consume this without changes.
        """
        tables = self.list_tables()
        folder_mapping = self.get_folder_mapping()

        # Build YAML structure matching existing data_description.md format
        yaml_data: dict[str, Any] = {}

        if folder_mapping:
            yaml_data["folder_mapping"] = folder_mapping

        yaml_tables = []
        for t in tables:
            entry: dict[str, Any] = {
                "id": t["id"],
                "name": t["name"],
                "description": t.get("description", ""),
                "primary_key": t["primary_key"],
                "sync_strategy": t["sync_strategy"],
            }

            # Optional fields -- only include if set
            if t.get("incremental_window_days"):
                entry["incremental_window_days"] = t["incremental_window_days"]
            if t.get("partition_by"):
                entry["partition_by"] = t["partition_by"]
            if t.get("partition_granularity"):
                entry["partition_granularity"] = t["partition_granularity"]
            if t.get("max_history_days"):
                entry["max_history_days"] = t["max_history_days"]
            if t.get("initial_load_chunk_days") and t["initial_load_chunk_days"] != 30:
                entry["initial_load_chunk_days"] = t["initial_load_chunk_days"]
            if t.get("foreign_keys"):
                entry["foreign_keys"] = t["foreign_keys"]
            if t.get("where_filters"):
                entry["where_filters"] = t["where_filters"]
            if t.get("folder"):
                entry["folder"] = t["folder"]
            if t.get("dataset"):
                entry["dataset"] = t["dataset"]

            yaml_tables.append(entry)

        yaml_data["tables"] = yaml_tables

        yaml_str = yaml.dump(
            yaml_data, default_flow_style=False, sort_keys=False, allow_unicode=True
        )

        # Compute checksum
        checksum = hashlib.sha256(yaml_str.encode()).hexdigest()[:16]

        # Build markdown
        lines = [
            f"<!-- AUTO-GENERATED from table_registry.json -- do not edit manually -->",
            f"<!-- Use the admin UI at /admin/tables to manage table registrations -->",
            f"<!-- checksum: sha256:{checksum} -->",
            "",
            "# Data Description",
            "",
            f"Generated at {_now_iso()} from table registry "
            f"(version {self.version}, {len(yaml_tables)} tables).",
            "",
            "```yaml",
            yaml_str.rstrip(),
            "```",
            "",
        ]

        content = "\n".join(lines)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)
        logger.info(
            f"Generated data_description.md: {len(yaml_tables)} tables "
            f"(checksum: {checksum})"
        )

    # ── Migration ────────────────────────────────────────────────────

    @classmethod
    def import_from_data_description(
        cls,
        md_path: Path,
        registry_path: Path,
        registered_by: str = "migration",
    ) -> "TableRegistry":
        """One-time migration: parse existing data_description.md into registry.

        Creates a new registry JSON from the existing markdown YAML blocks.
        """
        if not md_path.exists():
            raise FileNotFoundError(f"data_description.md not found: {md_path}")

        content = md_path.read_text()

        # Extract YAML blocks
        yaml_matches = re.findall(r"```yaml\n(.*?)```", content, re.DOTALL)
        if not yaml_matches:
            raise ValueError("No YAML blocks found in data_description.md")

        all_tables: list[dict] = []
        folder_mapping: dict[str, str] = {}

        for yaml_block in yaml_matches:
            data = yaml.safe_load(yaml_block)
            if data:
                if "tables" in data:
                    all_tables.extend(data["tables"])
                if "folder_mapping" in data:
                    folder_mapping.update(data["folder_mapping"])

        if not all_tables:
            raise ValueError("No tables found in YAML blocks")

        # Build registry
        registry = cls(registry_path)
        registry._data = cls._empty_registry()
        registry._data["folder_mapping"] = folder_mapping
        registry._data["_metadata"]["migrated_from"] = str(md_path)

        now = _now_iso()
        for table_data in all_tables:
            record = {
                "id": table_data.get("id", ""),
                "name": table_data.get("name", ""),
                "description": table_data.get("description", ""),
                "primary_key": table_data.get("primary_key", ""),
                "sync_strategy": table_data.get("sync_strategy", "full_refresh"),
                "incremental_window_days": table_data.get("incremental_window_days"),
                "partition_by": table_data.get("partition_by"),
                "partition_granularity": table_data.get("partition_granularity"),
                "foreign_keys": table_data.get("foreign_keys", []),
                "where_filters": table_data.get("where_filters", []),
                "folder": table_data.get("folder"),
                "dataset": table_data.get("dataset"),
                "initial_load_chunk_days": table_data.get("initial_load_chunk_days", 30),
                "max_history_days": table_data.get("max_history_days"),
                "registered_at": now,
                "registered_by": registered_by,
                "source_metadata": {},
            }
            registry._data["tables"].append(record)

        registry._save()

        _audit_log(registry_path, "migrate", {
            "source": str(md_path),
            "tables_imported": len(all_tables),
            "by": registered_by,
        })

        logger.info(
            f"Migrated {len(all_tables)} tables from {md_path} to registry"
        )
        return registry


class ConflictError(Exception):
    """Raised when optimistic locking version doesn't match."""
    pass
