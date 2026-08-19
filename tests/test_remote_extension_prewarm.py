"""Community extensions a remote row needs must be installed at process start.

`src/db.py::_reattach_remote_extensions` is the query path and deliberately
issues `LOAD` only — no `INSTALL` — so a read-only query never reaches the
network. That holds only while the extension is already on disk. DuckDB installs
community extensions into a per-container directory that a container recreate
wipes, so after every restart (a daily auto-upgrade, say) the `LOAD` fails, the
ATTACH is skipped *silently*, and every query against a `query_mode='remote'`
row answers `Catalog "sf" does not exist` until someone re-saves the
registration by hand.

`prewarm_remote_attach_extensions` closes that: at startup it walks the extracts
and INSTALLs what the `_remote_attach` rows ask for, so the LOAD-only query path
finds them.
"""

import duckdb

from src.remote_extension_prewarm import prewarm_remote_attach_extensions


def _extract_with_remote_attach(root, source, alias, extension):
    d = root / source
    d.mkdir(parents=True)
    conn = duckdb.connect(str(d / "extract.duckdb"))
    conn.execute("CREATE TABLE _remote_attach (alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR)")
    conn.execute("INSERT INTO _remote_attach VALUES (?, ?, ?, ?)", [alias, extension, "database=X", ""])
    conn.close()
    return d


def test_installs_the_extension_a_remote_row_asks_for(tmp_path, monkeypatch):
    _extract_with_remote_attach(tmp_path, "snowflake", "sf", "snowflake")
    installed = []
    monkeypatch.setattr(
        "src.remote_extension_prewarm._install_extension",
        lambda name: installed.append(name),
    )

    result = prewarm_remote_attach_extensions(tmp_path)

    assert installed == ["snowflake"]
    assert result["installed"] == ["snowflake"]


def test_builtin_extensions_are_not_installed_from_community(tmp_path, monkeypatch):
    """A built-in ships with DuckDB; INSTALL FROM community would fail."""
    _extract_with_remote_attach(tmp_path, "somewhere", "x", "httpfs")
    installed = []
    monkeypatch.setattr("src.remote_extension_prewarm._install_extension", lambda n: installed.append(n))

    prewarm_remote_attach_extensions(tmp_path)

    assert installed == []


def test_an_extension_outside_the_allowlist_is_refused(tmp_path, monkeypatch):
    """The extract is connector-supplied input; it does not get to pick."""
    _extract_with_remote_attach(tmp_path, "evil", "e", "not_an_allowed_extension")
    installed = []
    monkeypatch.setattr("src.remote_extension_prewarm._install_extension", lambda n: installed.append(n))

    result = prewarm_remote_attach_extensions(tmp_path)

    assert installed == []
    assert result["refused"] == ["not_an_allowed_extension"]


def test_a_failing_install_never_breaks_startup(tmp_path, monkeypatch):
    """Startup must survive a network blip; the query path degrades as before."""
    _extract_with_remote_attach(tmp_path, "snowflake", "sf", "snowflake")

    def boom(name):
        raise RuntimeError("no network")

    monkeypatch.setattr("src.remote_extension_prewarm._install_extension", boom)

    result = prewarm_remote_attach_extensions(tmp_path)

    assert result["failed"] == ["snowflake"]
    assert result["installed"] == []


def test_missing_extracts_dir_is_a_no_op(tmp_path):
    result = prewarm_remote_attach_extensions(tmp_path / "nope")
    assert result == {"installed": [], "refused": [], "failed": []}


def test_extract_without_remote_attach_is_skipped(tmp_path, monkeypatch):
    d = tmp_path / "local"
    d.mkdir(parents=True)
    duckdb.connect(str(d / "extract.duckdb")).close()
    installed = []
    monkeypatch.setattr("src.remote_extension_prewarm._install_extension", lambda n: installed.append(n))

    prewarm_remote_attach_extensions(tmp_path)

    assert installed == []
