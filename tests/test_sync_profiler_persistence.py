"""Regression: post-sync profiler persistence was silently broken.

The profiler block in ``app.api.sync._run_sync`` referenced the
``profile_repo`` factory without importing it — the resulting NameError was
swallowed by the block's broad except and surfaced only as a
``[SYNC] Profiler skipped: name 'profile_repo' is not defined`` stderr line,
so profiles were never persisted after any sync. A second latent bug in the
same block called ``.save`` on the factory *function* instead of the built
repository instance.
"""

from unittest.mock import patch


def test_profiles_persist_after_sync(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.api import sync as sync_module

    # A parquet for the profiler loop to pick up. Content is never read —
    # the profiler worker subprocess is mocked below.
    pq = tmp_path / "extracts" / "demo" / "data" / "t1.parquet"
    pq.parent.mkdir(parents=True)
    pq.write_bytes(b"x")

    fake_profile = {"rows": 3, "columns": [{"name": "a", "type": "BIGINT"}]}

    class FakeOrchestrator:
        def rebuild(self):
            return {"demo": ["t1"]}

    with (
        patch("src.orchestrator.SyncOrchestrator", FakeOrchestrator),
        patch(
            "src._subprocess_runner.run_subprocess_job",
            return_value=fake_profile,
        ),
        patch(
            "app.instance_config.get_data_source_type",
            return_value="csv",
        ),
    ):
        sync_module._run_sync(tables=None)

    err = capsys.readouterr().err
    assert "[SYNC] Profiler skipped" not in err
    assert "[SYNC] Profiled 1 tables" in err

    from src.repositories import profile_repo

    saved = profile_repo().get("t1")
    assert saved is not None
    saved.pop("profiled_at", None)  # repo stamps persistence time
    assert saved == fake_profile
