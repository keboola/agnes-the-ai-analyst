# CLI Auto-Upgrade — Server-Pinned Version

> **Status:** spec / design. Convert to an implementation plan in `docs/superpowers/plans/` once reviewed.

**Goal:** Keep an analyst's locally-installed `agnes` CLI in sync with the server it talks to. The server is the single source of truth for "what version should be running"; the CLI never asks PyPI, only the server.

**Why now:** today an analyst installs once via `uv tool install $SERVER/cli/wheel/<name>` and drifts arbitrarily. The CLI already prints a *warning* when out of date but never upgrades itself, and there's no hard-stop when a wire-protocol break ships — drifted clients fail with cryptic errors instead of being told to upgrade.

**Non-goal:** distributing the CLI through PyPI, GitHub releases, or any out-of-band channel. The wheel lives next to the server (`/app/dist/*.whl`) and is served by `app/api/cli_artifacts.py`.

---

## What already exists

The first half of this design **is already shipped**, just incomplete:

- **`GET /cli/latest`** (`app/api/cli_artifacts.py:42`) → `{version, wheel_filename, download_url_path}`. Public, no auth.
- **`GET /cli/wheel/{name}`** + `/cli/download` + `/cli/install.sh` for distribution.
- **`cli/update_check.py`** — polls `/cli/latest` on every CLI invocation from `cli/main.py:99-104`, caches result for 24h (positive) / 5min (negative), prints a stderr warning with a copy-paste `uv tool install --force <url>` command. Opt-out: `AGNES_NO_UPDATE_CHECK=1`.
- **`cli/client.py:216 get_client()`** — the shared `httpx.Client` factory. Single chokepoint for response-header inspection.
- **Hook installer** at `cli/lib/hooks.py:install_claude_hooks` writes:
  - `SessionStart` → `agnes pull --quiet 2>/dev/null || true`
  - `SessionEnd` → `agnes push --quiet 2>/dev/null || true`

What's missing:

1. The CLI prints a copy-paste command but never **executes** the upgrade.
2. No `min_version` floor — drift is unbounded; a wire break gives a cryptic 500 instead of a clear "you're too old, upgrade".
3. No SessionStart hook for proactive upgrade — analyst must notice the warning, copy, paste, run.
4. The server-side comment on `/cli/latest` (`app/api/cli_artifacts.py:47`) and the docstring in `cli/update_check.py` still reference the old `da` binary name; cleanup while we're in there.

---

## Design

Two layers, complementary, with different latencies and failure modes.

### Layer A — proactive auto-upgrade (SessionStart hook + new CLI command)

`agnes init` writes a **single** SessionStart hook entry that chains self-upgrade and pull with `;` so ordering is guaranteed by the shell, not by undocumented Claude Code hook-execution semantics:

```
SessionStart → agnes self-upgrade --quiet 2>/dev/null || true; agnes pull --quiet 2>/dev/null || true
SessionEnd   → agnes push --quiet 2>/dev/null || true
```

The `;` runs both unconditionally; each `|| true` keeps a single failure from aborting the line. We lose nothing the design relied on (the *"upgrade fail does not block pull"* property is preserved by the second `|| true`), and we gain an ordering guarantee that holds across every Claude Code version.

`agnes self-upgrade [--quiet] [--check-only] [--force]`:

1. Set `AGNES_SELF_UPGRADE_IN_PROGRESS=1` in `os.environ` for the duration of the call. Layer B's header check reads this sentinel and *skips* the hard-stop while we're upgrading — without this, a later refactor that has `self-upgrade` calling `get_client()` (e.g. for auth) would loop: hit `< min`, exit 2 with *"Run: agnes self-upgrade"* — inside `agnes self-upgrade`. Sentinel propagates to subprocesses via the explicit `env=` we pass to the smoke test.
2. If `--force`, **invalidate** the `update_check.json` cache *before* probing, so we always pick up the server's current `download_url`.
3. Reuse `cli.update_check.check(server_url)` — same `/cli/latest` call, same cache, same version comparison. No second polling path.
4. If `info is None` (disabled / no server / unknown local version) or `(not force and not info.is_outdated())` → exit 0.
5. `--check-only` → print `format_outdated_notice(info)`, exit 1 if outdated, 0 if current.
6. Otherwise: snapshot `prior_url = _read_last_known_good()` (the URL of the version we last successfully smoke-tested into; may be `None` on first upgrade — best-effort rollback only). Then reinstall:
   - `uv` available (`shutil.which("uv")`) → `uv tool install --force "<download_url>"`
   - else → download wheel to `mktemp -d` (curl), then `[sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", <wheel>]`. **Crucially** uses `sys.executable` (the running CLI's interpreter) rather than `python3` (PATH-resolved system Python), and **does not** pass `--user` — both would land the wheel outside the uv-tool venv that owns the `agnes` binary, silently no-op'ing the upgrade.
7. **Smoke-test the new binary** before declaring success — but not via `shutil.which("agnes")`. PATH may shadow the just-installed binary with a stale `/usr/local/bin/agnes` from an old `pip install --user` or Homebrew shim, in which case `--version` would print the *old* version and report success. Instead, locate the binary deterministically:
   - **uv path** → call `uv tool dir --bin` (one subprocess; uv's `--bin` flag returns the directory containing entrypoint shims, working transparently across POSIX/Windows). Look for `agnes` then `agnes.exe` in that directory.
   - **pip path** → `<sys.executable parent>/agnes` (POSIX) or `<sys.executable parent>/agnes.exe` (Windows) — the sibling of the running interpreter, which is the venv pip just rewrote.
   Then `subprocess.run([str(binary), "--version"], env={**os.environ, "AGNES_NO_UPDATE_CHECK": "1", "AGNES_SELF_UPGRADE_IN_PROGRESS": "1"}, timeout=10, capture_output=True)`. Smoke passes when returncode is 0 **and** the trailing token of stdout parses to a `packaging.version.Version` equal to `info.latest` — equality on `Version()` (not substring), so `0.40.0` does not falsely match `0.40.10` and PEP 440 local segments are handled.
8. On smoke fail: if `prior_url` is set and ≠ `info.download_url`, attempt a single rollback install of `prior_url` via the same uv/pip path. **Capture the rollback's return code** — if it's non-zero, the CLI is in a broken state, surface this on stderr alongside the bootstrap-recovery command. If `prior_url` is `None` (first-ever upgrade) or rollback also fails, stderr prints `Run: curl -fsSL <server>/cli/install.sh | bash` — the canonical bootstrap path that doesn't depend on local state. Either way `raise typer.Exit(1)`.
9. On smoke pass: `_record_last_known_good(info.download_url)` (writes `~/.config/agnes/last_known_good.json` — separate from `update_check.json`, updated only after a verified-good install) then `_invalidate_update_cache()`. Convention; no correctness consequence either way.
10. `--quiet` suppresses progress output; **stderr always passes through on install / smoke / rollback failures** — `--quiet` is for routine success runs (the SessionStart hook), not a gag on errors.
11. **`--force` + offline.** `--force` invalidates the cache before probing `/cli/latest`. If the probe fails (network down), `--force` raises `typer.Exit(1)` with `cannot reach <server>/cli/latest` on stderr — explicit destructive intent deserves explicit feedback. Without `--force`, an offline probe is silent (the implicit warning loop's contract).
12. **`--check-only` is read-only intent — exit 0 on transport errors.** Even with `--force`, when the probe is unreachable under `--check-only`, the command exits 0 silently rather than surfacing the error: `--check-only` should never produce a non-zero exit unless the CLI is *known* outdated. (`--force` semantics still apply to the actual install path; pairing `--check-only --force` is well-defined: it invalidates the cache, fresh-probes, prints status, never installs.)
13. **`AGNES_NO_UPDATE_CHECK=1`** silences the implicit warning loop only. Explicit `agnes self-upgrade` calls `check(server_url, bypass_disabled=True)` so the env var does not turn a user-typed upgrade command into a silent no-op.

**Platform support:** smoke test branches on `sys.platform == "win32"` for the `.exe` suffix; the rest of the flow is platform-neutral via uv. Windows is supported on a best-effort basis (analyst laptops are predominantly macOS/Linux).

Honors the existing `AGNES_NO_UPDATE_CHECK=1` opt-out — same flag, same intent. No new opt-out env var.

**Latency:** runs once at session start, blocks pull by ~3-10s on upgrade (install + ~1s smoke test), ~0.2s when in-sync (one cached HTTP roundtrip + early-out).

**Failure modes:** offline / server down → `|| true` → session continues on old version. Install succeeds but new wheel is broken → smoke test catches it, attempts rollback, prints recovery instructions. Layer B catches drift on the next API call.

### Layer B — reactive verification (response headers)

Every `/api/*` response includes two headers (FastAPI middleware):

- `X-Agnes-Latest-Version: 0.40.0` — `APP_VERSION`, same value the install script bakes in.
- `X-Agnes-Min-Version: 0.0.0` — oldest CLI version the server still accepts. Lives in a single Python constant. Bumped manually when a wire-protocol break ships. **Ships at `0.0.0` on day one** so rollout doesn't accidentally lock anyone out — first deliberate gate is the first time this gets bumped.

The shared HTTP client (`cli/client.py:216`) inspects these on every response:

| Local CLI version | Behavior |
|---|---|
| `>= latest` | nothing |
| `>= min` and `< latest` | nothing — Layer A's startup poll already prints the warning; no need to nag again on every API call |
| `< min` | print `error: agnes <local> is incompatible with server <latest> (min required: <min>). Run: agnes self-upgrade` and `sys.exit(2)`. **Operation is not performed.** |

**Recursion barrier:** `_check_version_headers` short-circuits (returns silently, no enforcement) when `os.environ.get("AGNES_SELF_UPGRADE_IN_PROGRESS") == "1"`. Set by Layer A's command for the duration of the upgrade so the in-flight `agnes self-upgrade` cannot be locked out from itself by a `< min` response on any internal `/api/*` call. The sentinel is process-local and propagates to the smoke-test subprocess via explicit `env=`.

The CLI also sends `User-Agent: agnes/<version> (<platform>)` so the server can audit drift in access logs.

**Day-one floor.** `MIN_COMPAT_CLI_VERSION = "0.0.0"` — no enforcement. The constant + middleware + CLI inspection are an opt-in mechanism for the future. When a wire break ships, the engineer bumps the constant in the same PR and adds a `**BREAKING**` CHANGELOG bullet — same review discipline as every other behavior change. No standalone CI gate, no doc, no PR-template checkbox: those would be theater that catches nothing real (an engineer can check a box without bumping a constant). The mechanism stays free-to-use; the policy is one constant change away when someone needs it.

### How the two layers compose

| Scenario | Layer A | Layer B | Outcome |
|---|---|---|---|
| Happy path | upgrade silent (already current) | headers OK | no output |
| Drift caught at session start | upgrades to latest | headers OK after upgrade | brief "installed: 0.40.0" line if not `--quiet` |
| Hook failed (offline at session start), online now | no-op | `< latest` ⇒ silent (warning still printed by `update_check` from main callback) | analyst sees one warning, runs `agnes self-upgrade` manually |
| Server shipped a wire break, analyst is `< min` | hook would have caught it, but maybe the analyst skipped Claude Code | hard-stop with remediation | exit 2, clear message |
| Headless / CI / ad-hoc terminal (no Claude Code) | hook never runs | warning + hard-stop still apply | covered |

---

## Server-side changes

### `app/version.py` (new — single source of truth)

```python
"""Single source of truth for app + CLI compat versions."""
import importlib.metadata

APP_VERSION = importlib.metadata.version("agnes-the-ai-analyst")

# Bump when shipping a wire-protocol break. Older CLIs are blocked at the
# response-header layer with exit 2 + remediation message. Day-one value
# of 0.0.0 means no enforcement — set the floor the first time a deliberate
# break ships.
MIN_COMPAT_CLI_VERSION = "0.0.0"
```

### `app/main.py` — middleware

```python
@app.middleware("http")
async def add_version_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["X-Agnes-Latest-Version"] = APP_VERSION
        response.headers["X-Agnes-Min-Version"] = MIN_COMPAT_CLI_VERSION
    return response
```

Applied only to `/api/` so marketplace / wheel / web UI responses stay clean. Verify CORS `expose_headers` includes these (or `*`).

### `app/api/cli_artifacts.py` — fix stale `da` reference

Drive-by: line 47 still says *"Consumed by `da` CLI's auto-update check"*. Update to `agnes`. No behavior change.

`/cli/latest` itself stays as-is — pure metadata about the wheel on disk. `min_version` is a server-policy concern (per-request), not wheel metadata, so it lives on the headers and not in this payload.

---

## CLI-side changes

### `cli/commands/self_upgrade.py` (new)

Logic per Layer A above. ~80 lines including the install subprocess call. Reuses:

- `cli.update_check.check()` for the version probe (identical to what `cli/main.py:102` already calls)
- `cli.update_check.format_outdated_notice()` for `--check-only` output
- `cli.config.get_server_url()` for the server URL
- `shutil.which("uv")` to choose install path
- `subprocess.run` with `check=True` to surface install failures

Wire into `cli/main.py` near the existing typer registrations.

### `cli/client.py:get_client()` — header inspection

Wrap the returned `httpx.Client` so every response goes through one hook. Cleanest is `httpx.Client(event_hooks={"response": [_check_version_headers]})`:

```python
def _check_version_headers(response: httpx.Response) -> None:
    latest = response.headers.get("X-Agnes-Latest-Version")
    minv = response.headers.get("X-Agnes-Min-Version")
    if not latest or not minv:
        return  # talking to an older server; no enforcement
    local = _installed_version()  # reuse from update_check
    if local == "unknown":
        return  # dev install / editable; never block
    if _version_lt(local, minv):  # reuse update_check._version_lt
        sys.stderr.write(
            f"error: agnes {local} is incompatible with server {latest}"
            f" (min required: {minv}). Run: agnes self-upgrade\n"
        )
        sys.exit(2)
```

Only the hard-stop is enforced here — drift warnings are already handled by `update_check` in the root callback, no point doubling them on every API call.

`_version_lt` and `_installed_version` move from `cli/update_check.py` into `cli/_version_compat.py` (or stay in `update_check.py` and `client.py` imports them) — pick whichever keeps imports simple. Both files need them.

User-Agent: extend `get_client()` to set `headers={"User-Agent": f"agnes/{_installed_version()} ({platform.system().lower()})"}` (merge with caller-supplied headers).

### `cli/lib/hooks.py:install_claude_hooks` — chain self-upgrade ahead of pull

```python
_OUR_COMMAND_MARKERS = ("agnes self-upgrade", "agnes pull", "agnes push", "da sync")

_replace_or_add(
    "SessionStart",
    "agnes self-upgrade --quiet 2>/dev/null || true; "
    "agnes pull --quiet 2>/dev/null || true",
)
_replace_or_add("SessionEnd", "agnes push --quiet 2>/dev/null || true")
```

Single chained SessionStart entry. Shell `;` guarantees ordering (no reliance on Claude Code's undocumented multi-hook semantics); each `|| true` ensures one segment's failure does not abort the line. `_OUR_COMMAND_MARKERS` is extended so re-running `agnes init` recognises the chained line on substring match and replaces rather than duplicates.

### Drive-by cleanup

`cli/update_check.py` docstring (lines 1-9) still references `da` four times. Update to `agnes`. No behavior change.

---

## Tests

### Server

- New: `tests/test_version_headers_middleware.py` — `/api/sync/trigger` (or any cheap `/api/*`) returns both headers; `/web/*` and `/cli/*` do not.
- Existing `/cli/latest` tests already cover the wheel metadata path.

### CLI

- `tests/test_self_upgrade.py` — mock `update_check.check()`, mock `subprocess.run`, assert correct command shape (uv vs pip path), assert `--check-only` exits 1 when outdated and 0 when current, assert `--force` skips the `is_outdated()` short-circuit, assert success path invalidates the `update_check.json` cache.
- `tests/test_client_version_check.py` — fake response with `min > local` ⇒ `SystemExit(2)`. Fake response with `latest > local >= min` ⇒ no stderr, no exit. Local `unknown` ⇒ no enforcement. Missing headers (old server) ⇒ no enforcement.
- `tests/test_lib_hooks.py` — assert the chained command is the sole SessionStart entry, that `self-upgrade` precedes `pull`, that both segments end in `|| true`, and that re-running `install_claude_hooks` stays idempotent (length stays at 1).

---

## Migration / rollout

- Additive — no breaking change. Old CLIs (no header check, no self-upgrade command) keep working; old servers (no headers) make the new CLI silent (no enforcement, just the existing warning loop).
- Ship in one PR. CHANGELOG entry under `### Added`: "CLI now auto-upgrades from the server at session start (`agnes self-upgrade`) and hard-stops on incompatible-version mismatch via response headers."
- After merge, manually bump `MIN_COMPAT_CLI_VERSION` in the next PR that ships a wire-protocol break — that's the first time the hard-stop actually fires.

---

## Self-review

- **Spec coverage:** both layers (A/B), both directions (check + enforce), reuse of `update_check` to avoid two polling paths, hook idempotency, drive-by `da → agnes` cleanup. ✓
- **Resolved during review:** A (`cli/client.py:216` + `cli/main.py:99-104`), B (`MIN_COMPAT_CLI_VERSION = "0.0.0"` on day one), D (reuse `AGNES_NO_UPDATE_CHECK`, no new opt-out flag).
- **No placeholders:** every component has a concrete file path and existing-symbol reference.
- **Type/name consistency:** `APP_VERSION`, `MIN_COMPAT_CLI_VERSION`, `X-Agnes-Latest-Version`, `X-Agnes-Min-Version`, `agnes self-upgrade`, reused `update_check.check()` / `format_outdated_notice()` / `_version_lt()` / `_installed_version()` — consistent throughout.
- Spec, not plan: no per-step TDD breakdown. Convert to a plan once reviewed.
