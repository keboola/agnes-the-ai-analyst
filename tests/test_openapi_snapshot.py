"""OpenAPI snapshot tests — detect breaking API changes and snapshot rot.

Two layers, both comparing the current app's OpenAPI schema against the
committed snapshot:

* ``test_no_removed_paths`` / ``test_no_removed_methods`` — the breaking-change
  ratchet. These fire only on *removals*.
* ``test_snapshot_is_fresh`` — full-schema equality. The removal-only ratchet
  is one-directional, so added paths and changed operation bodies used to rot
  the snapshot silently while CI stayed green (it had drifted 468 -> 509 paths,
  with 22 further paths changed in place, before this test existed).

To update the snapshot after an intentional change:
    make update-openapi-snapshot
"""

import json
import os
from pathlib import Path

import pytest

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "openapi.json"

_REGENERATE_HINT = "Run: make update-openapi-snapshot  (then review the diff and commit it)"


@pytest.fixture(scope="module")
def current_schema():
    os.environ.setdefault("TESTING", "1")
    from app.main import create_app

    app = create_app()
    return app.openapi()


def test_snapshot_exists():
    """Committed OpenAPI snapshot must exist."""
    assert SNAPSHOT_PATH.exists(), "No OpenAPI snapshot found. Generate one with: make update-openapi-snapshot"


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


def _normalized(schema: dict) -> dict:
    """Strip the one field that legitimately varies between environments.

    ``info.version`` is ``app.version``, resolved from *installed* package
    metadata (``app/version.py`` -> ``importlib.metadata``). It therefore
    changes on every release cut, and can differ between a working copy and a
    freshly-installed CI checkout. Comparing it would turn this test into a
    merge magnet that reds every version-bump PR without ever catching a real
    API change, so it is normalized out on both sides.

    Everything else is compared verbatim: the generator sorts keys, so the
    comparison is order-independent and deterministic.
    """
    out = {k: v for k, v in schema.items() if k != "info"}
    out["info"] = {k: v for k, v in schema.get("info", {}).items() if k != "version"}
    return out


def _summarize_drift(current: dict, snapshot: dict, limit: int = 15) -> str:
    """Human-readable, bounded description of how the two schemas differ."""
    current_paths, snapshot_paths = current.get("paths", {}), snapshot.get("paths", {})
    added = sorted(set(current_paths) - set(snapshot_paths))
    removed = sorted(set(snapshot_paths) - set(current_paths))
    changed = sorted(p for p in set(current_paths) & set(snapshot_paths) if current_paths[p] != snapshot_paths[p])

    def _block(label: str, paths: list[str]) -> list[str]:
        if not paths:
            return []
        shown = [f"    {p}" for p in paths[:limit]]
        if len(paths) > limit:
            shown.append(f"    … and {len(paths) - limit} more")
        return [f"  {label} ({len(paths)}):", *shown]

    lines: list[str] = []
    lines += _block("paths in the app but missing from the snapshot", added)
    lines += _block("paths in the snapshot but gone from the app", removed)
    lines += _block("paths whose operations changed", changed)
    if not lines:
        # Same path map — the drift is elsewhere (components, top-level fields).
        differing = sorted(k for k in set(current) | set(snapshot) if current.get(k) != snapshot.get(k))
        lines.append(f"  differing top-level section(s): {differing}")
    return "\n".join(lines)


def test_snapshot_is_fresh(current_schema):
    """The committed snapshot must match the app's actual schema.

    Guards the whole document, not just the path map, so structural drift
    (a route gaining a request body, a response schema changing) is caught too.
    """
    assert SNAPSHOT_PATH.exists(), f"No OpenAPI snapshot found. {_REGENERATE_HINT}"

    snapshot = _normalized(json.loads(SNAPSHOT_PATH.read_text()))
    current = _normalized(current_schema)

    assert current == snapshot, (
        "tests/snapshots/openapi.json is stale — it no longer matches the app's "
        "OpenAPI schema.\n" + _summarize_drift(current, snapshot) + f"\n{_REGENERATE_HINT}"
    )
