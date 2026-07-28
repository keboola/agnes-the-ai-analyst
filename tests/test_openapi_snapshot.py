"""OpenAPI snapshot test — detect breaking API changes.

Compares the current app's OpenAPI schema against a committed snapshot.
Fails if any path or HTTP method has been removed (breaking change).

To update the snapshot after an intentional change:
    make update-openapi-snapshot
"""

import json
import os
from pathlib import Path

import pytest

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "openapi.json"


@pytest.fixture(scope="module")
def current_schema():
    os.environ.setdefault("TESTING", "1")
    from app.main import create_app

    app = create_app()
    return app.openapi()


def test_snapshot_exists():
    """Committed OpenAPI snapshot must exist."""
    assert SNAPSHOT_PATH.exists(), (
        "No OpenAPI snapshot found. Generate one with: make update-openapi-snapshot"
    )


def test_no_removed_paths(current_schema):
    """No API paths should be removed compared to the snapshot."""
    if not SNAPSHOT_PATH.exists():
        pytest.skip("No snapshot to compare against")

    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    current_paths = set(current_schema.get("paths", {}))
    snapshot_paths = set(snapshot.get("paths", {}))

    removed = snapshot_paths - current_paths
    assert not removed, (
        f"BREAKING: {len(removed)} API path(s) removed: {sorted(removed)}\n"
        "If intentional, run: make update-openapi-snapshot"
    )


def test_no_removed_methods(current_schema):
    """No HTTP methods should be removed from existing paths."""
    if not SNAPSHOT_PATH.exists():
        pytest.skip("No snapshot to compare against")

    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    current_paths = current_schema.get("paths", {})
    snapshot_paths = snapshot.get("paths", {})

    breaking = []
    for path in set(snapshot_paths) & set(current_paths):
        removed_methods = set(snapshot_paths[path]) - set(current_paths[path])
        # Ignore non-HTTP keys like 'parameters'
        http_methods = {"get", "post", "put", "delete", "patch", "head", "options"}
        removed_http = removed_methods & http_methods
        if removed_http:
            breaking.append(f"  {path}: {sorted(removed_http)}")

    assert not breaking, (
        f"BREAKING: HTTP methods removed from {len(breaking)} path(s):\n"
        + "\n".join(breaking)
        + "\nIf intentional, run: make update-openapi-snapshot"
    )
