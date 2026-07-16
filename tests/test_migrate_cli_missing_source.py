"""CLI behavior of ``python -m scripts.migrate_duckdb_to_pg`` when the
source ``system.duckdb`` does not exist.

Regression for the fresh-volume boot path: on a brand-new ``data`` volume
the docker-compose ``data-migrate`` one-shot found no ``system.duckdb``,
exited 2, and ``app``/``scheduler`` (gated on
``service_completed_successfully``) never started — a fresh
Postgres-backend deployment could not boot compose from scratch.

The fix is opt-in: ``--missing-source-ok`` turns the no-source case into
exit 0 ("nothing to migrate"). WITHOUT the flag the CLI keeps exiting 2 so
the one-time cutover path (infra ``pg-cutover.sh`` with ``--reset-target``)
still fails loudly on a missing or mis-mounted source instead of silently
"succeeding" with an empty copy.

None of these paths may touch Postgres: the file check runs before any
engine construction, so the tests run without DATABASE_URL set.
"""

from __future__ import annotations

import sys

import pytest

from scripts.migrate_duckdb_to_pg.__main__ import main


@pytest.fixture(autouse=True)
def _no_pg_env(monkeypatch):
    """Prove the missing-source paths never construct a PG engine."""
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _run_cli(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["migrate_duckdb_to_pg", *argv])
    return main()


def test_missing_source_exits_2_by_default(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "state" / "system.duckdb"
    rc = _run_cli(monkeypatch, "--duckdb-path", str(missing))
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_missing_source_ok_exits_0(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "state" / "system.duckdb"
    rc = _run_cli(monkeypatch, "--duckdb-path", str(missing), "--missing-source-ok")
    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to migrate" in out
    # The message must point operators at the mis-mounted-volume case —
    # exit 0 on a missing source is only safe when it is loud about it.
    assert "volume" in out


def test_missing_source_ok_via_data_dir_default(tmp_path, monkeypatch):
    """The DATA_DIR-derived default path honors the flag the same way
    (the compose service passes --duckdb-path explicitly, but operators
    running the module by hand rely on DATA_DIR)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    rc = _run_cli(monkeypatch, "--missing-source-ok")
    assert rc == 0


def test_missing_source_ok_rejects_reset_target(tmp_path, monkeypatch, capsys):
    """--reset-target asserts a one-time cutover, which requires an
    existing source; combining it with --missing-source-ok would let a
    mis-mounted volume no-op-succeed mid-cutover. argparse rejects the
    combination (SystemExit 2)."""
    with pytest.raises(SystemExit) as excinfo:
        _run_cli(
            monkeypatch,
            "--duckdb-path",
            str(tmp_path / "system.duckdb"),
            "--missing-source-ok",
            "--reset-target",
        )
    assert excinfo.value.code == 2
    assert "--reset-target" in capsys.readouterr().err
