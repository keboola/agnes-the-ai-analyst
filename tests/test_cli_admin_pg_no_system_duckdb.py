"""Regression (PR #878 review — architecture: 1 BROKEN).

The `agnes admin metrics {export,validate}` and `agnes admin break-glass-grant-admin`
/ `metadata-apply` CLI commands each opened a *vestigial* system-DuckDB
connection (`conn = get_system_db()`) that was never used — the real work routes
through the repository factory (`metric_repo()`, `users_repo()`, …). Once
`get_system_db()` hard-raises under `use_pg()`, that vestigial call turned these
commands into a hard crash on a Postgres instance (a regression from
"silently wrong" to "always fails").

The fix gates the call behind `not use_pg()`. These tests pin it: with `use_pg()`
true and `get_system_db()` wired to raise, the commands must complete without
ever touching the system DuckDB.
"""

import cli.commands.admin as admin_mod
import cli.commands.admin_metrics as metrics_mod
import src.db as db_mod
import src.repositories as repos_mod


def _boom(*_a, **_k):
    raise RuntimeError("system DuckDB must not be opened on a Postgres instance")


class _StubMetricRepo:
    def export_to_yaml(self, _output_dir):
        return 0

    def list(self):
        return []


class _StubRegistryRepo:
    def list_all(self):
        return []


def test_metrics_export_and_validate_never_open_system_duckdb_on_pg(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "get_system_db", _boom)
    monkeypatch.setattr(metrics_mod, "use_pg", lambda: True)
    monkeypatch.setattr(repos_mod, "use_pg", lambda: True)
    monkeypatch.setattr(metrics_mod, "metric_repo", lambda: _StubMetricRepo())
    monkeypatch.setattr(metrics_mod, "table_registry_repo", lambda: _StubRegistryRepo())

    # Would raise RuntimeError before the fix (vestigial get_system_db call).
    metrics_mod.export_metrics(output_dir=str(tmp_path))
    metrics_mod.validate_metrics()


def test_break_glass_grant_admin_never_opens_system_duckdb_on_pg(monkeypatch):
    monkeypatch.setattr(db_mod, "get_system_db", _boom)
    monkeypatch.setattr(repos_mod, "use_pg", lambda: True)

    class _Users:
        def get_by_email_ci(self, _email):
            return {"id": "u1"}

    class _Groups:
        def get_by_name(self, _name):
            return {"id": "g1"}

    class _Members:
        def has_membership(self, _uid, _gid):
            return True  # already a member → clean early return

    monkeypatch.setattr(admin_mod, "users_repo", lambda: _Users())
    monkeypatch.setattr(admin_mod, "user_groups_repo", lambda: _Groups())
    monkeypatch.setattr(admin_mod, "user_group_members_repo", lambda: _Members())

    # Would raise RuntimeError before the fix.
    admin_mod.break_glass_grant_admin(email="ops@example.com", yes=True)


def test_break_glass_grant_admin_tolerates_a_padded_address(monkeypatch):
    """``get_by_email_ci`` folds case in SQL but does NOT trim, while the create
    below normalizes (strip + lower). A padded argument therefore missed the
    existing row and then hit ``UNIQUE(email)`` on insert — the last-resort
    admin recovery dying with a raw database error on a stray space."""
    import cli.commands.admin as admin_mod

    looked_up: list[str] = []
    created: list[dict] = []

    class _Users:
        def get_by_email_ci(self, email):
            looked_up.append(email)
            return {"id": "u1"} if email == "ops@example.com" else None

        def create(self, **kw):  # pragma: no cover - must not be reached
            created.append(kw)
            raise AssertionError("existing account must be found, not re-created")

    class _Groups:
        def get_by_name(self, _name):
            return {"id": "g1"}

    class _Members:
        def has_membership(self, _uid, _gid):
            return True  # already a member → clean early return

    monkeypatch.setattr(admin_mod, "users_repo", lambda: _Users())
    monkeypatch.setattr(admin_mod, "user_groups_repo", lambda: _Groups())
    monkeypatch.setattr(admin_mod, "user_group_members_repo", lambda: _Members())
    monkeypatch.setattr(admin_mod, "use_pg", lambda: True, raising=False)

    admin_mod.break_glass_grant_admin(email="  Ops@Example.com  ", yes=True)
    assert looked_up == ["ops@example.com"], looked_up
    assert created == []
