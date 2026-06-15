"""Guard tests for docs/runbooks/wal-recovery.md.

Every code path, log string, file name, and Python symbol cited in the
runbook is verified to exist in the real codebase here.  A failing test
means the runbook references something that has been renamed or removed.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _runbook_text() -> str:
    return (REPO_ROOT / "docs" / "runbooks" / "wal-recovery.md").read_text()


def _db_source() -> str:
    return (REPO_ROOT / "src" / "db.py").read_text()


# ---------------------------------------------------------------------------
# File-existence guards
# ---------------------------------------------------------------------------

def test_runbook_file_exists():
    assert (REPO_ROOT / "docs" / "runbooks" / "wal-recovery.md").is_file()


def test_db_py_exists():
    assert (REPO_ROOT / "src" / "db.py").is_file()


def test_duckdb_conn_py_exists():
    assert (REPO_ROOT / "src" / "duckdb_conn.py").is_file()


def test_wal_recovery_test_file_exists():
    assert (REPO_ROOT / "tests" / "test_db_wal_recovery.py").is_file()


def test_state_dir_doc_exists():
    assert (REPO_ROOT / "docs" / "state-dir.md").is_file()


# ---------------------------------------------------------------------------
# Runbook cites real Python functions
# ---------------------------------------------------------------------------

def _defined_names_in_db_py() -> set[str]:
    """Return the set of all top-level function / class names defined in db.py."""
    tree = ast.parse(_db_source())
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def test_function_try_open_system_db_exists():
    assert "_try_open_system_db" in _defined_names_in_db_py()


def test_function_salvage_discard_wal_exists():
    assert "_salvage_discard_wal" in _defined_names_in_db_py()


def test_function_move_to_broken_exists():
    assert "_move_to_broken" in _defined_names_in_db_py()


def test_function_peek_schema_version_exists():
    assert "_peek_schema_version" in _defined_names_in_db_py()


def test_function_ensure_schema_exists():
    assert "_ensure_schema" in _defined_names_in_db_py()


def test_function_get_schema_version_exists():
    assert "get_schema_version" in _defined_names_in_db_py()


# ---------------------------------------------------------------------------
# SCHEMA_VERSION constant exists and is an integer
# ---------------------------------------------------------------------------

def test_schema_version_is_int():
    import importlib.util
    # Parse without executing by searching the source text
    m = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", _db_source(), re.MULTILINE)
    assert m is not None, "SCHEMA_VERSION constant not found in src/db.py"
    assert int(m.group(1)) > 0


def test_runbook_schema_version_matches_source():
    """The SCHEMA_VERSION value the runbook cites must match src/db.py."""
    src_m = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", _db_source(), re.MULTILINE)
    assert src_m, "SCHEMA_VERSION not found in src/db.py"
    src_ver = int(src_m.group(1))

    rb = _runbook_text()
    # Runbook mentions SCHEMA_VERSION=76 (or whatever the current value is)
    # in the detection section. Check the numeric value appears.
    assert str(src_ver) in rb, (
        f"Runbook does not mention SCHEMA_VERSION={src_ver}; "
        "update docs/runbooks/wal-recovery.md to match src/db.py"
    )


# ---------------------------------------------------------------------------
# Log strings cited in the runbook exist in src/db.py
# ---------------------------------------------------------------------------

def test_log_failure_while_replaying_wal_in_db_py():
    assert "Failure while replaying WAL" in _db_source()


def test_log_replay_alter_in_db_py():
    assert "ReplayAlter" in _db_source()


def test_log_get_default_database_in_db_py():
    assert "GetDefaultDatabase" in _db_source()


def test_log_auto_restoring_from_pre_migrate_in_db_py():
    assert "auto-restoring from pre-migrate" in _db_source()


def test_log_refusing_auto_recovery_in_db_py():
    assert "REFUSING auto-recovery" in _db_source()


def test_log_discarded_wal_message_in_db_py():
    assert "discarded the unreplayable WAL" in _db_source()


def test_log_wal_salvage_could_not_move_in_db_py():
    assert "WAL salvage: could not move WAL aside" in _db_source()


# ---------------------------------------------------------------------------
# File naming patterns cited in the runbook exist in source
# ---------------------------------------------------------------------------

def test_broken_file_pattern_in_db_py():
    """src/db.py must construct the .broken.<ts> suffix."""
    assert ".broken." in _db_source()


def test_wal_discarded_pattern_in_db_py():
    """src/db.py must construct the .wal.discarded.<ts> suffix."""
    assert ".wal.discarded." in _db_source()


def test_pre_migrate_snapshot_name_in_db_py():
    """src/db.py must reference system.duckdb.pre-migrate."""
    assert "system.duckdb.pre-migrate" in _db_source()


# ---------------------------------------------------------------------------
# schema_version table shape cited in the runbook
# ---------------------------------------------------------------------------

def test_schema_version_table_has_version_column():
    assert "version INTEGER" in _db_source()


def test_schema_version_table_has_applied_at_column():
    assert "applied_at TIMESTAMP" in _db_source()


# ---------------------------------------------------------------------------
# Cross-reference completeness: every function name the runbook lists in
# its cross-reference table must appear in db.py.
# ---------------------------------------------------------------------------

RUNBOOK_CROSS_REF_FUNCTIONS = [
    "_try_open_system_db",
    "_salvage_discard_wal",
    "_move_to_broken",
    "_peek_schema_version",
    "_ensure_schema",
    "SCHEMA_VERSION",
    "get_schema_version",
]


def test_runbook_cross_ref_functions_exist_in_source():
    src = _db_source()
    missing = [f for f in RUNBOOK_CROSS_REF_FUNCTIONS if f not in src]
    assert not missing, f"Functions cited in runbook cross-ref are missing from src/db.py: {missing}"
