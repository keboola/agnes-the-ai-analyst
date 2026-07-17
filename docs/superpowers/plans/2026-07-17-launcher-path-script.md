# Launcher as PATH Script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the rc-file shell-function launcher written by `agnes init` with an executable wrapper script installed into `~/.local/bin` (POSIX) / `<word>.cmd` (Windows), and migrate existing installs via `agnes update`.

**Architecture:** `cli/lib/shortcut.py` keeps its public entry point `install_launcher_shortcut(workspace, no_shortcut=...)` (so `cli/commands/init.py` is almost untouched) but writes a marker-carrying executable script into `~/.local/bin` instead of appending a `function <word>` block to `~/.zshrc`/`~/.bashrc`/PowerShell profiles. Install also strips any legacy marked rc blocks (same sentinel format, precedent #783). A new `migrate_launcher_shortcut(workspace, quiet=...)` converges existing installs and is wired as a new best-effort step in `agnes update` (which runs detached at every SessionStart), so the fleet migrates without re-running `agnes init`. Opt-out semantics are preserved: migration only acts when there is evidence of a previous install.

**Tech Stack:** Python 3.13, typer, pytest (`tests/test_cli_init.py`, `tests/test_cli_update.py`).

## Global Constraints

- Vendor-agnostic public repo: no customer names, hostnames, or absolute `/Users/...` paths anywhere (code, tests, docs, plan).
- CHANGELOG bullet under `## [Unreleased]` in the same PR (Changed).
- Full suite before push: `.venv/bin/pytest tests/ --tb=short -n auto -q`.
- PostToolUse hook runs ruff fix/format + mypy on every edited Python file — keep style consistent, expect reformat churn.
- No AI attribution in commits. Branch pushes as lowercase `zs/...` (case-collision gotcha).
- The IWT contract (`docs/initial-workspace-override.md`): `workspace/bin/<raw_word>` naming and routing must keep working unchanged.
- `--no-shortcut` on `agnes init` must still suppress everything; users who opted out must NOT get a script re-added by `agnes update`.
- All shortcut writes are best-effort: failure never aborts `agnes init` or `agnes update`.

## Design invariants (shared vocabulary for all tasks)

- **bin dir:** `~/.local/bin` on every platform (`uv tool install` puts the `agnes` binary there, so PATH is already handled for anyone who can run `agnes`).
- **Script names:** `<word>` (POSIX, `#!/bin/sh`, mode 0o755), `<word>.cmd` (Windows cmd shim — works from cmd.exe and PowerShell regardless of ExecutionPolicy).
- **Ownership marker:** every script we write contains the line `# >>> agnes launcher: <word> <<<` (POSIX) / `rem >>> agnes launcher: <word> <<<` (Windows). A file containing the substring `>>> agnes launcher:` is ours and may be overwritten; anything else is foreign and must never be touched.
- **Collision ladder:** `_launcher_word()` (builtins + `agnes`/`claude` → `ai` suffix, unchanged) → then for each candidate `(word, word + "ai")`: reject if the target file exists and is foreign, or if `shutil.which(candidate)` resolves to a foreign executable elsewhere. Both candidates foreign ⇒ warn + skip.
- **Legacy cleanup words:** `{raw_word, launcher_word, launcher_word + "ai", chosen_word}` — covers pre-#783 blocks (`raw_word`), normal blocks, and suffixed blocks. Cleanup uses the existing `_strip_marked_block` against `~/.zshrc`, `~/.bashrc`, `~/.bash_profile` (POSIX) or both PowerShell profiles (Windows).
- **Migration evidence:** a legacy marked rc block for any cleanup word, or an ours-marked script already in the bin dir. No evidence ⇒ `migrate_launcher_shortcut` is a no-op (returns `"absent"`).

---

### Task 1: Rewrite `cli/lib/shortcut.py` — POSIX script installer + legacy rc cleanup

**Files:**
- Modify: `cli/lib/shortcut.py` (full rewrite of the write path; `_SHELL_BUILTINS`, `_RESERVED_COMMANDS`, `_sanitized_word`, `_launcher_word`, `_markers`, `_strip_marked_block`, `_defines_function` survive as-is)
- Test: `tests/test_cli_init.py` (replace the shortcut test section, lines ~552–1120)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `install_launcher_shortcut(workspace: Path, *, no_shortcut: bool = False, quiet: bool = False) -> None` (same name; new `quiet` kw suppresses the stdout `Shortcut :` line — needed by Task 3's update step).
  - `migrate_launcher_shortcut(workspace: Path, *, quiet: bool = True) -> str` returning `"absent" | "migrated" | "converged"` (implemented in this task for POSIX; Windows branch completed in Task 2).
  - Internal helpers used by tests: `_bin_dir(home) -> Path`, `_script_is_ours(path) -> bool`, `_posix_script(word, raw_word, workspace) -> str`.

- [ ] **Step 1: Replace the shortcut tests in `tests/test_cli_init.py`**

Delete every existing `test_shortcut_*` / `test_launcher_word_*` test (lines ~563–1120) EXCEPT `test_launcher_word_avoids_shell_builtin_collision` and `test_launcher_word_avoids_toolchain_command_collision` (pure-function tests, still valid) and `test_init_no_shortcut_flag_accepted` / `test_init_writes_shortcut_and_reports_it` (rewire below). Keep `_make_shortcut_env` as-is. Add:

```python
def _bin(home):
    return home / ".local" / "bin"


def test_shortcut_installs_executable_script_into_local_bin(tmp_path, monkeypatch):
    """POSIX: the launcher is a #!/bin/sh script in ~/.local/bin, 0o755,
    carrying our ownership marker, cd-ing into the workspace and exec-ing
    claude with --permission-mode auto. No rc file is touched."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    script = _bin(home) / "myworkspace"
    assert script.exists()
    assert os.access(script, os.X_OK)
    content = script.read_text(encoding="utf-8")
    assert content.startswith("#!/bin/sh\n")
    assert "# >>> agnes launcher: myworkspace <<<" in content
    assert f'cd "{workspace}" && exec claude --permission-mode auto "$@"' in content
    assert not (home / ".zshrc").exists()
    assert not (home / ".bashrc").exists()


def test_shortcut_routes_via_bin_launcher_when_present(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    bin_dir = workspace / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "myworkspace"
    launcher.write_text('#!/bin/bash\ncd workspace && claude --agent myworkspace "$@"\n')
    launcher.chmod(0o755)

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    content = (_bin(home) / "myworkspace").read_text(encoding="utf-8")
    assert f'exec "{launcher}" --permission-mode auto "$@"' in content


def test_shortcut_idempotent_reinstall_overwrites_own_script(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)
    first = (_bin(home) / "myworkspace").read_text(encoding="utf-8")
    install_launcher_shortcut(workspace)
    assert (_bin(home) / "myworkspace").read_text(encoding="utf-8") == first
    assert list(_bin(home).iterdir()) == [_bin(home) / "myworkspace"]


def test_shortcut_never_overwrites_foreign_file_suffixes_ai(tmp_path, monkeypatch):
    """A user-owned ~/.local/bin/myworkspace must survive; we fall back to
    myworkspaceai."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    _bin(home).mkdir(parents=True)
    foreign = _bin(home) / "myworkspace"
    foreign.write_text("#!/bin/sh\necho user script\n")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert foreign.read_text() == "#!/bin/sh\necho user script\n"
    assert (_bin(home) / "myworkspaceai").exists()


def test_shortcut_skips_when_both_names_foreign(tmp_path, monkeypatch, capsys):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    _bin(home).mkdir(parents=True)
    (_bin(home) / "myworkspace").write_text("user\n")
    (_bin(home) / "myworkspaceai").write_text("user\n")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert (_bin(home) / "myworkspace").read_text() == "user\n"
    assert (_bin(home) / "myworkspaceai").read_text() == "user\n"
    assert "skip" in capsys.readouterr().err.lower()


def test_shortcut_avoids_shadowing_binary_on_path(tmp_path, monkeypatch):
    """Workspace named like a real command (e.g. `Node`) must not shadow it —
    the PATH-level guard kicks in and the script gets the ai suffix."""
    home, workspace_parent = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    workspace = tmp_path / "Node"
    workspace.mkdir()
    import cli.lib.shortcut as shortcut

    def fake_which(cmd):
        return "/usr/bin/node" if cmd == "node" else None

    monkeypatch.setattr(shortcut.shutil, "which", fake_which)
    shortcut.install_launcher_shortcut(workspace)

    assert not (_bin(home) / "node").exists()
    assert (_bin(home) / "nodeai").exists()


def test_shortcut_no_shortcut_flag_writes_nothing(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace, no_shortcut=True)

    assert not (_bin(home)).exists()


def test_shortcut_write_failure_does_not_raise(tmp_path, monkeypatch, capsys):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    # Make ~/.local a file so mkdir of the bin dir fails.
    (home / ".local").write_text("not a dir")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)  # must not raise
    assert "could not install" in capsys.readouterr().err.lower()


def test_shortcut_skips_when_name_has_no_alphanumerics(tmp_path, monkeypatch, capsys):
    home, _ = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    workspace = tmp_path / "•••"
    workspace.mkdir()

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)
    assert not (_bin(home)).exists()
    assert "skipping" in capsys.readouterr().err.lower()


def test_shortcut_second_workspace_gets_own_script(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    other = tmp_path / "OtherTeam"
    other.mkdir()

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)
    install_launcher_shortcut(other)

    assert (_bin(home) / "myworkspace").exists()
    assert (_bin(home) / "otherteam").exists()


def test_install_removes_legacy_rc_blocks(tmp_path, monkeypatch, capsys):
    """Old marked function blocks (normal, pre-#783 raw-word, and suffixed)
    are stripped from ~/.zshrc and ~/.bashrc on install; user lines survive."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    legacy = (
        "# user line above\n"
        "\n# >>> agnes launcher: myworkspace <<<\n"
        'function myworkspace {\n  cd "/old/path" && claude --permission-mode auto "$@"\n}\n'
        "# <<< agnes launcher: myworkspace >>>\n"
        "# user line below\n"
    )
    (home / ".zshrc").write_text(legacy, encoding="utf-8")
    (home / ".bashrc").write_text(legacy, encoding="utf-8")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    for rc in (home / ".zshrc", home / ".bashrc"):
        content = rc.read_text(encoding="utf-8")
        assert "agnes launcher" not in content
        assert "# user line above" in content and "# user line below" in content
    assert (_bin(home) / "myworkspace").exists()


def test_install_warns_on_foreign_shadowing_function(tmp_path, monkeypatch, capsys):
    """An unmarked `function myworkspace` in an rc file would shadow the new
    PATH script in interactive shells — warn, never edit."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    (home / ".zshrc").write_text("function myworkspace {\n  echo mine\n}\n", encoding="utf-8")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert "function myworkspace" in (home / ".zshrc").read_text(encoding="utf-8")
    assert "shadow" in capsys.readouterr().err.lower()


def test_migrate_absent_is_noop(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")

    from cli.lib.shortcut import migrate_launcher_shortcut

    assert migrate_launcher_shortcut(workspace) == "absent"
    assert not (_bin(home)).exists()


def test_migrate_from_legacy_rc_block(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    (home / ".zshrc").write_text(
        "# >>> agnes launcher: myworkspace <<<\n"
        "function myworkspace {\n  cd x\n}\n"
        "# <<< agnes launcher: myworkspace >>>\n",
        encoding="utf-8",
    )

    from cli.lib.shortcut import migrate_launcher_shortcut

    assert migrate_launcher_shortcut(workspace) == "migrated"
    assert "agnes launcher" not in (home / ".zshrc").read_text(encoding="utf-8")
    assert (_bin(home) / "myworkspace").exists()


def test_migrate_converges_existing_script(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")

    from cli.lib.shortcut import install_launcher_shortcut, migrate_launcher_shortcut

    install_launcher_shortcut(workspace)
    assert migrate_launcher_shortcut(workspace) == "converged"
    assert (_bin(home) / "myworkspace").exists()
```

Also rewire `test_init_writes_shortcut_and_reports_it` (line ~729): its assertions on rc content change to `(home / ".local" / "bin" / "<word>").exists()` — read the test first, keep its init-flow scaffolding.

Note `_make_shortcut_env` monkeypatches `sys.platform` — `install_launcher_shortcut` dispatches on it, so "linux" forces the POSIX branch even on macOS dev machines. Add `import os` at the top of the test file if missing.

- [ ] **Step 2: Run new tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli_init.py -k "shortcut or launcher or migrate" -q`
Expected: FAIL (new assertions against `~/.local/bin`; `migrate_launcher_shortcut` not defined).

- [ ] **Step 3: Rewrite `cli/lib/shortcut.py`**

Keep: module imports (`os`, `re`, `sys`, `Path`, `typer`), `_SHELL_BUILTINS`, `_RESERVED_COMMANDS`, `_sanitized_word`, `_launcher_word`, `_markers`, `_strip_marked_block`, `_defines_function`. Add `import shutil`. Replace the module docstring (script-in-PATH design, legacy-rc cleanup, migration, ownership marker, collision ladder, opt-out preservation — see Design invariants). Delete `_rc_file`, `_posix_block`, `_windows_block`, `_warn_if_foreign_function`, `_heal_stale_shadowing_block`, `_install_posix`, `_install_windows` and add:

```python
_OWNERSHIP_TOKEN = ">>> agnes launcher:"


def _bin_dir(home: Path) -> Path:
    """Launcher install dir — the same ``~/.local/bin`` where ``uv tool
    install`` places the ``agnes`` binary itself, so anyone who can run
    ``agnes`` already has it on PATH (both POSIX and Windows)."""
    return home / ".local" / "bin"


def _script_is_ours(path: Path) -> bool:
    """True when ``path`` carries the ownership marker — only such files may
    be overwritten. Unreadable/binary files are treated as foreign."""
    try:
        return _OWNERSHIP_TOKEN in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _posix_script(word: str, raw_word: str, workspace: Path) -> str:
    """POSIX launcher script body.

    Routes through the IWT ``bin/<raw_word>`` launcher when present (same
    contract as before); otherwise cd + exec claude. ``exec`` keeps the
    process tree flat — the terminal's cwd is untouched because cd happens
    in the script's own process.
    """
    open_marker, _ = _markers(word)
    launch_cmd = f'cd "{workspace}" && exec claude --permission-mode auto "$@"'
    for candidate in dict.fromkeys((word, raw_word)):
        launcher = workspace / "bin" / candidate
        if launcher.exists() and os.access(launcher, os.X_OK):
            launch_cmd = f'exec "{launcher}" --permission-mode auto "$@"'
            break
    return (
        "#!/bin/sh\n"
        f"# {open_marker}\n"
        f"# Launcher for the {workspace.name} workspace. Managed by `agnes init`;\n"
        "# safe to delete — `agnes update` will not resurrect it once removed\n"
        "# together with the workspace.\n"
        f"{launch_cmd}\n"
    )


def _windows_script(word: str, raw_word: str, workspace: Path) -> str:
    """Windows ``.cmd`` shim body — works from cmd.exe AND PowerShell and,
    unlike the old ``$PROFILE`` function, is immune to the default
    ``ExecutionPolicy Restricted`` (which silently blocks profile loading)."""
    open_marker, _ = _markers(word)
    launch_cmd = f'cd /d "{workspace}"\r\nclaude --permission-mode auto %*'
    bin_dir = workspace / "bin"
    candidates = [
        bin_dir / f"{candidate}{ext}" for candidate in dict.fromkeys((word, raw_word)) for ext in (".cmd", ".ps1")
    ]
    for launcher in candidates:
        if launcher.exists():
            if launcher.suffix == ".ps1":
                launch_cmd = f'powershell -ExecutionPolicy Bypass -File "{launcher}" --permission-mode auto %*'
            else:
                launch_cmd = f'call "{launcher}" --permission-mode auto %*'
            break
    return f"@echo off\r\nrem {open_marker}\r\n{launch_cmd}\r\n"


def _script_ext() -> str:
    return ".cmd" if sys.platform == "win32" else ""


def _choose_target(workspace_name: str, bin_dir: Path) -> tuple[str, Path] | tuple[None, None]:
    """Collision ladder: builtin/toolchain guard, then foreign-file and
    foreign-PATH-executable guards, with one ``ai``-suffix retry."""
    base = _launcher_word(workspace_name)
    if not base:
        return None, None
    ext = _script_ext()
    for candidate in dict.fromkeys((base, base + "ai")):
        target = bin_dir / f"{candidate}{ext}"
        if target.exists() and not _script_is_ours(target):
            continue
        resolved = shutil.which(candidate)
        if resolved is not None:
            resolved_path = Path(resolved)
            if resolved_path != target and not _script_is_ours(resolved_path):
                continue
        return candidate, target
    return None, None


def _rc_paths(home: Path) -> list[Path]:
    if sys.platform == "win32":
        return _ps_profile_paths(home)
    return [home / ".zshrc", home / ".bashrc", home / ".bash_profile"]


def _cleanup_words(workspace_name: str, chosen: str | None) -> set[str]:
    """Every word a legacy block may have been written under: pre-#783 raw
    word, the guarded word, its suffixed form, and the freshly chosen word."""
    raw = _sanitized_word(workspace_name)
    base = _launcher_word(workspace_name)
    words = {raw, base, base + "ai" if base else ""}
    if chosen:
        words.add(chosen)
    return {w for w in words if w}


def _cleanup_legacy_rc_blocks(home: Path, words: set[str]) -> list[Path]:
    """Strip our marked launcher blocks from shell rc / PowerShell profiles.

    Returns the files that were modified. Only sentinel-delimited text is
    removed (see ``_strip_marked_block``); user lines are never touched.
    """
    cleaned: list[Path] = []
    for rc in _rc_paths(home):
        if not rc.exists():
            continue
        try:
            content = rc.read_text(encoding="utf-8")
        except OSError:
            continue
        stripped = content
        for word in words:
            stripped = _strip_marked_block(stripped, word)
        if stripped != content:
            rc.write_text(stripped, encoding="utf-8")
            cleaned.append(rc)
    return cleaned


def _warn_if_shadowing_function(home: Path, word: str) -> None:
    """An unmarked ``function <word>`` in an rc file takes precedence over the
    PATH script in interactive shells. User content — warn, never edit."""
    if sys.platform == "win32":
        return
    for rc in _rc_paths(home):
        if not rc.exists():
            continue
        try:
            content = rc.read_text(encoding="utf-8")
        except OSError:
            continue
        if _defines_function(content, word):
            typer.echo(
                f"  Warning  : {rc.name} defines a `{word}` shell function that will "
                f"shadow the new launcher script in interactive shells. Delete that "
                f"`function {word}` block to use the script.",
                err=True,
            )
```

Then the two public entry points:

```python
def install_launcher_shortcut(workspace: Path, *, no_shortcut: bool = False, quiet: bool = False) -> None:
    """Install the one-word launcher script into ``~/.local/bin`` and remove
    any legacy rc-function blocks a previous version wrote."""
    if no_shortcut:
        return

    if not _sanitized_word(workspace.name):
        typer.echo(
            f"  Warning  : workspace name {workspace.name!r} has no alphanumeric "
            "characters to derive a launcher word from; skipping shortcut.",
            err=True,
        )
        return

    _home_env = os.environ.get("HOME")
    home = Path(_home_env) if _home_env else Path(os.path.expanduser("~"))

    try:
        bin_dir = _bin_dir(home)
        chosen, target = _choose_target(workspace.name, bin_dir)

        cleaned = _cleanup_legacy_rc_blocks(home, _cleanup_words(workspace.name, chosen))
        for rc in cleaned:
            typer.echo(
                f"  Note     : removed the legacy launcher shell function from {rc.name} "
                "— the launcher is now a script on PATH.",
                err=True,
            )

        if chosen is None or target is None:
            typer.echo(
                "  Warning  : could not pick a launcher name that does not collide "
                "with an existing command; skipping shortcut.",
                err=True,
            )
            return

        raw_word = _sanitized_word(workspace.name)
        if sys.platform == "win32":
            script = _windows_script(chosen, raw_word, workspace)
        else:
            script = _posix_script(chosen, raw_word, workspace)
        bin_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(script, encoding="utf-8")
        if sys.platform != "win32":
            target.chmod(0o755)

        _warn_if_shadowing_function(home, chosen)
        if shutil.which(chosen) is None:
            typer.echo(
                f"  Note     : {bin_dir} is not on PATH in this shell — add it "
                f'(export PATH="$HOME/.local/bin:$PATH") to use `{chosen}`.',
                err=True,
            )
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"  Warning  : could not install launcher shortcut ({exc}). Add it manually: see `agnes init --help`.",
            err=True,
        )
        return

    if not quiet:
        typer.echo(f"  Shortcut : type `{chosen}` from any terminal to launch")


def migrate_launcher_shortcut(workspace: Path, *, quiet: bool = True) -> str:
    """Converge an existing launcher install to the script form (used by
    ``agnes update``).

    Acts only on evidence of a previous install — a legacy marked rc block or
    an ours-marked script — so ``agnes init --no-shortcut`` users stay
    untouched. Returns ``"absent"``, ``"migrated"`` (legacy rc block found and
    replaced) or ``"converged"`` (script already present, re-asserted).
    """
    _home_env = os.environ.get("HOME")
    home = Path(_home_env) if _home_env else Path(os.path.expanduser("~"))
    words = _cleanup_words(workspace.name, None)
    if not words:
        return "absent"

    had_legacy = False
    for rc in _rc_paths(home):
        if not rc.exists():
            continue
        try:
            content = rc.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(_markers(w)[0] in content for w in words):
            had_legacy = True
            break

    ext = _script_ext()
    has_script = any(
        (candidate := _bin_dir(home) / f"{w}{ext}").exists() and _script_is_ours(candidate) for w in words
    )

    if not had_legacy and not has_script:
        return "absent"

    install_launcher_shortcut(workspace, quiet=quiet)
    return "migrated" if had_legacy else "converged"
```

- [ ] **Step 4: Run the shortcut tests**

Run: `.venv/bin/pytest tests/test_cli_init.py -q`
Expected: PASS (all, including the untouched init-flow tests).

- [ ] **Step 5: Commit**

```bash
git add cli/lib/shortcut.py tests/test_cli_init.py
git commit -m "feat(cli): install launcher as a PATH script instead of an rc shell function"
```

---

### Task 2: Windows coverage

**Files:**
- Modify: `cli/lib/shortcut.py` (only if Step 2 exposes gaps — the Windows branch is already written in Task 1)
- Test: `tests/test_cli_init.py`

**Interfaces:**
- Consumes: Task 1's `install_launcher_shortcut`, `_ps_profile_paths` (kept from the old module).
- Produces: verified `<word>.cmd` behavior; nothing new.

- [ ] **Step 1: Write the failing Windows tests**

```python
def test_shortcut_windows_writes_cmd_shim(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "win32")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    shim = _bin(home) / "myworkspace.cmd"
    assert shim.exists()
    content = shim.read_text(encoding="utf-8")
    assert content.startswith("@echo off")
    assert "rem >>> agnes launcher: myworkspace <<<" in content
    assert f'cd /d "{workspace}"' in content
    assert "claude --permission-mode auto %*" in content


def test_shortcut_windows_cleans_both_powershell_profiles(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "win32")
    legacy = (
        "# >>> agnes launcher: myworkspace <<<\n"
        "function myworkspace {\n  Set-Location x\n}\n"
        "# <<< agnes launcher: myworkspace >>>\n"
    )
    from cli.lib.shortcut import _ps_profile_paths

    for profile in _ps_profile_paths(home):
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(legacy, encoding="utf-8")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    for profile in _ps_profile_paths(home):
        assert "agnes launcher" not in profile.read_text(encoding="utf-8")
    assert (_bin(home) / "myworkspace.cmd").exists()


def test_shortcut_windows_routes_via_bin_cmd_launcher(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "win32")
    bin_dir = workspace / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "myworkspace.cmd"
    launcher.write_text("@echo off\r\nclaude --agent myworkspace %*\r\n")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    content = (_bin(home) / "myworkspace.cmd").read_text(encoding="utf-8")
    assert f'call "{launcher}" --permission-mode auto %*' in content
```

Gotcha: on `sys.platform == "win32"` the chmod call is skipped and `_choose_target` appends `.cmd`; `shutil.which` on a POSIX dev machine won't resolve `.cmd` files, which is fine — the PATH-note branch just fires into stderr and the test ignores it.

- [ ] **Step 2: Run tests**

Run: `.venv/bin/pytest tests/test_cli_init.py -k "windows" -q`
Expected: PASS directly if Task 1's Windows branch is correct; otherwise fix `cli/lib/shortcut.py` until green.

- [ ] **Step 3: Commit**

```bash
git add cli/lib/shortcut.py tests/test_cli_init.py
git commit -m "test(cli): cover Windows .cmd launcher shim + profile cleanup"
```

---

### Task 3: `agnes update` migration step

**Files:**
- Modify: `cli/commands/update.py` (new `_step_launcher` + wiring; module docstring step list)
- Test: `tests/test_cli_update.py`

**Interfaces:**
- Consumes: `migrate_launcher_shortcut(workspace, quiet=True) -> str` from Task 1.
- Produces: report line `{"stage": "launcher", "status": "ok", "detail": <status>}`.

- [ ] **Step 1: Write the failing test in `tests/test_cli_update.py`**

First read the file's existing fixtures/stubs and mimic its patterns for invoking steps. Add (adjust imports/fixtures to the file's local conventions):

```python
def test_step_launcher_migrates_legacy_block(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.platform", "linux")
    workspace = tmp_path / "MyWorkspace"
    workspace.mkdir()
    (home / ".zshrc").write_text(
        "# >>> agnes launcher: myworkspace <<<\nfunction myworkspace {\n  cd x\n}\n# <<< agnes launcher: myworkspace >>>\n",
        encoding="utf-8",
    )

    from cli.commands.update import _step_launcher

    report: list[dict] = []
    _step_launcher(workspace, report=report)

    assert report == [{"stage": "launcher", "status": "ok", "detail": "migrated"}]
    assert "agnes launcher" not in (home / ".zshrc").read_text(encoding="utf-8")
    assert (home / ".local" / "bin" / "myworkspace").exists()


def test_step_launcher_noop_without_evidence(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.platform", "linux")
    workspace = tmp_path / "MyWorkspace"
    workspace.mkdir()

    from cli.commands.update import _step_launcher

    report: list[dict] = []
    _step_launcher(workspace, report=report)

    assert report == [{"stage": "launcher", "status": "ok", "detail": "absent"}]
    assert not (home / ".local" / "bin").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli_update.py -k launcher -q`
Expected: FAIL — `_step_launcher` not defined.

- [ ] **Step 3: Implement `_step_launcher` and wire it**

In `cli/commands/update.py`, after `_step_agnes_owned` (def around line 312), add:

```python
# --------------------------------------------------------------------------- #
# Step 3b — launcher shortcut (migrates legacy rc-function installs to the
# ~/.local/bin script; no-op when the user never had a shortcut, preserving
# the `agnes init --no-shortcut` opt-out).
# --------------------------------------------------------------------------- #
def _step_launcher(workspace: Path, *, report: list[dict]) -> None:
    from cli.lib.shortcut import migrate_launcher_shortcut

    status = migrate_launcher_shortcut(workspace, quiet=True)
    report.append({"stage": "launcher", "status": "ok", "detail": status})
```

Wire it in the workspace-steps block (anchor: the `_run_step("agnes-owned", ...)` call at line ~572), directly after that line:

```python
                    _run_step("launcher", lambda: _step_launcher(workspace, report=report), report)
```

Also add a line to the module docstring's numbered step list (after step 3): `3b. Launcher shortcut — migrate a legacy rc-function launcher to the ~/.local/bin script; skipped when no prior install evidence exists.`

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_cli_update.py tests/test_cli_init.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli/commands/update.py tests/test_cli_update.py
git commit -m "feat(cli): agnes update migrates rc-function launchers to PATH scripts"
```

---

### Task 4: Copy, docs & changelog

**Files:**
- Modify: `cli/commands/init.py:264-268` (`--no-shortcut` help), `cli/commands/init.py:982-987` (comment)
- Modify: `docs/initial-workspace-override.md:205-225` (naming/collision contract section)
- Modify: `CHANGELOG.md` (`## [Unreleased]`)

**Interfaces:** none — text only.

- [ ] **Step 1: Update `cli/commands/init.py` copy**

Help text at line ~266: replace the rc-file mention, e.g. `"Skip installing the one-word launcher script into ~/.local/bin (POSIX) / <word>.cmd (Windows)."` Update the comment block above `install_launcher_shortcut(...)` (line ~982) to say "launcher script" instead of rc-function wording (keep the IWT/default-mode routing note).

- [ ] **Step 2: Update `docs/initial-workspace-override.md`**

In the naming-contract / collision-guard paragraphs (lines ~205–225): the shortcut is now an executable script `~/.local/bin/<word>` (POSIX) / `<word>.cmd` (Windows); the collision guard renames the *script* (`Agnes` → `agnesai`) and additionally refuses to shadow an existing PATH executable; the `bin/<word>` IWT naming contract and both-names lookup are unchanged. Mention that legacy rc-function blocks are removed automatically by `agnes init`/`agnes update`.

- [ ] **Step 3: Sweep for stale mentions**

Run: `grep -rn "shell function\|zshrc\|launcher" docs/ cli/ README.md --include="*.md" --include="*.py" | grep -iv "test\|archive\|node_modules" | grep -i "launcher\|shortcut"`
Fix any remaining copy that still describes the rc-function mechanism (excluding `docs/archive/` and CHANGELOG history, which are immutable records).

- [ ] **Step 4: CHANGELOG bullet under `## [Unreleased]`**

```markdown
### Changed
- The one-word workspace launcher installed by `agnes init` is now an executable script in `~/.local/bin` (`<word>.cmd` on Windows) instead of a shell function appended to `~/.zshrc` / `~/.bashrc` / PowerShell profiles. Scripts are visible to `which`, work from non-interactive shells, and on Windows are immune to `ExecutionPolicy Restricted` (which silently blocked the old profile function). The collision guard now also refuses to shadow any existing PATH executable. `agnes init` and `agnes update` automatically remove the legacy marked rc-function blocks; users who opted out via `--no-shortcut` are left untouched.
```

- [ ] **Step 5: Commit**

```bash
git add cli/commands/init.py docs/initial-workspace-override.md CHANGELOG.md
git commit -m "docs(cli): describe PATH-script launcher; changelog"
```

---

### Task 5: Full suite, push, PR

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS. Unrelated failures: confirm with `git stash` they reproduce on a clean tree, note them in the PR body, don't block.

- [ ] **Step 2: Vendor-agnostic scan**

Run: `git diff main... | grep -inE "keboola|groupon|foundry|zsrotyr|/Users/"`
Expected: no hits (plan file uses relative paths only).

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin HEAD:refs/heads/zs/launcher-path-script
gh pr create --title "Install workspace launcher as a PATH script instead of an rc shell function" --body "<summary + migration story + test notes>"
```

Then run the mandatory review loop (`/agnes-review` → fix → Devin Review → repeat) before any merge; release-cut decision (patch bump) happens per `docs/RELEASING.md` at merge time.

## Self-review notes

- Spec coverage: script install (T1), legacy cleanup (T1), Windows (T2), update migration (T3), docs/CHANGELOG (T4) — all requirements from the discussion covered; opt-out preservation is a tested invariant (T3 no-op test).
- Type consistency: `install_launcher_shortcut(workspace, *, no_shortcut=False, quiet=False)` and `migrate_launcher_shortcut(workspace, *, quiet=True) -> str` used identically in Tasks 1/3.
- Known non-goals: cleaning the developer's own polluted `~/.zshrc` (separate, machine-local); test-HOME-isolation audit (separate spawned task); PATH bootstrap for shells without `~/.local/bin` (already handled by the install flow that delivers `agnes` itself).
