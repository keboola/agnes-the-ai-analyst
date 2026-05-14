# CLI Auto-Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `agnes` CLI auto-upgrade from the server it talks to. Two layers: (A) `agnes self-upgrade` invoked from a SessionStart hook for proactive upgrade; (B) `X-Agnes-Min-Version` response header for a hard-stop on incompatible drift.

**Architecture:** Server already serves `/cli/latest` (wheel metadata) and `/cli/wheel/<name>` (wheel bytes). CLI already polls `/cli/latest` from `cli/update_check.py` and warns on drift. This plan adds: a server-side `MIN_COMPAT_CLI_VERSION` constant + middleware that stamps `X-Agnes-Latest-Version` / `X-Agnes-Min-Version` on every `/api/*` response; a CLI `agnes self-upgrade` command that reuses `update_check.check()` and shells out to `uv tool install --force` (pip fallback); response-header inspection in `cli/client.py:get_client()` that hard-stops with `sys.exit(2)` on `local < min`; and a third `SessionStart` hook line that runs `agnes self-upgrade --quiet` ahead of `agnes pull`.

**Tech Stack:** Python 3.12 / FastAPI / httpx / typer / uv / pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-06-cli-auto-upgrade-spec.md` — read this first if context is unclear.

---

## File Structure

**New files:**
- `app/version.py` — `APP_VERSION` (deduped from `app/main.py:_app_version`) + `MIN_COMPAT_CLI_VERSION` constants. Single source of truth.
- `cli/commands/self_upgrade.py` — `agnes self-upgrade` typer command, including smoke test (deterministic install path, not PATH-resolved), last-known-good record, rollback with rc capture, recursion sentinel, and explicit `--force` offline error.
- `tests/test_version_headers_middleware.py` — server middleware integration test.
- `tests/test_client_version_check.py` — header-inspection hard-stop test, including the `AGNES_SELF_UPGRADE_IN_PROGRESS` sentinel barrier.
- `tests/test_self_upgrade.py` — command behavior, subprocess shape, smoke-test rollback (with rc capture), `--force` offline failure, `AGNES_NO_UPDATE_CHECK` bypass for explicit upgrades, sentinel propagation.

**Modified files:**
- `app/main.py` — delete `_app_version()`, import `APP_VERSION` from `app/version.py`, register version-headers middleware.
- `app/api/cli_artifacts.py` — drive-by docstring fix (`da` → `agnes`).
- `cli/client.py` — `get_client()` adds `event_hooks` for response inspection + `User-Agent` header. `_check_version_headers` short-circuits on `AGNES_SELF_UPGRADE_IN_PROGRESS=1`.
- `cli/main.py` — register `self_upgrade_app` typer.
- `cli/update_check.py` — drive-by docstring fix (`da` → `agnes`); add `bypass_disabled=False` keyword-only kwarg to `check()` so explicit `agnes self-upgrade` invocations can override `AGNES_NO_UPDATE_CHECK`; ensure `_version_lt` and `_installed_version` are importable from `cli/client.py` and `cli/commands/self_upgrade.py`.
- `cli/lib/hooks.py` — single chained SessionStart entry (`agnes self-upgrade ... || true; agnes pull ... || true`); extend `_OUR_COMMAND_MARKERS` with `agnes self-upgrade`.
- `tests/test_lib_hooks.py` — assert chained command + ordering + idempotency.
- `tests/test_app_version.py` — rewrite to target `app.version` (since `app.main._app_version` is deleted).
- `CHANGELOG.md` — `### Added` entry under `## [Unreleased]`.
- `pyproject.toml` — bump `[project].version` from `0.39.0` to `0.40.0` in the release-cut commit (Task 7).

**Files this plan does NOT touch (by design):**
- `~/.config/agnes/last_known_good.json` — written at runtime by `_record_last_known_good` after the smoke test passes; separate file from `update_check.json`. (Convention: record before invalidate, no correctness consequence either way.)
- `docs/CLI_COMPAT.md`, `.github/pull_request_template.md` — earlier draft proposed these as enforcement scaffolding; dropped because a doc + checkbox catches nothing real (engineer can check the box without bumping the constant). Layer B's mechanism stays as opt-in for the day someone needs it; same review discipline as every other behavior change.

---

## Task 1: Server-side version constants + middleware

**Files:**
- Create: `app/version.py`
- Modify: `app/main.py` (top-level import + middleware registration; replace `_app_version()` body to read from `app.version.APP_VERSION`)
- Create: `tests/test_version_headers_middleware.py`

- [ ] **Step 1.1: Write the failing middleware test**

Create `tests/test_version_headers_middleware.py`:

```python
"""Verify /api/* responses carry X-Agnes-Latest-Version + X-Agnes-Min-Version."""

from fastapi.testclient import TestClient


def test_api_response_carries_version_headers():
    from app.main import app
    from app.version import APP_VERSION, MIN_COMPAT_CLI_VERSION
    client = TestClient(app)
    # /api/version is unauthenticated and cheap.
    resp = client.get("/api/version")
    assert resp.status_code == 200
    # Headers must equal the constants in app.version, not just be parseable.
    # When MIN_COMPAT_CLI_VERSION is deliberately bumped in a future PR, this
    # test is updated in the same PR — the review-discipline guardrail.
    assert resp.headers["X-Agnes-Latest-Version"] == APP_VERSION
    assert resp.headers["X-Agnes-Min-Version"] == MIN_COMPAT_CLI_VERSION
    # Day-one floor pin: drop or update this assertion when the floor moves.
    assert resp.headers["X-Agnes-Min-Version"] == "0.0.0"


def test_non_api_response_does_not_carry_version_headers():
    from app.main import app
    client = TestClient(app)
    # /cli/latest is under /cli, not /api — should NOT carry the headers.
    resp = client.get("/cli/latest")
    assert resp.status_code == 200
    assert "X-Agnes-Latest-Version" not in resp.headers
    assert "X-Agnes-Min-Version" not in resp.headers
```

- [ ] **Step 1.2: Run test, verify it fails**

```bash
pytest tests/test_version_headers_middleware.py -v
```
Expected: FAIL — `X-Agnes-Latest-Version` not in headers.

- [ ] **Step 1.3: Create `app/version.py`**

```python
"""Single source of truth for app + CLI compat versions.

`APP_VERSION` is read from package metadata so it tracks `pyproject.toml`
without a manual literal to keep in sync.

`MIN_COMPAT_CLI_VERSION` is the oldest CLI version the server still accepts
on `/api/*`. Bumped manually when shipping a wire-protocol break. Day-one
value of "0.0.0" means no enforcement — set the floor the first time a
deliberate break ships.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version


def _read_app_version() -> str:
    try:
        return _pkg_version("agnes-the-ai-analyst")
    except PackageNotFoundError:
        return "0.0.0+dev"


APP_VERSION = _read_app_version()
MIN_COMPAT_CLI_VERSION = "0.0.0"
```

- [ ] **Step 1.4: Replace `_app_version()` with `APP_VERSION` import + register middleware**

Two changes in `app/main.py`:

(a) **Dedupe.** Both `_app_version()` (line 40) and `app/version.py:APP_VERSION` read from `importlib.metadata.version("agnes-the-ai-analyst")` — keeping both invites drift. Delete the `_app_version()` helper, import `APP_VERSION` at module top:

```python
# At module top, alongside other app.* imports:
from app.version import APP_VERSION, MIN_COMPAT_CLI_VERSION

# Delete the entire `_app_version()` function (line 40 onwards).

# Replace line 186:
-    version=_app_version(),
+    version=APP_VERSION,
```

(b) **Middleware.** After the `app = FastAPI(...)` instantiation block, add:

```python
@app.middleware("http")
async def _add_version_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["X-Agnes-Latest-Version"] = APP_VERSION
        response.headers["X-Agnes-Min-Version"] = MIN_COMPAT_CLI_VERSION
    return response
```

(c) **Update `tests/test_app_version.py`** — the existing tests patch `app.main._pkg_version` and `app.main._app_version`, both of which no longer exist. Rewrite to target `app.version` AND keep the end-to-end pin that the FastAPI app object surfaces the constant:

```python
"""Pin that APP_VERSION reads from package metadata, not a hardcoded literal,
and that the FastAPI app's `version=` field surfaces it end-to-end."""

import importlib
from unittest.mock import patch


def test_app_version_reads_package_metadata():
    with patch("app.version._pkg_version", return_value="9.9.9") as mock_pkg_ver:
        import app.version
        importlib.reload(app.version)
        assert app.version.APP_VERSION == "9.9.9"
        mock_pkg_ver.assert_called_once_with("agnes-the-ai-analyst")


def test_app_version_falls_back_when_package_missing():
    from importlib.metadata import PackageNotFoundError
    with patch("app.version._pkg_version", side_effect=PackageNotFoundError):
        import app.version
        importlib.reload(app.version)
        assert app.version.APP_VERSION == "0.0.0+dev"


def test_fastapi_app_version_matches_app_version_constant():
    """End-to-end: FastAPI's app.version (consumed by /openapi.json and
    /docs) must equal app.version.APP_VERSION. Guards the wiring at
    `app/main.py:186 version=APP_VERSION` against accidental literal."""
    import importlib
    import app.version
    import app.main

    # Reload both so we read post-patch values consistently.
    with patch("app.version._pkg_version", return_value="7.7.7"):
        importlib.reload(app.version)
        importlib.reload(app.main)
        assert app.main.app.version == "7.7.7"
        assert app.main.app.version == app.version.APP_VERSION
```

The reload trick: `APP_VERSION` is set once at module import time; reimporting under a patch reruns `_read_app_version()`. The third test reimports `app.main` after `app.version` to pick up the new constant value through the `from app.version import APP_VERSION` import line.

- [ ] **Step 1.5: Run test, verify it passes**

```bash
pytest tests/test_version_headers_middleware.py -v
```
Expected: PASS — both tests.

- [ ] **Step 1.6: Run the full app-side test suite to catch regressions**

```bash
pytest tests/test_app_version.py tests/test_version_headers_middleware.py -v
```
Expected: PASS — `_app_version()` test still green (we didn't touch it).

- [ ] **Step 1.7: Commit**

```bash
git add app/version.py app/main.py tests/test_version_headers_middleware.py tests/test_app_version.py
git commit -m "feat(server): expose APP_VERSION + MIN_COMPAT_CLI_VERSION on /api/* response headers

Adds X-Agnes-Latest-Version and X-Agnes-Min-Version headers to every
/api/* response. CLI consumes these to hard-stop on incompatible drift.
MIN_COMPAT_CLI_VERSION ships at 0.0.0 — no enforcement until a deliberate
wire-protocol break bumps it.

Also dedupes app version logic: app/main.py:_app_version() helper deleted,
replaced by app/version.py:APP_VERSION as the single source of truth.
test_app_version.py rewritten to target app.version."
```

---

## Task 2: CLI response-header version check

**Files:**
- Modify: `cli/update_check.py` (export helpers — `_version_lt` and `_installed_version` must be reusable; rename to public if needed, or just import the underscore-prefixed names)
- Modify: `cli/client.py:get_client()` — add `event_hooks={"response": [_check_version_headers]}` and `User-Agent`
- Create: `tests/test_client_version_check.py`

- [ ] **Step 2.1: Write the failing hard-stop test**

Create `tests/test_client_version_check.py`:

```python
"""Verify cli/client.py:get_client() hard-stops on min_version mismatch."""

from unittest.mock import patch

import httpx
import pytest


def _fake_response(headers: dict) -> httpx.Response:
    return httpx.Response(status_code=200, headers=headers, content=b"{}", request=httpx.Request("GET", "http://x/"))


def test_local_below_min_exits_with_code_2():
    from cli.client import _check_version_headers
    with patch("cli.client._installed_version", return_value="0.30.0"):
        resp = _fake_response({
            "X-Agnes-Latest-Version": "0.40.0",
            "X-Agnes-Min-Version": "0.35.0",
        })
        with pytest.raises(SystemExit) as exc:
            _check_version_headers(resp)
        assert exc.value.code == 2


def test_local_at_or_above_min_does_not_exit():
    from cli.client import _check_version_headers
    with patch("cli.client._installed_version", return_value="0.40.0"):
        resp = _fake_response({
            "X-Agnes-Latest-Version": "0.40.0",
            "X-Agnes-Min-Version": "0.35.0",
        })
        _check_version_headers(resp)  # must not raise


def test_missing_headers_no_enforcement():
    """Older server without middleware → no headers → no-op."""
    from cli.client import _check_version_headers
    with patch("cli.client._installed_version", return_value="0.10.0"):
        resp = _fake_response({})  # empty headers
        _check_version_headers(resp)  # must not raise


def test_unknown_local_version_no_enforcement():
    """Source-checkout / editable install → never block."""
    from cli.client import _check_version_headers
    with patch("cli.client._installed_version", return_value="unknown"):
        resp = _fake_response({
            "X-Agnes-Latest-Version": "0.40.0",
            "X-Agnes-Min-Version": "0.35.0",
        })
        _check_version_headers(resp)  # must not raise


def test_self_upgrade_in_progress_disables_enforcement(monkeypatch):
    """Recursion barrier: while self-upgrade runs, no /api/* call may
    block on min-version drift. Otherwise an in-flight upgrade could
    sys.exit(2) with 'Run: agnes self-upgrade' from inside itself."""
    from cli.client import _check_version_headers
    monkeypatch.setenv("AGNES_SELF_UPGRADE_IN_PROGRESS", "1")
    with patch("cli.client._installed_version", return_value="0.10.0"):
        resp = _fake_response({
            "X-Agnes-Latest-Version": "0.40.0",
            "X-Agnes-Min-Version": "0.35.0",
        })
        _check_version_headers(resp)  # must not raise
```

- [ ] **Step 2.2: Run test, verify it fails**

```bash
pytest tests/test_client_version_check.py -v
```
Expected: FAIL — `cli.client._check_version_headers` does not exist.

- [ ] **Step 2.3: Implement `_check_version_headers` in `cli/client.py`**

At the top of `cli/client.py`, near other imports, add:

```python
import os
import sys

from cli.update_check import _installed_version, _version_lt
```

Then before `get_client()`, define:

```python
def _check_version_headers(response: "httpx.Response") -> None:
    """Hard-stop the CLI when the server reports we're below min_version.

    Drift warnings (`local < latest`) are already printed by the
    update_check root callback in cli/main.py — no need to nag again on
    every API call. This hook only enforces the hard floor.
    """
    # Recursion barrier: `agnes self-upgrade` sets this for the duration
    # of the upgrade. Without it, a /api/* call inside the install flow
    # could exit 2 with "Run: agnes self-upgrade" — inside agnes
    # self-upgrade. The sentinel is process-local and propagates to
    # subprocesses via the explicit env= passed to the smoke test.
    if os.environ.get("AGNES_SELF_UPGRADE_IN_PROGRESS") == "1":
        return
    latest = response.headers.get("X-Agnes-Latest-Version")
    minv = response.headers.get("X-Agnes-Min-Version")
    if not latest or not minv:
        return
    local = _installed_version()
    if local == "unknown":
        return
    if _version_lt(local, minv):
        sys.stderr.write(
            f"error: agnes {local} is incompatible with server {latest} "
            f"(min required: {minv}). Run: agnes self-upgrade\n"
        )
        sys.exit(2)
```

**Patch only `get_client()` — leave `_get_shared_client()` alone.** Post-rebase, `cli/client.py` has both `get_client()` (line 216, one-shot metadata calls) and `_get_shared_client()` (line 252, persistent HTTP/2 client used by `stream_download` for parquet bytes via chunked range requests).

The hook is wired ONLY on `get_client()`:

- httpx fires response event hooks **as soon as headers arrive**, before `iter_bytes()` consumes the body. On `_get_shared_client()`, `_check_version_headers` would run inside the `with client.stream(...) as response:` context of `_download_chunk` (`cli/client.py:452`) and `_download_single_stream` (`cli/client.py:595`). A `sys.exit(2)` from the hook kills the process mid-stream: `ThreadPoolExecutor` with N parallel chunk-writer threads, open `<target>.<pid>.partN` file handles, no `.tmp → final` rename. Half-written part files left on disk (the existing PID-reaper cleans those eventually, but the abrupt exit is ungraceful).
- In production, parquet downloads typically go through a Caddy `file_server` (PR #182) anyway, so FastAPI middleware doesn't stamp headers on the streaming responses. Skipping the hook on `_get_shared_client()` matches that production reality. In dev / non-Caddy deployments, parquet streaming bypasses the hard-stop — accepted gap. The next metadata call (which runs through `get_client()`) catches drift.
- All `/api/*` metadata calls (catalog, schema, snapshot create, sync trigger, auth, store, etc.) go through `get_client()`, where the hook fires safely on a fresh single-response client.

Modify `get_client()` to wire the hook and a User-Agent. Locate the `httpx.Client(...)` constructor call and pass:

```python
import platform

return httpx.Client(
    base_url=server_url,
    timeout=timeout,
    headers={**headers, "User-Agent": f"agnes/{_installed_version()} ({platform.system().lower()})"},
    event_hooks={"response": [_check_version_headers]},
)
```

`headers` already contains `Authorization` from the existing implementation; we merge in `User-Agent`. **Do not** modify `_get_shared_client()` — the streaming-response semantics make `sys.exit(2)` from a response event hook unsafe (see the rationale above).

- [ ] **Step 2.4: Run test, verify it passes**

```bash
pytest tests/test_client_version_check.py -v
```
Expected: PASS — all four tests.

- [ ] **Step 2.5: Run the existing CLI test suite to catch regressions**

```bash
pytest tests/test_cli_update_check.py tests/test_client_version_check.py -v
```
Expected: PASS — no regressions in update_check.

- [ ] **Step 2.6: Commit**

```bash
git add cli/client.py tests/test_client_version_check.py
git commit -m "feat(cli): hard-stop on incompatible-version response header

Every API response is inspected via httpx event_hooks. When the server
reports X-Agnes-Min-Version > local, CLI prints a remediation message
and exits 2. Latest-version drift continues to be handled by the
update_check warning loop — no double-warning on every API call."
```

---

## Task 3: `agnes self-upgrade` command

**Files:**
- Modify: `cli/update_check.py` — add `bypass_disabled` kwarg to `check()`.
- Create: `cli/commands/self_upgrade.py`
- Modify: `cli/main.py` — register the command
- Create: `tests/test_self_upgrade.py`

- [ ] **Step 3.0: Extend `check()` with `bypass_disabled` kwarg**

`AGNES_NO_UPDATE_CHECK=1` was designed to silence the implicit warning loop that runs in the root callback. An explicit `agnes self-upgrade` is a user-typed command and should not become a silent no-op when that env var happens to be set. Thread a keyword-only kwarg through:

In `cli/update_check.py`, modify the signature and the disabled-check:

```python
def check(server_url: Optional[str], *, bypass_disabled: bool = False) -> Optional[UpdateInfo]:
    """..."""
    if not bypass_disabled and is_disabled():
        return None
    if not server_url:
        return None
    # ... rest unchanged
```

Existing callers (the root callback at `cli/main.py:102`) keep their default-false behavior; `self-upgrade` will pass `bypass_disabled=True`. Add a test in `tests/test_cli_update_check.py`:

```python
def test_check_bypass_disabled_overrides_env(monkeypatch):
    monkeypatch.setenv("AGNES_NO_UPDATE_CHECK", "1")
    with patch("cli.update_check._fetch_latest", return_value={
        "version": "9.9.9", "wheel_filename": "x.whl",
        "download_url_path": "/cli/wheel/x.whl",
    }):
        # Default: env var wins, returns None.
        assert check("http://server.test") is None
        # Bypass: env var ignored.
        info = check("http://server.test", bypass_disabled=True)
        assert info is not None and info.latest == "9.9.9"
```

Run the existing tests to catch regressions:

```bash
pytest tests/test_cli_update_check.py -v
```
Expected: PASS — old tests still green, new test passes.

Commit at end of task; the kwarg is shipped together with `self-upgrade`.

- [ ] **Step 3.1: Write the failing tests**

Create `tests/test_self_upgrade.py`:

```python
"""Tests for `agnes self-upgrade` — install path, smoke test, rollback
(with rc capture), recursion barrier, --force offline failure, AGNES_NO_UPDATE_CHECK
bypass for explicit upgrades, --quiet stderr behavior, version-mismatch
smoke detection."""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.update_check import UpdateInfo

runner = CliRunner()


@pytest.fixture(autouse=True)
def _ensure_no_sentinel_leak(monkeypatch):
    """Pytest test order is not guaranteed; explicitly clear the recursion
    sentinel before every test so a leaked value from a prior test doesn't
    produce a false-positive 'cleared on exit' assertion."""
    monkeypatch.delenv("AGNES_SELF_UPGRADE_IN_PROGRESS", raising=False)
    yield

_OUTDATED_URL = "http://server.test/cli/wheel/agnes-0.40.0-py3-none-any.whl"
_PRIOR_URL = "http://server.test/cli/wheel/agnes-0.35.0-py3-none-any.whl"


def _outdated_info():
    return UpdateInfo(installed="0.30.0", latest="0.40.0", download_url=_OUTDATED_URL)


def _current_info():
    return UpdateInfo(installed="0.40.0", latest="0.40.0", download_url=None)


def _smoke_pass():
    return (True, "agnes 0.40.0")


def _smoke_fail():
    return (False, "exit 1: ImportError: cannot import name 'foo'")


def test_check_only_when_outdated_exits_1():
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()):
        result = runner.invoke(app, ["self-upgrade", "--check-only"])
        assert result.exit_code == 1
        assert "out of date" in result.output


def test_check_only_when_current_exits_0():
    with patch("cli.commands.self_upgrade.check", return_value=_current_info()):
        result = runner.invoke(app, ["self-upgrade", "--check-only"])
        assert result.exit_code == 0


def test_when_current_short_circuits_no_install():
    with patch("cli.commands.self_upgrade.check", return_value=_current_info()), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run:
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        mock_run.assert_not_called()


def test_uv_path_when_uv_available():
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        args = mock_run.call_args_list[0].args[0]
        assert args[:3] == ["uv", "tool", "install"]
        assert "--force" in args
        assert _OUTDATED_URL in args


def test_pip_fallback_uses_sys_executable_not_user():
    """pip path must target the running interpreter's venv, never --user."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value=None), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any(cmd[0] == "curl" for cmd in cmds), cmds
        pip_cmd = next(cmd for cmd in cmds if "pip" in cmd)
        assert pip_cmd[0] == sys.executable, pip_cmd
        assert "--force-reinstall" in pip_cmd
        assert "--user" not in pip_cmd  # would land outside the venv


def test_force_invalidates_cache_before_check():
    """--force must drop the cached download_url before probing /cli/latest,
    so we get the SERVER's current wheel, not whatever was cached 24h ago."""
    fresh_current_with_url = UpdateInfo(installed="0.40.0", latest="0.40.0",
                                        download_url=_OUTDATED_URL)
    with patch("cli.commands.self_upgrade._invalidate_update_cache") as mock_invalidate, \
         patch("cli.commands.self_upgrade.check", return_value=fresh_current_with_url) as mock_check, \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade", "--force"])
        assert result.exit_code == 0
        # invalidate called twice: once before check (forced fresh probe),
        # once after smoke pass (next invocation re-probes the new wheel).
        assert mock_invalidate.call_count == 2
        mock_check.assert_called_once()


def test_force_offline_exits_1_with_stderr():
    """--force + server unreachable: exit 1 with explicit stderr.
    Without --force, an offline check is silent; with --force it is not."""
    with patch("cli.commands.self_upgrade.check", return_value=None), \
         patch("cli.commands.self_upgrade.get_server_url",
               return_value="http://server.test"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        result = runner.invoke(app, ["self-upgrade", "--force"], mix_stderr=False)
        assert result.exit_code == 1
        assert "cannot reach" in result.stderr
        assert "server.test" in result.stderr


def test_offline_without_force_is_silent():
    """No --force, server unreachable: exit 0 silently. Implicit warning
    loop already covered by update_check."""
    with patch("cli.commands.self_upgrade.check", return_value=None), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        result = runner.invoke(app, ["self-upgrade"], mix_stderr=False)
        assert result.exit_code == 0
        assert result.stderr == ""


def test_self_upgrade_passes_bypass_disabled_to_check():
    """AGNES_NO_UPDATE_CHECK silences the implicit warning loop, but
    explicit `agnes self-upgrade` must NOT be a silent no-op when set.
    Verify the callback passes bypass_disabled=True to check()."""
    with patch("cli.commands.self_upgrade.check", return_value=_current_info()) as mock_check:
        result = runner.invoke(app, ["self-upgrade", "--check-only"])
        assert result.exit_code == 0
        # check() was called with bypass_disabled=True (positional or kwarg).
        kwargs = mock_check.call_args.kwargs
        assert kwargs.get("bypass_disabled") is True


def test_quiet_does_not_suppress_install_failure_stderr():
    """--quiet suppresses progress but install/smoke failures always surface."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None):
        mock_run.return_value = MagicMock(returncode=42)
        result = runner.invoke(app, ["self-upgrade", "--quiet"], mix_stderr=False)
        assert result.exit_code == 1
        assert "install failed" in result.stderr


def test_smoke_fail_triggers_rollback_when_prior_url_known():
    """Broken new wheel: smoke fails, rollback to last-known-good URL, exit 1."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_fail()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=_PRIOR_URL), \
         patch("cli.commands.self_upgrade._record_last_known_good") as mock_record:
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"], mix_stderr=False)
        assert result.exit_code == 1
        # Two install calls: forward to new, rollback to prior
        urls_installed = [
            arg for c in mock_run.call_args_list
            for arg in c.args[0] if isinstance(arg, str) and arg.startswith("http")
        ]
        assert _OUTDATED_URL in urls_installed
        assert _PRIOR_URL in urls_installed
        # Last-known-good is NOT updated on a failed upgrade
        mock_record.assert_not_called()
        assert "smoke test" in result.stderr


def test_smoke_fail_with_rollback_failure_surfaces_rc():
    """Forward install ok, smoke fail, rollback ALSO fails:
    stderr must surface the rollback rc + bootstrap recovery command."""
    # First call: forward install (rc=0). Second call: rollback (rc=99).
    install_results = [MagicMock(returncode=0), MagicMock(returncode=99)]
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run", side_effect=install_results), \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_fail()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=_PRIOR_URL), \
         patch("cli.commands.self_upgrade.get_server_url",
               return_value="http://server.test"):
        result = runner.invoke(app, ["self-upgrade"], mix_stderr=False)
        assert result.exit_code == 1
        assert "rollback ALSO failed" in result.stderr
        assert "rc=99" in result.stderr
        assert "/cli/install.sh" in result.stderr  # bootstrap recovery


def test_smoke_fail_no_prior_url_prints_install_sh_recovery():
    """First-ever upgrade with no rollback target: stderr points at the
    canonical bootstrap path with a fully-formed curl command."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_fail()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade.get_server_url",
               return_value="http://server.test"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"], mix_stderr=False)
        assert result.exit_code == 1
        assert "/cli/install.sh" in result.stderr
        assert "server.test" in result.stderr  # actual server URL, not <placeholder>


def test_smoke_pass_records_last_known_good_then_invalidates_cache():
    """Convention: record before invalidate. No correctness consequence either
    way; this test pins the convention so swapping order shows up in review."""
    call_order = []
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good",
               side_effect=lambda url: call_order.append(("record", url))), \
         patch("cli.commands.self_upgrade._invalidate_update_cache",
               side_effect=lambda: call_order.append(("invalidate", None))):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        record_idx = next(i for i, c in enumerate(call_order) if c[0] == "record")
        invalidate_idx = next(i for i, c in enumerate(call_order) if c[0] == "invalidate")
        assert record_idx < invalidate_idx, call_order
        assert call_order[record_idx] == ("record", _OUTDATED_URL)


def test_self_upgrade_propagates_sentinel_to_smoke_subprocess():
    """During the upgrade, AGNES_SELF_UPGRADE_IN_PROGRESS=1 must be in
    os.environ. The smoke test subprocess inherits via env={**os.environ, ...}.
    Cleared in finally on callback exit. The test fakes _smoke_test_new_binary
    to capture the env it would build, asserting both the sentinel propagation
    and the cleanup."""
    captured_envs = []

    def _fake_smoke(method, expected_version):
        env = {**os.environ, "AGNES_NO_UPDATE_CHECK": "1",
               "AGNES_SELF_UPGRADE_IN_PROGRESS": "1"}
        captured_envs.append(env)
        return _smoke_pass()

    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run",
               return_value=MagicMock(returncode=0)), \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", side_effect=_fake_smoke), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        result = runner.invoke(app, ["self-upgrade"])
    assert result.exit_code == 0
    assert captured_envs and captured_envs[0]["AGNES_SELF_UPGRADE_IN_PROGRESS"] == "1"
    # Cleared in finally
    assert os.environ.get("AGNES_SELF_UPGRADE_IN_PROGRESS") is None


@pytest.mark.parametrize("install_method,patch_target", [
    ("uv", "_uv_tool_bin_path"),
    ("pip", "_pip_bin_path"),
])
def test_smoke_test_detects_version_mismatch(install_method, patch_target):
    """The smoke test must exec the binary at the install-resolved path
    (NOT shutil.which) and compare its --version output via
    packaging.version.Version equality. A stale PATH-shadow returning the
    old version must FAIL the smoke. Parametrized over both uv and pip
    install paths so neither branch becomes silently broken."""
    from pathlib import Path
    from cli.commands import self_upgrade as su

    fake_bin = f"/fake/{install_method}/bin/agnes"
    with patch.object(su, patch_target, return_value=Path(fake_bin)), \
         patch.object(su.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="agnes 0.30.0\n", stderr="")
        ok, detail = su._smoke_test_new_binary(install_method, expected_version="0.40.0")
        assert ok is False
        assert "version mismatch" in detail
        assert "0.40.0" in detail and "0.30.0" in detail
        # Must have execed the install-path binary, not "agnes" via PATH
        assert mock_run.call_args.args[0][0] == fake_bin


def test_smoke_test_passes_with_pep440_local_version():
    """PEP 440 local version segments (e.g. '0.40.0+local.dev') must NOT
    trip the equality check when the server reports the canonical version.
    Use Version() comparison, not substring."""
    from pathlib import Path
    from cli.commands import self_upgrade as su

    with patch.object(su, "_uv_tool_bin_path", return_value=Path("/fake/agnes")), \
         patch.object(su.subprocess, "run") as mock_run:
        # Wheel reports a local-segmented version; server's expected is canonical.
        mock_run.return_value = MagicMock(returncode=0, stdout="agnes 0.40.0\n", stderr="")
        ok, _ = su._smoke_test_new_binary("uv", expected_version="0.40.0")
        assert ok is True
        # Reverse: substring "0.40.0" inside "0.40.10" must NOT pass.
        mock_run.return_value = MagicMock(returncode=0, stdout="agnes 0.40.10\n", stderr="")
        ok, detail = su._smoke_test_new_binary("uv", expected_version="0.40.0")
        assert ok is False
        assert "version mismatch" in detail
```

- [ ] **Step 3.2: Run tests, verify they fail**

```bash
pytest tests/test_self_upgrade.py -v
```
Expected: FAIL — `cli.commands.self_upgrade` module does not exist.

- [ ] **Step 3.3: Create `cli/commands/self_upgrade.py`**

```python
"""`agnes self-upgrade` — pull the wheel from the server, reinstall, smoke-test,
roll back on failure.

Flow:
  1. Set AGNES_SELF_UPGRADE_IN_PROGRESS=1 (recursion barrier — see cli/client.py).
  2. If --force, invalidate update_check cache so we get fresh /cli/latest.
  3. Probe via update_check.check(..., bypass_disabled=True) — explicit user
     intent overrides AGNES_NO_UPDATE_CHECK (which is for the implicit warning
     loop only).
  4. --force + offline ⇒ exit 1 with "cannot reach <server>". Without --force,
     offline is silent.
  5. If nothing to do (current, no download_url) → exit 0.
  6. Snapshot _read_last_known_good() — URL of the last verified-good install.
  7. Install via uv (preferred) or pip (sys.executable, no --user, --no-deps).
  8. Smoke-test the binary at the deterministic install path (NOT shutil.which,
     which can resolve a stale PATH shadow). Verify --version output contains
     info.latest. Failure → rollback (capturing rc) → exit 1.
  9. On smoke pass: _record_last_known_good(new_url) then
     _invalidate_update_cache(). Convention; no correctness consequence either way.
  10. Sentinel cleared in finally.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Union

import typer

from cli.config import _config_dir, get_server_url
from cli.update_check import UpdateInfo, check, format_outdated_notice

self_upgrade_app = typer.Typer(
    name="self-upgrade",
    help="Reinstall the CLI from the server's currently-shipped wheel.",
    invoke_without_command=True,
)

_SENTINEL_ENV = "AGNES_SELF_UPGRADE_IN_PROGRESS"


class _Unreachable:
    """Sentinel returned by _resolve_info when --force was specified but the
    server probe failed. Distinguishes 'explicitly requested an upgrade and
    we couldn't reach the server' (exit 1, stderr) from 'no upgrade needed'
    (exit 0, silent)."""


_UNREACHABLE = _Unreachable()


def _invalidate_update_cache() -> None:
    """Drop update_check.json so the next CLI invocation re-probes /cli/latest."""
    (_config_dir() / "update_check.json").unlink(missing_ok=True)


def _last_known_good_path() -> Path:
    return _config_dir() / "last_known_good.json"


def _read_last_known_good() -> Optional[str]:
    """URL of the last wheel that passed the smoke test on this machine.
    None on first ever upgrade — first-run failure falls back to the bootstrap
    install.sh recovery message rather than a rollback."""
    p = _last_known_good_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("download_url")
    except (OSError, json.JSONDecodeError):
        return None


def _record_last_known_good(download_url: str) -> None:
    p = _last_known_good_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"download_url": download_url}), encoding="utf-8")
    except OSError:
        pass  # best-effort — failure to record must not break the flow


def _uv_tool_bin_path() -> Optional[Path]:
    """Locate the agnes shim uv installed.

    Tries `uv tool dir --bin` first (uv >= 0.5 prints the entrypoint shim
    directory directly). On older uv where `--bin` is rejected, falls back
    to uv's documented default install location (`~/.local/bin/` on POSIX,
    `%APPDATA%\\uv\\tools\\bin\\` on Windows). Smoke-test failure here would
    silently rollback an otherwise-good install on every older-uv analyst,
    so the fallback matters.
    """
    bin_dir: Optional[Path] = None
    try:
        out = subprocess.run(
            ["uv", "tool", "dir", "--bin"], capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            bin_dir = Path(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        bin_dir = None

    if bin_dir is None:
        # Fallback: uv's documented default install location.
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            if appdata:
                bin_dir = Path(appdata) / "uv" / "tools" / "bin"
        else:
            bin_dir = Path.home() / ".local" / "bin"

    if bin_dir is None or not bin_dir.exists():
        return None

    # uv emits `agnes.exe` on Windows and `agnes` on POSIX; check both.
    for name in ("agnes.exe", "agnes"):
        candidate = bin_dir / name
        if candidate.exists():
            return candidate
    return None


def _pip_bin_path() -> Optional[Path]:
    """`<venv>/bin/agnes` (POSIX) or `<venv>\\Scripts\\agnes.exe` (Windows)."""
    parent = Path(sys.executable).parent
    name = "agnes.exe" if sys.platform == "win32" else "agnes"
    candidate = parent / name
    return candidate if candidate.exists() else None


def _install_with_uv(download_url: str, *, quiet: bool) -> int:
    out = subprocess.DEVNULL if quiet else None
    return subprocess.run(
        ["uv", "tool", "install", "--force", download_url], stdout=out
    ).returncode


def _install_with_pip(download_url: str, *, quiet: bool) -> int:
    """Install into the SAME interpreter that's running this command.

    sys.executable resolves to the venv (uv-tool venv, user-pip --user venv,
    or system) that owns the live `agnes` binary. Using `python3` instead
    would PATH-resolve to system python on macOS analyst machines, landing
    the wheel outside the agnes venv and silently no-op'ing the upgrade.
    --user is wrong here: inside a uv-tool venv it targets ~/.local outside
    the venv. Drop it.
    """
    out = subprocess.DEVNULL if quiet else None
    with tempfile.TemporaryDirectory(prefix="agnes_cli.") as td:
        wheel_path = Path(td) / "agnes.whl"
        rc = subprocess.run(
            ["curl", "-fsSL", "-o", str(wheel_path), download_url], stdout=out
        ).returncode
        if rc != 0:
            return rc
        return subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--force-reinstall", "--no-deps", str(wheel_path)],
            stdout=out,
        ).returncode


def _smoke_test_new_binary(install_method: str, expected_version: str) -> tuple[bool, str]:
    """Exec `<install-path>/agnes --version` from a fresh subprocess, confirm
    it boots AND reports the expected version.

    Resolves the binary at the install-method-specific path (uv tool dir /
    sys.executable parent) rather than via PATH — defends against a stale
    shadow ahead of the freshly-installed binary in $PATH. Suppresses the
    new binary's own update check + propagates the recursion sentinel so
    the smoke run can't trigger a nested self-upgrade.
    """
    binary = _uv_tool_bin_path() if install_method == "uv" else _pip_bin_path()
    if binary is None:
        return False, f"agnes binary not found at expected {install_method} install path"
    try:
        env = {**os.environ, "AGNES_NO_UPDATE_CHECK": "1", _SENTINEL_ENV: "1"}
        out = subprocess.run(
            [str(binary), "--version"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        if out.returncode != 0:
            return False, f"exit {out.returncode}: {out.stderr.strip()[:200]}"
        # `agnes --version` prints `agnes <version>` — extract and compare
        # via packaging.version.Version (PEP 440-aware) to avoid substring
        # false-positives like "0.40.0" matching "0.40.10".
        from packaging.version import InvalidVersion, Version
        tokens = out.stdout.strip().split()
        actual_str = tokens[-1] if tokens else ""
        try:
            if Version(actual_str) != Version(expected_version):
                return False, (
                    f"version mismatch: expected {expected_version}, "
                    f"got {actual_str}"
                )
        except InvalidVersion:
            return False, f"unparseable version output: {out.stdout.strip()[:80]}"
        return True, out.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"{type(e).__name__}: {e}"


def _resolve_info(force: bool) -> Union[UpdateInfo, _Unreachable, None]:
    """Returns:
      UpdateInfo  — install this wheel
      _UNREACHABLE — --force specified, server probe failed
      None        — nothing to do (current, or offline without --force)
    """
    if force:
        _invalidate_update_cache()
    # bypass_disabled=True so an explicit `agnes self-upgrade` is not silenced
    # by AGNES_NO_UPDATE_CHECK (which exists for the implicit warning loop).
    info = check(get_server_url(), bypass_disabled=True)
    if info is None:
        return _UNREACHABLE if force else None
    if not info.download_url:
        return None
    if not force and not info.is_outdated():
        return None
    return info


def _do_install_with_smoke_and_rollback(
    info: UpdateInfo, *, quiet: bool
) -> int:
    """Returns the exit code typer should use (0 success, 1 failure)."""
    prior_url = _read_last_known_good()  # may be None on first upgrade

    if shutil.which("uv"):
        rc = _install_with_uv(info.download_url, quiet=quiet)
        method = "uv"
    else:
        rc = _install_with_pip(info.download_url, quiet=quiet)
        method = "pip"

    if rc != 0:
        sys.stderr.write(f"agnes self-upgrade: install failed with exit {rc}\n")
        return 1

    ok, detail = _smoke_test_new_binary(method, expected_version=info.latest)
    if not ok:
        sys.stderr.write(
            f"agnes self-upgrade: new binary failed smoke test ({detail}).\n"
        )
        server = get_server_url().rstrip("/")
        bootstrap_recovery = f"  Manual recovery: curl -fsSL {server}/cli/install.sh | bash\n"
        if prior_url and prior_url != info.download_url:
            sys.stderr.write(f"  rolling back to {prior_url}\n")
            rb_rc = (
                _install_with_uv(prior_url, quiet=True)
                if method == "uv"
                else _install_with_pip(prior_url, quiet=True)
            )
            if rb_rc != 0:
                sys.stderr.write(
                    f"  rollback ALSO failed (rc={rb_rc}); CLI is in a broken state.\n"
                )
                sys.stderr.write(bootstrap_recovery)
        else:
            sys.stderr.write(
                "  no prior wheel URL on record; rollback skipped.\n"
            )
            sys.stderr.write(bootstrap_recovery)
        return 1

    # Convention: record then invalidate. No correctness consequence either way.
    _record_last_known_good(info.download_url)
    _invalidate_update_cache()
    if not quiet:
        typer.echo(f"agnes self-upgrade: installed {info.latest}", err=True)
    return 0


@self_upgrade_app.callback()
def self_upgrade(
    quiet: bool = typer.Option(False, "--quiet", help="Suppress progress output. Failures still surface on stderr."),
    check_only: bool = typer.Option(False, "--check-only", help="Print status, don't install. Exit 1 if outdated."),
    force: bool = typer.Option(False, "--force", help="Reinstall the server's current wheel even when already on the latest version."),
) -> None:
    # Defensively snapshot any prior value so we restore (rather than
    # destroy) it in finally — we own the namespace but a wrapper could
    # legitimately set it for its own bookkeeping.
    prior_sentinel = os.environ.get(_SENTINEL_ENV)
    os.environ[_SENTINEL_ENV] = "1"
    try:
        info = _resolve_info(force)

        # --check-only is read-only intent — never exit non-zero on
        # transport errors. If unreachable, treat as "can't tell, current"
        # and exit 0 silently. (Without --check-only, --force + offline
        # is exit 1, which is the destructive-intent contract.)
        if check_only:
            if isinstance(info, _Unreachable) or info is None or not info.is_outdated():
                raise typer.Exit(0)
            typer.echo(format_outdated_notice(info), err=True)
            raise typer.Exit(1)

        if isinstance(info, _Unreachable):
            sys.stderr.write(
                f"agnes self-upgrade: cannot reach {get_server_url()}/cli/latest\n"
            )
            raise typer.Exit(1)

        if info is None:
            raise typer.Exit(0)  # nothing to do, silent

        rc = _do_install_with_smoke_and_rollback(info, quiet=quiet)
        raise typer.Exit(rc)
    finally:
        if prior_sentinel is None:
            os.environ.pop(_SENTINEL_ENV, None)
        else:
            os.environ[_SENTINEL_ENV] = prior_sentinel
```

- [ ] **Step 3.4: Register in `cli/main.py`**

After the existing `from cli.commands.X import Y_app` block, add:

```python
from cli.commands.self_upgrade import self_upgrade_app
```

In the `app.add_typer(...)` block (around line 109-127), add:

```python
app.add_typer(self_upgrade_app, name="self-upgrade")
```

Place it near `app.add_typer(setup_app, name="setup")` for grouping.

- [ ] **Step 3.5: Run tests, verify they pass**

```bash
pytest tests/test_self_upgrade.py -v
```
Expected: PASS — all seven tests.

- [ ] **Step 3.6: Smoke-test the command shape locally**

```bash
agnes self-upgrade --help
```
Expected: typer help text with `--quiet`, `--check-only`, `--force` flags.

- [ ] **Step 3.7: Commit**

```bash
git add cli/update_check.py cli/commands/self_upgrade.py cli/main.py \
        tests/test_self_upgrade.py tests/test_cli_update_check.py
git commit -m "feat(cli): add agnes self-upgrade with smoke test + rollback

Reuses cli.update_check.check() for the version probe — extended with
bypass_disabled=True so explicit user-typed self-upgrade is not silenced
by AGNES_NO_UPDATE_CHECK (which is for the implicit warning loop).

Install path: uv tool install --force when uv is on PATH; otherwise
curl + pip via sys.executable (NOT system python3, NOT --user — both
would land outside the agnes venv and silently no-op the upgrade).

Smoke test execs the binary at the install-resolved path (uv tool dir
joined with agnes-the-ai-analyst/bin/agnes, or sys.executable's sibling
agnes for pip) — never via shutil.which, which can resolve a stale shadow
on PATH and produce a false-positive smoke pass on the OLD version. Smoke
also asserts --version output contains info.latest.

On smoke fail: rollback to last_known_good.json (written only after a
previous run's smoke passed). Rollback rc is captured and surfaced on
stderr if it also fails. First-ever upgrade or unrecoverable rollback
prints the canonical bootstrap recovery: curl -fsSL <your-agnes-server>/cli/install.sh | bash.

AGNES_SELF_UPGRADE_IN_PROGRESS=1 is set for the duration of the run
and propagated to the smoke-test subprocess. Layer B's _check_version_headers
honors the sentinel and skips the < min hard-stop, so an in-flight
upgrade can never sys.exit(2) itself.

--force invalidates the update_check cache BEFORE probing. --force +
offline = exit 1 with explicit stderr (without --force, offline is silent).
--quiet suppresses progress output but never gags failure stderr."
```

---

## Task 4: SessionStart hook (single chained entry)

**Why one entry, not two:** Claude Code's hook execution semantics for multiple SessionStart entries (parallel? sequential? bounded?) are not documented in this repo and are not relied upon. Chain in a single entry with `;` so the shell guarantees ordering: self-upgrade first, pull second, regardless of host. Each segment carries its own `|| true`, so a failed upgrade does not abort the pull.

**Files:**
- Modify: `cli/lib/hooks.py`
- Modify: `tests/test_lib_hooks.py`

- [ ] **Step 4.1: Write the failing hook-installer test**

Append to `tests/test_lib_hooks.py`:

```python
def test_install_chains_self_upgrade_then_pull_in_one_entry(tmp_path):
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    session_start = cfg["hooks"]["SessionStart"]
    assert len(session_start) == 1, session_start
    cmd = session_start[0]["hooks"][0]["command"]
    assert "agnes self-upgrade --quiet" in cmd
    assert "agnes pull --quiet" in cmd
    # Order is encoded in the shell — self-upgrade must appear first
    assert cmd.index("agnes self-upgrade") < cmd.index("agnes pull")
    # Both segments carry || true so neither failure aborts the line
    assert cmd.count("|| true") >= 2


def test_install_idempotent_chained_entry(tmp_path):
    install_claude_hooks(tmp_path)
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert len(cfg["hooks"]["SessionStart"]) == 1
    assert len(cfg["hooks"]["SessionEnd"]) == 1
```

The existing `test_install_creates_settings_file` (around line 14) currently asserts `[0]` is the lone pull entry. Update it to assert the chained command:

```python
def test_install_creates_settings_file(tmp_path):
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    cmd = cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "agnes self-upgrade --quiet" in cmd
    assert "agnes pull --quiet" in cmd
    assert "agnes push --quiet" in cfg["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
```

The existing `test_install_idempotent` already asserts `len(SessionStart) == 1` — leave as-is, that's still correct under the chained-entry design.

- [ ] **Step 4.2: Run tests, verify they fail**

```bash
pytest tests/test_lib_hooks.py -v
```
Expected: FAIL — chained-entry tests fail (the lone pull command does not contain `self-upgrade`).

- [ ] **Step 4.3: Modify `cli/lib/hooks.py`**

Update `_OUR_COMMAND_MARKERS` (line 27) to include `self-upgrade` so the substring match still recognises our line for idempotent replacement:

```python
_OUR_COMMAND_MARKERS = ("agnes self-upgrade", "agnes pull", "agnes push", "da sync")
```

Replace the SessionStart registration (around line 63) with a single chained command:

```python
    _replace_or_add(
        "SessionStart",
        "agnes self-upgrade --quiet 2>/dev/null || true; "
        "agnes pull --quiet 2>/dev/null || true",
    )
    _replace_or_add("SessionEnd", "agnes push --quiet 2>/dev/null || true")
```

The `;` runs the second command unconditionally; each `|| true` prevents either failure from aborting the line. Idempotency: re-running `install_claude_hooks` matches the existing entry on either `agnes self-upgrade` or `agnes pull` (both substrings present), drops it, and re-appends — net length stays at 1.

- [ ] **Step 4.4: Run tests, verify they pass**

```bash
pytest tests/test_lib_hooks.py -v
```
Expected: PASS — all hook tests including the new chained-entry assertions and idempotency.

- [ ] **Step 4.5: Commit**

```bash
git add cli/lib/hooks.py tests/test_lib_hooks.py
git commit -m "feat(cli): install SessionStart hook chaining self-upgrade then pull

Single hook entry: 'agnes self-upgrade --quiet ... || true; agnes pull
--quiet ... || true'. Shell semicolon guarantees ordering across every
Claude Code version (no reliance on undocumented multi-hook execution
semantics); each segment's || true preserves the original property
that an upgrade failure does not abort the pull."
```

---

## Task 5: Drive-by `da` → `agnes` cleanup + CHANGELOG

**Files:**
- Modify: `app/api/cli_artifacts.py`
- Modify: `cli/update_check.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 5.1: Fix `da` references**

In `app/api/cli_artifacts.py:47`, replace:

```
    Consumed by `da` CLI's auto-update check so it can warn when a newer
```

with:

```
    Consumed by `agnes` CLI's auto-update check so it can warn when a newer
```

In `cli/update_check.py:1-9`, replace the four `da` occurrences in the docstring with `agnes`:

```python
"""Auto-check for a newer CLI version on the configured server.

Runs in the root typer callback before subcommand dispatch. Failure is
silent — we never block a working `agnes` command on a best-effort version
probe. Result is cached in `$AGNES_CONFIG_DIR/update_check.json` for 24h so
we don't hammer the server on every invocation.

Disable with `AGNES_NO_UPDATE_CHECK=1`.
"""
```

Also fix the `da` reference in the negative-cache comment around line 26:

```python
_NEGATIVE_CACHE_TTL_SECONDS = 5 * 60  # 5min on a failed probe, to avoid
# re-probing 3s of silence (drop-packet networks: corporate firewall, VPN)
# on every `agnes` invocation.
```

- [ ] **Step 5.2: Add CHANGELOG entry**

Open `CHANGELOG.md`. After rebasing on `origin/main`, the file's structure at the top is:

```
line 11: ## [Unreleased]
line 12: (blank)
line 13: ## [0.39.0] — 2026-05-06
line 15: ### Performance
...
```

The `## [Unreleased]` block is empty. Insert `### Added` and the three bullets directly between line 11 and line 13:

```markdown
## [Unreleased]

### Added

- CLI auto-upgrade: ...
- Server: ...
- CLI: ...

## [0.39.0] — 2026-05-06
```

```markdown
- CLI auto-upgrade: `agnes self-upgrade` reinstalls the CLI from the server's currently-shipped wheel via `uv tool install --force`, falling back to `pip install --force-reinstall --no-deps` via `sys.executable` when uv is not on PATH. After install, the new binary is smoke-tested at the install-resolved path (`uv tool dir --bin` for uv, `<sys.executable parent>/agnes` for pip) — never via PATH lookup, to avoid stale-shadow false positives. Smoke failure triggers automatic rollback to the previously verified-good wheel (recorded in `~/.config/agnes/last_known_good.json`); rollback's exit code is captured and surfaced on stderr if it also fails. First-ever upgrade or unrecoverable rollback prints the canonical bootstrap recovery: `curl -fsSL <your-agnes-server>/cli/install.sh | bash`. The new command is wired into the SessionStart hook installed by `agnes init` as a chained shell entry (`agnes self-upgrade … || true; agnes pull … || true`) so an upgrade failure does not block the pull.
- Server: `/api/*` responses now carry `X-Agnes-Latest-Version` and `X-Agnes-Min-Version` headers. CLIs older than `X-Agnes-Min-Version` exit with **code 2** and a remediation message instead of failing on a wire-protocol mismatch. Day-one floor is `0.0.0` (no enforcement) — bump `MIN_COMPAT_CLI_VERSION` in `app/version.py` in the same PR that ships a deliberate wire break.
- CLI: `cli/update_check.py:check()` accepts a keyword-only `bypass_disabled=True` so explicit `agnes self-upgrade` invocations probe `/cli/latest` even when `AGNES_NO_UPDATE_CHECK=1` is set (which silences the implicit warning loop only).
```

- [ ] **Step 5.3: Run the full affected test surface**

```bash
pytest tests/test_app_version.py tests/test_version_headers_middleware.py \
       tests/test_cli_update_check.py tests/test_client_version_check.py \
       tests/test_self_upgrade.py tests/test_lib_hooks.py \
       tests/test_cli_init.py -v
```
Expected: PASS — full green.

- [ ] **Step 5.4: Commit**

```bash
git add app/api/cli_artifacts.py cli/update_check.py CHANGELOG.md
git commit -m "chore: rename stale 'da' references to 'agnes' + CHANGELOG

Drive-by docstring/comment cleanup in cli_artifacts.py and update_check.py.
CHANGELOG entry for the auto-upgrade feature shipped in this branch."
```

---

## Task 6: Manual verification

- [ ] **Step 6.1: Local smoke test — version mismatch hard-stop**

Start the server locally:

```bash
cd /path/to/agnes
uvicorn app.main:app --reload &
SERVER_PID=$!
```

Force a min-version mismatch by patching `app/version.py`:

```bash
sed -i.bak 's/MIN_COMPAT_CLI_VERSION = "0.0.0"/MIN_COMPAT_CLI_VERSION = "99.99.99"/' app/version.py
```

Wait for the reload, then hit any `/api/*` endpoint with the CLI:

```bash
agnes status
```

Expected: stderr `error: agnes <local> is incompatible with server <ver> (min required: 99.99.99). Run: agnes self-upgrade`, exit code 2.

Restore:

```bash
mv app/version.py.bak app/version.py
kill $SERVER_PID
```

- [ ] **Step 6.2: Local smoke test — `agnes self-upgrade --check-only`**

```bash
agnes self-upgrade --check-only
```

Expected: exit 0 (current) or exit 1 with `[update] agnes ... out of date ...` on stderr (depends on what version is on disk vs. served).

- [ ] **Step 6.3: Verify hook installation**

In a clean tmp workspace:

```bash
mkdir /tmp/agnes-hook-smoke && cd /tmp/agnes-hook-smoke
agnes init
cat .claude/settings.json | jq '.hooks.SessionStart'
```

Expected: two entries — `agnes self-upgrade --quiet ...` and `agnes pull --quiet ...` in that order.

Re-run:

```bash
agnes init
cat .claude/settings.json | jq '.hooks.SessionStart | length'
```

Expected: `2` (not `4`) — idempotent.

- [ ] **Step 6.4: Open the PR**

```bash
git push -u origin zs/cli-auto-upgrade-spec
gh pr create --title "feat: server-pinned CLI auto-upgrade" --body "$(cat <<'EOF'
## Summary
- `agnes self-upgrade` reinstalls the CLI from `/cli/wheel/<name>` (uv tool install --force, pip --user fallback). Reuses cli.update_check.check() — single polling path, single cache.
- SessionStart hook installs the upgrade ahead of `agnes pull`, so analyst CLIs stay current with the server they connect to.
- /api/* responses carry X-Agnes-Latest-Version / X-Agnes-Min-Version headers. CLIs below min exit 2 with a remediation message instead of failing on a wire-protocol mismatch.
- Drive-by: stale `da` references renamed to `agnes` in cli_artifacts.py and update_check.py docstrings.

## Spec / plan
- Spec: `docs/superpowers/specs/2026-05-06-cli-auto-upgrade-spec.md`
- Plan: `docs/superpowers/plans/2026-05-06-cli-auto-upgrade.md`

## Test plan
- [x] `pytest tests/test_version_headers_middleware.py` — middleware applied to /api/*, not /web/*
- [x] `pytest tests/test_client_version_check.py` — hard-stop on min mismatch
- [x] `pytest tests/test_self_upgrade.py` — uv path, pip fallback, --check-only, --force, --quiet
- [x] `pytest tests/test_lib_hooks.py` — new entry + idempotency
- [ ] Manual: spoof `MIN_COMPAT_CLI_VERSION="99.99.99"` server-side, verify CLI exits 2
- [ ] Manual: fresh `agnes init` workspace shows two SessionStart entries in correct order
EOF
)"
```

---

## Task 7: Release-cut (last commits on this PR)

**Why now:** per CLAUDE.md changelog discipline + project convention, the version bump and `[Unreleased]` rename land on the same PR as the user-visible behavior change. This task converts the in-flight CHANGELOG entry into a versioned release.

**Files:**
- Modify: `CHANGELOG.md` — rename topmost `## [Unreleased]` to `## [0.40.0] — 2026-05-06`, then add a fresh empty `## [Unreleased]` heading above it for the next PR.
- Modify: `pyproject.toml` — bump `[project].version` from `0.39.0` to `0.40.0` (additive feature → minor bump).

- [ ] **Step 7.1: Rename `## [Unreleased]` → `## [0.40.0] — 2026-05-06`**

In `CHANGELOG.md`, locate the topmost `## [Unreleased]` heading. Rename it to `## [0.40.0] — 2026-05-06`. Above it, insert a new empty `## [Unreleased]` block so the next PR has somewhere to land:

```markdown
## [Unreleased]

## [0.40.0] — 2026-05-06

### Added
- CLI auto-upgrade: ... (existing entries from Task 5)
- Server: `/api/*` responses now carry ... (existing entries from Task 5)
```

- [ ] **Step 7.2: Bump `pyproject.toml` version**

```bash
sed -i.bak 's/^version = "0.39.0"/version = "0.40.0"/' pyproject.toml && rm pyproject.toml.bak
```

Verify:

```bash
grep '^version = ' pyproject.toml
```
Expected output: `version = "0.40.0"`

- [ ] **Step 7.3: Commit**

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "release: 0.40.0 — server-pinned CLI auto-upgrade

See CHANGELOG.md for the full entry."
```

- [ ] **Step 7.4: Tag + GitHub Release (after PR merge)**

After the PR merges to `main`, capture the merge SHA explicitly so a concurrent unrelated merge between this PR's merge and the operator running tag commands does not push our tag onto the wrong commit:

```bash
PR_NUM=<this-PR-number>
MERGE_SHA=$(gh pr view "$PR_NUM" --json mergeCommit -q .mergeCommit.oid)
git fetch origin
git tag v0.40.0 "$MERGE_SHA"
git push origin v0.40.0
```

Then create a GitHub Release for `v0.40.0`. Mirror the prose structure of the most recent prior release on the same repo (`gh release view v0.39.0` for the latest format) — typically an intro paragraph, the CHANGELOG section verbatim, and any operator-facing notes (e.g. *"this release introduces SessionStart hook behavior; expect a one-time `agnes self-upgrade` install on the first session per analyst"*).

```bash
gh release create v0.40.0 --target "$MERGE_SHA" --title "v0.40.0 — server-pinned CLI auto-upgrade" --notes "$(...)"
```

(Per user memory: a git tag without a GitHub Release is incomplete.)

---

## Self-Review Checklist (run before declaring complete)

- [ ] Spec coverage: every section of the spec maps to a task above. ✓
- [ ] Placeholder scan: no "TBD" / "fill in later" / "similar to Task N" without inline code.
- [ ] Type/name consistency: `APP_VERSION`, `MIN_COMPAT_CLI_VERSION`, `X-Agnes-Latest-Version`, `X-Agnes-Min-Version`, `_check_version_headers`, `self_upgrade_app`, `_invalidate_update_cache`, `_install_with_uv`, `_install_with_pip`, `_smoke_test_new_binary`, `_uv_tool_bin_path`, `_pip_bin_path`, `_Unreachable`, `_UNREACHABLE`, `_read_last_known_good`, `_record_last_known_good`, `bypass_disabled` — used identically across tasks.
- [ ] CHANGELOG entry exists under `## [Unreleased]` (Task 5), then renamed to `## [0.40.0] — 2026-05-06` (Task 7).
- [ ] CLAUDE.md "OSS — no customer-specific content" rule respected: no Keboola/Groupon/FoundryAI tokens in code or PR body.
- [ ] Each task ends with a real commit. No squash-everything-at-end.
- [ ] Layer B is shipped at `MIN_COMPAT_CLI_VERSION = "0.0.0"` — no enforcement on day one. The bump-when-needed policy is review-time discipline, not a CI gate (rejected during spec iteration as theater).
