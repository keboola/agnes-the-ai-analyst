"""Tests for `agnes admin metrics {import,export,validate}`."""

from typer.testing import CliRunner

from cli.commands.admin import admin_app

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


def test_admin_metrics_subcommands_present():
    runner = CliRunner()
    result = runner.invoke(admin_app, ["metrics", "--help"])
    assert result.exit_code == 0
    assert "import" in _clean(result.output)
    assert "export" in _clean(result.output)
    assert "validate" in _clean(result.output)


class TestImportExportValidateWriteThroughFactory:
    """Regression: `import`/`export`/`validate` each used to open a
    `get_system_db()` connection purely to keep it alive around the
    `metric_repo()`/`table_registry_repo()` factory calls, never reading
    from it directly. Locks in that dropping the dead connections didn't
    break the actual read/write paths."""

    def test_import_writes_metric(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        (tmp_path / "data").mkdir()
        yaml_file = tmp_path / "revenue.yml"
        yaml_file.write_text("name: mrr\ncategory: revenue\ndisplay_name: MRR\nsql: SELECT SUM(amount) FROM orders\n")

        runner = CliRunner()
        result = runner.invoke(admin_app, ["metrics", "import", str(yaml_file)])
        assert result.exit_code == 0, result.output
        assert "Imported 1 metric(s)" in result.output

        from src.repositories import metric_repo

        metrics = metric_repo().list()
        assert any(m["name"] == "mrr" for m in metrics)

    def test_export_reads_back_imported_metric(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        (tmp_path / "data").mkdir()
        yaml_file = tmp_path / "revenue.yml"
        yaml_file.write_text("name: arr\ncategory: revenue\nsql: SELECT 1\n")

        runner = CliRunner()
        r1 = runner.invoke(admin_app, ["metrics", "import", str(yaml_file)])
        assert r1.exit_code == 0, r1.output

        out_dir = tmp_path / "export"
        r2 = runner.invoke(admin_app, ["metrics", "export", "--dir", str(out_dir)])
        assert r2.exit_code == 0, r2.output
        assert "Exported 1 metric(s)" in r2.output
        assert (out_dir / "revenue" / "arr.yml").exists()

    def test_validate_flags_unregistered_table(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        (tmp_path / "data").mkdir()
        yaml_file = tmp_path / "revenue.yml"
        yaml_file.write_text("name: mrr\ncategory: revenue\ntable: no_such_table\nsql: SELECT 1\n")
        runner = CliRunner()
        r1 = runner.invoke(admin_app, ["metrics", "import", str(yaml_file)])
        assert r1.exit_code == 0, r1.output

        r2 = runner.invoke(admin_app, ["metrics", "validate"])
        assert r2.exit_code == 1, r2.output
        assert "WARN" in r2.output
        assert "no_such_table" in r2.output
