"""Tests for `agnes admin metrics {import,export,validate}`."""

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")
def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)

from cli.commands.admin import admin_app


def test_admin_metrics_subcommands_present():
    runner = CliRunner()
    result = runner.invoke(admin_app, ["metrics", "--help"])
    assert result.exit_code == 0
    assert "import" in _clean(result.output)
    assert "export" in _clean(result.output)
    assert "validate" in _clean(result.output)


def test_import_help_documents_the_reconcile_flags():
    runner = CliRunner()
    result = runner.invoke(admin_app, ["metrics", "import", "--help"])
    assert result.exit_code == 0
    out = _clean(result.output)
    assert "--prune" in out
    assert "--dry-run" in out
    assert "--source-ref" in out


class _FakeRepo:
    """Stands in for the metric repo so the CLI's own reporting is the subject.

    The reconcile semantics are covered against real backends in
    tests/test_metrics.py and tests/db_pg/test_ported_methods_contract.py.
    """

    def __init__(self, report):
        self.report = report
        self.calls = []

    def reconcile_from_yaml(self, path, **kwargs):
        self.calls.append((str(path), kwargs))
        return self.report


def _run(monkeypatch, tmp_path, report, argv):
    import cli.commands.admin_metrics as mod

    repo = _FakeRepo(report)
    monkeypatch.setattr(mod, "metric_repo", lambda: repo)
    monkeypatch.setattr(mod, "use_pg", lambda: True)  # skip opening the system DuckDB
    monkeypatch.setattr(mod, "_audit_prune", lambda *a, **k: None)
    (tmp_path / "m").mkdir()
    result = CliRunner().invoke(admin_app, ["metrics", "import", str(tmp_path / "m"), *argv])
    return result, repo


def test_import_without_prune_says_what_it_left_behind(monkeypatch, tmp_path):
    """An operator who forgot --prune must not read a clean import as
    'everything is in sync'."""
    report = {"added": ["a/1"], "updated": [], "adopted": [], "written": ["a/1"], "deleted": []}
    result, _ = _run(monkeypatch, tmp_path, report, [])
    assert result.exit_code == 0, result.output
    assert "--prune not set" in _clean(result.output)


def test_dry_run_says_would_and_passes_the_flag_through(monkeypatch, tmp_path):
    report = {"added": ["a/1"], "updated": [], "adopted": [], "written": ["a/1"], "deleted": ["a/2"]}
    result, repo = _run(monkeypatch, tmp_path, report, ["--dry-run", "--prune"])
    out = _clean(result.output)
    assert "Would import" in out
    assert "would delete a/2" in out
    flags = {k: v for k, v in repo.calls[0][1].items() if k != "on_delete"}
    assert flags == {"source_ref": None, "prune": True, "dry_run": True}
    assert callable(repo.calls[0][1]["on_delete"]), "the repo must be able to audit before each delete"


def test_prune_lists_each_deletion(monkeypatch, tmp_path):
    report = {"added": [], "updated": ["a/1"], "adopted": [], "written": ["a/1"], "deleted": ["a/2", "a/3"]}
    result, _ = _run(monkeypatch, tmp_path, report, ["--prune"])
    out = _clean(result.output)
    assert "deleted a/2" in out and "deleted a/3" in out


def test_source_ref_reaches_the_repo(monkeypatch, tmp_path):
    report = {"added": [], "updated": [], "adopted": [], "written": [], "deleted": []}
    _, repo = _run(monkeypatch, tmp_path, report, ["--source-ref", "finance", "--prune"])
    assert repo.calls[0][1]["source_ref"] == "finance"


def test_refused_prune_exits_with_the_reason_not_a_traceback(monkeypatch, tmp_path):
    """The repo refuses prune shapes that would empty the scope; the operator
    should read why, and the command should fail rather than look successful."""
    import cli.commands.admin_metrics as mod

    class _Refusing:
        def reconcile_from_yaml(self, path, **kwargs):
            raise ValueError("refusing to prune against a single file: …")

    monkeypatch.setattr(mod, "metric_repo", lambda: _Refusing())
    monkeypatch.setattr(mod, "use_pg", lambda: True)
    (tmp_path / "m").mkdir()
    result = CliRunner().invoke(admin_app, ["metrics", "import", str(tmp_path / "m"), "--prune"])
    assert result.exit_code == 1
    assert "refusing to prune" in _clean(result.output)
    assert "Traceback" not in _clean(result.output)


def test_taking_over_another_sources_metric_is_called_out(monkeypatch, tmp_path):
    """"added" would read as "a new metric appears"; this row already exists
    and is about to change owner, which the operator needs to see."""
    report = {"added": [], "updated": [], "adopted": ["revenue/mrr"], "written": ["revenue/mrr"], "deleted": []}
    result, _ = _run(monkeypatch, tmp_path, report, ["--dry-run"])
    out = _clean(result.output)
    assert "would take over revenue/mrr" in out
    assert "another source" in out


def test_deletions_carry_the_pre_upgrade_upload_caveat(monkeypatch, tmp_path):
    """Uploads made through /admin before the web_upload split are stamped
    yaml_import and cannot be told apart from CLI imports, so they fall in
    scope. The warning belongs where the deletions are listed, not only in the
    release notes."""
    report = {"added": [], "updated": [], "adopted": [], "written": [], "deleted": ["a/2"]}
    result, _ = _run(monkeypatch, tmp_path, report, ["--prune", "--dry-run"])
    out = _clean(result.output)
    assert "before this release are indistinguishable" in out


def test_no_caveat_when_there_is_nothing_to_delete(monkeypatch, tmp_path):
    report = {"added": ["a/1"], "updated": [], "adopted": [], "written": ["a/1"], "deleted": []}
    result, _ = _run(monkeypatch, tmp_path, report, ["--prune"])
    assert "indistinguishable" not in _clean(result.output)
