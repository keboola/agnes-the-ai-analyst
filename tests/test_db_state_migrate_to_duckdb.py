# tests/test_db_state_migrate_to_duckdb.py
"""H7-NEW — POST /migrate with target='duckdb' / 'duckdb_quack'
returns a clean 501 instead of silently mis-routing to CLOUD.

The transition matrix (commit 965e7870) allows SIDE_CAR/CLOUD → DUCKDB,
but the endpoint's branch logic only wired 'side_car' and 'cloud' paths.
Posting target='duckdb' from a PG-backed state fell to the else-branch,
writing CLOUD_IN_PROGRESS into instance.yaml then crashing the migrator
with BackendNotYetSupportedError (uncaught → 500).

For 'duckdb_quack', validate_transition already raises
BackendNotYetSupportedError (it is in _NOT_YET_SUPPORTED_TARGETS) —
but the endpoint never caught it either, so callers got a raw 500.
Both cases now return 501.

Test strategy: call start_migration() directly (bypassing FastAPI's
Depends(require_admin) which is router-scoped, not inside the function
body). We patch app.api.db_state.read_backend_state → SIDE_CAR so
validate_transition passes for target='duckdb'. The 501 guard fires
before write_backend_state is reached, so no filesystem writes occur.
Note: read_backend_state is imported at module level into app.api.db_state,
so the patch target is 'app.api.db_state.read_backend_state'.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch


def _make_side_car_state():
    """Return (BackendState.SIDE_CAR, url) — a valid source for duckdb target."""
    from src.db_state_machine import BackendState
    return BackendState.SIDE_CAR, "postgresql+psycopg://agnes:pw@postgres:5432/agnes"


@pytest.mark.parametrize("target", ["duckdb", "duckdb_quack"])
def test_start_migration_duckdb_target_returns_501(target: str) -> None:
    """target='duckdb' and 'duckdb_quack' from SIDE_CAR must return 501.

    Calling start_migration() directly — require_admin is a router-level
    Depends, not called inside the function body, so it is skipped here.
    read_backend_state is patched in the db_state module namespace (where
    it was imported at module level) to return SIDE_CAR so validate_transition
    passes (SIDE_CAR → DUCKDB is a valid graph edge). The 501 fires before
    any filesystem I/O.
    """
    from app.api import db_state
    from fastapi import HTTPException

    with patch("app.api.db_state.read_backend_state", return_value=_make_side_car_state()):
        with pytest.raises(HTTPException) as exc:
            db_state.start_migration(
                payload=db_state.MigrateRequest(target=target, cloud_url=None)
            )

    assert exc.value.status_code == 501, (
        f"target={target!r} from SIDE_CAR must return 501 (not yet supported) "
        f"until the migrator wires reverse-to-duckdb; got {exc.value.status_code}: "
        f"{exc.value.detail}"
    )
    detail = str(exc.value.detail).lower()
    assert (
        "not yet supported" in detail
        or "not yet runtime-supported" in detail
        or "not implemented" in detail
    ), (
        f"501 detail must mention 'not yet supported'/'not yet runtime-supported'/"
        f"'not implemented'; got: {detail!r}"
    )


def test_start_migration_side_car_not_501(tmp_path, monkeypatch) -> None:
    """Regression guard: side_car target must not return 501.

    From DUCKDB (default), target='side_car' is a valid transition.
    We set POSTGRES_PASSWORD so the endpoint passes the credential check.
    State paths are pointed at tmp_path so the write doesn't pollute the
    module-level _OVERLAY_PATH and bleed into subsequent tests.
    The function will succeed or raise a non-501 exception. Either way,
    501 must not be raised.
    """
    from app.api import db_state
    from fastapi import HTTPException

    monkeypatch.setenv("POSTGRES_PASSWORD", "test-pw")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "state" / "instance.yaml")
    monkeypatch.setattr("src.db_state_machine._LOCK_PATH", tmp_path / "state" / "db-migration.lock")

    try:
        db_state.start_migration(
            payload=db_state.MigrateRequest(target="side_car", cloud_url=None)
        )
    except HTTPException as exc:
        assert exc.status_code != 501, (
            f"side_car target must NOT trigger H7-NEW 501 guard; "
            f"got 501: {exc.detail}"
        )
    except Exception:
        pass  # Non-HTTPException (e.g. OSError from MigrationLock) is fine.


def test_post_migrate_duckdb_invalid_transition_still_400(seeded_app, monkeypatch) -> None:
    """From DUCKDB (default state), target='duckdb' is invalid-transition 400.

    Confirms H7-NEW's 501 guard doesn't swallow the self-loop rejection.
    The 400 comes from validate_transition (duckdb → duckdb is not in
    _ALLOWED_TRANSITIONS), which fires BEFORE the 501 guard because the
    transition is outright invalid — not "valid but not yet implemented".
    """
    data_dir = seeded_app["env"]["data_dir"]
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        data_dir / "state" / "instance.yaml",
    )
    monkeypatch.setattr(
        "src.db_state_machine._LOCK_PATH",
        data_dir / "state" / "db-migration.lock",
    )
    # No overlay file → state defaults to DUCKDB.

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        "/api/admin/db/migrate",
        json={"target": "duckdb"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400, (
        f"duckdb → duckdb self-loop must remain 400 (invalid transition), "
        f"not {r.status_code}: {r.text}"
    )
    assert "not allowed" in r.json()["detail"].lower()
