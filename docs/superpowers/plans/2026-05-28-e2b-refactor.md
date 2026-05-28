# Cloud Chat — E2B-first refactor plan (Phase H)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `SubprocessProvider` (nsjail-wrapped local subprocess on the Agnes server) with `E2BProvider` (E2B-hosted ephemeral microVMs) as the default and only production provider. Drop nsjail config, iptables OWNER rules, host-uid plumbing, and most of the docker-compose E2E env. Local subprocess survives only as `MockE2BProvider` for dev tests (no real E2B billing in CI).

**Why:** Owner reversed the v1 default during the cloud-chat PR (#465) review. Operator burden of nsjail+iptables+`agnes-sandbox` uid mapping is incompatible with the original "click and create workspace" UX vision. E2B handles isolation, network policy, and lifecycle natively — operator setup is "obtain E2B API key + build template", everything else is the provider's problem.

**Architecture:** All cloud chat sessions spawn an E2B ephemeral microVM via the E2B Python SDK. Per-user persistent workspace lives on the Agnes server file system (unchanged); at sandbox spawn, `e2b_workspace_sync.py` uploads the workspace tree into the sandbox at `/work/`. On session end, modified workspace contents flow back to Agnes via the same sync layer. `claude-agent-sdk` runs inside the sandbox against the synced workspace.

**Tech stack:** Python 3.11+, `e2b` Python SDK (>=1.0.0), pre-built E2B template `agnes-chat-vX` containing Python+claude-agent-sdk+`agnes` CLI. No nsjail, no iptables, no host uid mapping.

**Reference:**
- Current spec: `docs/superpowers/specs/2026-05-28-cloud-claude-code-design.md` (will be patched in Task H.0)
- Current implementation plan: `docs/superpowers/plans/2026-05-28-cloud-claude-code.md` (will be marked superseded for provider sections)
- Follow-up plan: `docs/superpowers/plans/2026-05-28-cloud-chat-followups-and-e2e.md`
- E2B Python SDK docs: https://e2b.dev/docs

---

## Migration story — what changes vs. what stays

### Stays (no rework)
- `app/chat/types.py` — Surface, SessionState, ChatSession, ChatMessage, UserWorkdir
- `app/chat/persistence.py` — ChatRepository
- `app/chat/workdir.py` — WorkdirManager (per-user workspace path management; only the "use this from inside sandbox" assumption changes)
- `app/chat/manager.py` — ChatManager (interface unchanged; internals delegate to provider; some _spawn_runner changes)
- `app/chat/runner.py` — the in-sandbox Python entrypoint stays identical; only its execution location changes
- `app/chat/provider.py` — SandboxProvider Protocol
- `app/chat/config.py` — ChatConfig (some knobs renamed, see Task H.4)
- `app/chat/audit.py` — audit_log writer
- `app/chat/persistence.py::ChatRepository` — sessions, messages, workdirs
- `app/api/chat.py` — REST + WS endpoints
- `app/api/admin_chat.py` — admin observability
- `services/slack_bot/*` — Slack adapter
- `app/initial_workspace_default/.claude/hooks/pre_tool_use.py` — default safety hook
- Database migration v60 — no schema change
- Architect's 6 caveats from foundation review — all still satisfied (the rationale survives the provider swap)

### Goes away
- `app/chat/subprocess_provider.py` (~250 LOC, gone)
- `config/nsjail/chat-session.cfg.template` (~100 LOC, gone)
- `tests/security/test_nsjail_escape.py` (gone — E2B holds isolation)
- `tests/e2e/iptables-setup.sh` (gone)
- `tests/e2e/Dockerfile.e2e` + `docker-compose.e2e.yml` — drastically simplified (still need a way to run real chat with sample data, but no nsjail/iptables stack)
- `.github/workflows/e2e-nsjail.yml` — renamed and rewritten as `e2e-e2b.yml`
- `ChatConfig.sandbox_uid` — irrelevant under E2B
- `ChatConfig.require_isolation` — irrelevant under E2B (E2B IS the isolation)
- `_ENV_ALLOWLIST` in subprocess provider — E2B sandbox is its own env namespace; no host-env leakage path exists
- `docs/cloud-chat.md` § "Network egress allowlist (operator setup)" — bake the allowlist into E2B template, drop the iptables recipe

### New
- `app/chat/e2b_provider.py` — implements SandboxProvider Protocol via E2B SDK (~300 LOC)
- `app/chat/e2b_workspace_sync.py` — upload/download per-user workspace to/from sandbox (~150 LOC)
- `app/initial_workspace_default/e2b-template/` — Dockerfile + `e2b.toml` defining the Agnes sandbox template (~100 LOC + ops docs). **No firewall rules** per Q4 — egress allowlist lives only in PreToolUse hook.
- `tests/test_chat_e2b_provider.py` — unit tests via `unittest.mock.patch("e2b.Sandbox")` (~250 LOC). No `MockE2BProvider` class.
- `tests/e2e/test_e2b_smoke.py` — opt-in real E2B smoke test (`AGNES_E2E_E2B=1`)

---

## Pre-flight — 7 design decisions (owner-signed 2026-05-28)

**All 7 questions answered.** Three diverge from architect's default recommendation; consequences flagged inline.

### Q1 — Workspace sync strategy

| Option | Description | Recommendation |
|---|---|---|
| A | Push entire workspace to sandbox at spawn (rsync-style, every file) | ✓ **recommended for v1** |
| B | Push only diff since last sync (per-user manifest, content-hash based) | future optimization |
| C | Mount workspace from external storage (E2B doesn't natively support this for arbitrary user dirs; would need a custom FUSE layer or remote-FS) | reject |

**Recommendation:** A. Cap per-user workspace at 100 MB (configurable). First-sync latency at 100 MB ≈ 3–5 s over E2B's filesystem API; acceptable for first-message-of-session given users wait for the LLM anyway. B is a future-optimization commit on top.

### Q2 — Template versioning + lifecycle

| Option | Description | Recommendation |
|---|---|---|
| A | Build template per Agnes release; `chat.e2b_template_id` pinned in `instance.yaml`; operator runs `e2b template build` as a release step | ✓ **recommended** |
| B | Build template at server startup if mismatch detected | reject — too much operator surprise |
| C | Use latest template tag; trust E2B namespace | reject — risk of silent template upgrade breaking runner |

**Owner decision: C (latest tag) — diverges from recommendation.** Operator picks ops simplicity over upgrade safety. Consequence: when anyone in the org rebuilds `agnes-chat:latest`, all live deployments pick up the new template on their next sandbox spawn. If a rebuild ships an incompatible `claude-agent-sdk` version, the runner code may break silently. Mitigation: docs warn operators *"`:latest` is global — test rebuilds on a dev Agnes first; for production rollouts consider pinning a content-hashed tag temporarily"*.

### Q3 — Cost gating

| Option | Description | Recommendation |
|---|---|---|
| A | Default 30-min idle TTL; kill on idle | current behavior — keep |
| B | Add: kill on WS disconnect (no grace period for cloud-chat) | ✓ **add as additional gate** |
| C | Pre-pool warm sandboxes (E2B supports session resume in newer SDK) | future — separate spec |

**Recommendation:** A + B both active. Idle TTL is the safety net; WS-disconnect-kill is the optimistic case. Per-session cost cap: kill at `chat.max_session_seconds` (already exists) AND emit a warning frame to WS at 80% of cap so user can wrap up.

### Q4 — E2B sandbox network policy

| Option | Description | Recommendation |
|---|---|---|
| A | Bake allowlist into E2B template (firewall rules in template); template ships with allowed = `api.anthropic.com, api.github.com, <agnes-host>` | ✓ **recommended** |
| B | E2B template defaults open; allowlist enforced by inspecting outbound at PreToolUse hook only | reject — fail-open |
| C | No egress; runner only talks to Anthropic via E2B's hosted LLM gateway | tempting but coupling to E2B LLM gateway is yet another dep |

**Owner decision: B (default open + PreToolUse hook only) — diverges from recommendation.** Owner picks ops simplicity over defense-in-depth. Consequence: a prompt injection that convinces the agent to bypass or rewrite the hook's allowlist can exfil data to arbitrary external hosts. Architect's Critical caveat #6 ("tighten allowlist to `api.github.com` only; fail-closed") is **partially undone** — the allowlist exists only in the hook, which is itself running inside the agent's tool surface. Mitigation: PreToolUse hook is bundled in the default workspace template and applies to every `Bash` invocation; spec § Known limitations gets an explicit "fail-open egress" entry; future commit could re-introduce E2B firewall as additional defense layer once owner reverses.

### Q5 — E2B API key handling

| Option | Description | Recommendation |
|---|---|---|
| A | `E2B_API_KEY` env on Agnes server; gate at startup (mirror `ANTHROPIC_API_KEY` gate) | ✓ **recommended** |
| B | Per-user E2B accounts; user provides their own E2B key on first chat | reject — UX friction defeats the point |
| C | Use E2B's "team" / "org" account; user's identity surfaces via session attribution | future — once E2B ships that feature |

**Recommendation:** A. Single org-level E2B account per Agnes deployment. Operator obtains key from E2B dashboard, sets `E2B_API_KEY` env. Mirror `_chat_jwt_secret_ok` / `_chat_anthropic_key_ok` gate.

### Q6 — Failure mode when E2B is unavailable

| Option | Description | Recommendation |
|---|---|---|
| A | No fallback — chat returns 503 "chat sandbox provider unreachable; try again" | ✓ **recommended** |
| B | Keep `SubprocessProvider` as opt-in emergency fallback in `instance.yaml :: chat.fallback_provider: subprocess` | reject — duplicates ops surface |
| C | Graceful degradation: serve last N user sessions read-only | future |

**Recommendation:** A. E2B SLA + operator monitoring is the answer. Surfacing a clear error to the user is better than silently degrading to less-isolated subprocess.

### Q7 — Dev experience (local + CI)

| Option | Description | Recommendation |
|---|---|---|
| A | Dev burns real E2B budget (each `pytest tests/e2e/` invocation = ~$0.X) | reject — bad for CI matrix runs |
| B | `MockE2BProvider` runs `claude-agent-sdk` as a subprocess locally with a thin shim that mimics E2B's stream API; selected by `instance.yaml :: chat.provider: mock_e2b` or `AGNES_TESTING=1` | ✓ **recommended** |
| C | Provider selection per-test via fixture (some tests with real E2B, most with mock) | overlaps with B; B + opt-in real-E2B tests handles it |

**Owner decision: A (real E2B everywhere) — diverges from recommendation.** Owner accepts the per-PR CI billing cost in exchange for full-fidelity testing. Consequences:

- **`MockE2BProvider` is not implemented.** Task H.5 is removed from this plan. Unit tests mock the `e2b` SDK at the import boundary (`unittest.mock.patch("app.chat.e2b_provider.Sandbox")`); E2E tests require a real `E2B_API_KEY`.
- **Agnes OSS contributors without an E2B account cannot run `tests/e2e/`.** README and CONTRIBUTING update needed to document this.
- **CI billing impact:** every PR + main push runs the E2E suite against real E2B. At 10 sandbox spawns × ~30s each × $X/spawn-minute, this is a per-run cost the operator monitors. GitHub Secret holds the API key.

---

## Phase H tasks (sequential — most touch shared files)

### Task H.0 — Spec update

**Files:** Modify `docs/superpowers/specs/2026-05-28-cloud-claude-code-design.md`

- [ ] **Step 1: Patch § "Why subprocess, not E2B-style sandbox, in v1"**
  Replace with § "Why E2B in v1": owner reversed default during PR review; nsjail+iptables operator burden incompatible with "zero-install UX" intent; E2B carries isolation+network+lifecycle natively; single-tenant Agnes still benefits because operator burden — not threat model — is the deciding factor.
- [ ] **Step 2: Patch § "Architecture" diagram**
  Replace the per-session subprocess box with an E2B sandbox box; show the sync layer between Agnes and E2B.
- [ ] **Step 3: Patch § "Security & isolation"**
  Drop nsjail config, iptables, env scrub, host uid mapping, workspace-shared-dir flock. Add E2B network policy (template-baked allowlist), E2B's filesystem isolation, the API key handling.
- [ ] **Step 4: Patch § "Deployment requirements"**
  Drop nsjail binary, agnes-sandbox host user, iptables operator step, host RAM/CPU floor for sandboxes. Add E2B account requirements, template build step, `E2B_API_KEY` env, per-session E2B cost estimate.
- [ ] **Step 5: Patch § "Defaults chosen — confirm or flip in review" table**
  Drop `sandbox_uid`, `require_isolation`. Add `chat.provider` (`e2b` default, `mock_e2b` opt-in for testing), `chat.e2b_template_id`, `chat.e2b_workspace_max_bytes`.
- [ ] **Step 6: Patch § "Build plan"**
  Add the H phase as a final batch.
- [ ] **Step 7: Commit** with explicit paths.

```
git add docs/superpowers/specs/2026-05-28-cloud-claude-code-design.md
git commit -m "docs(spec): reverse v1 sandbox default to E2B; subprocess becomes mock_e2b dev-only"
```

### Task H.1 — Add `e2b` Python SDK to deps

**Files:** Modify `pyproject.toml`

- [ ] Add `e2b>=1.0.0,<2.0.0` to `[project] dependencies` (alphabetical).
- [ ] Install + verify: `/Users/zdeneksrotyr/Sources/VsCode/component_factory/tmp_oss/.venv/bin/python -c "import e2b; print(e2b.__version__)"`
- [ ] Smoke-check the SDK's `Sandbox` class signature (constructor args, `process.start`, `files.write`, stream API) — paste actual output into the commit body for the next task to reference.
- [ ] Commit: `deps: add e2b Python SDK for cloud chat sandbox provider`.

### Task H.2 — Define E2B sandbox template

**Files:**
- Create: `app/initial_workspace_default/e2b-template/Dockerfile`
- Create: `app/initial_workspace_default/e2b-template/e2b.toml`
- Create: `app/initial_workspace_default/e2b-template/README.md` (operator build instructions)

- [ ] **Step 1: Dockerfile**

```dockerfile
FROM e2bdev/python:latest
RUN pip install claude-agent-sdk>=0.2.87,<0.3.0
RUN pip install duckdb httpx pyyaml jwt   # whatever agnes CLI needs
COPY agnes_cli_install.sh /opt/agnes_cli_install.sh
RUN bash /opt/agnes_cli_install.sh
# The runner script is uploaded at spawn time via files.write — not baked.
```

- [ ] **Step 2: `e2b.toml`** — defines template name `agnes-chat:latest` (per Q2 decision — single mutable tag, not per-version), CPU/memory limits. **No `allowed_hosts` / firewall rules** per Q4 decision — egress allowlist lives only in PreToolUse hook bundled in workspace template. Document the trade-off in the template README.
- [ ] **Step 3: README.md** — `e2b auth login` → `e2b template build` → save returned `template_id` into `chat.e2b_template_id` in `instance.yaml`.
- [ ] Commit.

### Task H.3 — Implement E2BProvider

**Files:**
- Create: `app/chat/e2b_provider.py`
- Test:   `tests/test_chat_e2b_provider.py`

- [ ] **Step 1: SandboxHandle adapter over E2B's Process API.** Map E2B's `Process.stdout.stream()` → `asyncio.StreamReader`-like adapter; map `Process.stdin.write()` → bytes channel.
- [ ] **Step 2: `E2BProvider.spawn(...)`** that:
  - Creates `Sandbox(template_id=..., api_key=..., env=env_vars)`
  - Uploads runner code path / dependencies that aren't baked into the template
  - Calls `sandbox.process.start(argv)`
  - Returns an `E2BSandboxHandle` wrapping the running process + the sandbox lifetime
- [ ] **Step 3: `E2BSandboxHandle.kill(grace_sec)`** sends SIGTERM via `process.send_signal`, waits, then `sandbox.kill()` if still alive.
- [ ] **Step 4: Tests** using `unittest.mock` to fake the E2B SDK. Real-SDK integration covered by `tests/e2e/test_e2b_smoke.py` (Task H.10).
- [ ] Commit.

### Task H.4 — Workspace sync layer

**Files:**
- Create: `app/chat/e2b_workspace_sync.py`
- Test:   `tests/test_chat_e2b_workspace_sync.py`

- [ ] **Step 1: `upload_workspace(sandbox, local_path, max_bytes)`** — walks local workspace tree, uploads each file via `sandbox.files.write`. Refuses if total > `max_bytes` (default 100 MB).
- [ ] **Step 2: `download_workspace(sandbox, local_path)`** — reverse direction; called on session end.
- [ ] **Step 3: Symlink handling** — `.claude/skills`, `.claude/plugins`, `CLAUDE.md` are normally symlinks from session dir to per-user workspace; the sync layer must dereference them so the sandbox sees real files.
- [ ] **Step 4: Tests** with mocked E2B SDK + a `pyfakefs`-style local workspace.
- [ ] Commit.

### ~~Task H.5 — `MockE2BProvider`~~ (DROPPED per owner decision on Q7)

Owner picked real E2B everywhere (dev + CI). Unit tests mock the SDK at the
import boundary instead of a dedicated provider class. See `tests/test_chat_e2b_provider.py`
(Task H.3) for the `unittest.mock.patch` pattern.

### Task H.6 — Refactor ChatManager._spawn_runner + app/main.py wiring

**Files:**
- Modify: `app/chat/manager.py` (`_spawn_runner` body)
- Modify: `app/main.py` (provider selection at startup)
- Modify: `app/chat/config.py` (new knobs)

- [ ] **Step 1:** `_spawn_runner` becomes:
  ```python
  handle = await self._provider.spawn(workdir=session_dir, env=env, argv=argv)
  if not getattr(self._provider, "syncs_workspace", False):
      await upload_workspace(handle.sandbox, session_dir, self._config.e2b_workspace_max_bytes)
  return handle
  ```
- [ ] **Step 2:** `app/main.py` reads `chat.provider`; instantiates `E2BProvider` or `MockE2BProvider` accordingly; refuses unknown values.
- [ ] **Step 3:** `ChatConfig` gains `provider: str = "e2b"`, `e2b_template_id: Optional[str] = None`, `e2b_workspace_max_bytes: int = 100 * 1024 * 1024`. Drops `sandbox_uid`, `require_isolation`.
- [ ] **Step 4:** Add `_chat_e2b_api_key_ok` startup gate (mirror Anthropic gate). Add `_chat_e2b_template_id_ok` (refuse `chat.enabled: true` without a template id when provider is `e2b`).
- [ ] **Step 5:** Tests update in `tests/test_chat_deployment_gates.py`.
- [ ] Commit.

### Task H.7 — Drop `SubprocessProvider` + nsjail config + iptables setup

**Files:**
- Delete: `app/chat/subprocess_provider.py`
- Delete: `config/nsjail/chat-session.cfg.template`
- Delete: `tests/security/test_nsjail_escape.py`
- Delete: `tests/e2e/iptables-setup.sh`
- Modify: `tests/e2e/Dockerfile.e2e` (drop nsjail build chain; if Dockerfile becomes pointless, delete and revisit Task H.10)
- Modify: `tests/e2e/docker-compose.e2e.yml` (drop NET_ADMIN cap; simplify drastically)
- Modify: `tests/test_chat_subprocess_provider.py` → rename → `tests/test_chat_mock_e2b_provider.py` if not already covered by H.5
- Delete: `.github/workflows/e2e-nsjail.yml` (renamed in Task H.10)

- [ ] Commit: `refactor(chat): drop SubprocessProvider, nsjail config, iptables setup — E2B is now the only provider`

### Task H.8 — Rewrite `docs/cloud-chat.md` for E2B model

- [ ] Replace § "Enabling on an instance" — operator obtains E2B API key, builds template, sets envs, flips flag.
- [ ] Replace § "Host requirements" — Agnes server no longer needs nsjail/iptables/sandbox_uid; runtime sandbox lives in E2B. Single-worker constraint stays (manager state still in-memory).
- [ ] Replace § "Network egress allowlist (operator setup)" with the new "Template firewall rules" — operator extends the e2b.toml template if they need additional hosts.
- [ ] Replace § "Known limitations" — the 6 architect-bullets from `f1ac0427` mostly survive; drop the `sandbox_uid ↔ iptables` bullet; add: "E2B API outage → chat unavailable, no fallback", "Per-session E2B cost is operator-visible in their E2B dashboard, not in Agnes UI yet".
- [ ] Commit.

### Task H.9 — Rewrite `docs/DEPLOYMENT.md` § Cloud-chat host requirements

- [ ] Drop host RAM/CPU floor for sandboxes (E2B handles it). Keep the Agnes-side floor for manager + chat_repo + WS connections.
- [ ] Add E2B account requirements, billing setup, template build step.
- [ ] Drop iptables OWNER recipe.
- [ ] Commit.

### Task H.10 — Rewrite E2E infrastructure for E2B

**Files:**
- Modify: `tests/e2e/conftest.py` — `docker_e2e_agnes` fixture becomes `e2e_agnes`; spins up uvicorn directly without docker (or keeps a much simpler docker-compose for the FastAPI app, no nsjail, no sample-data loader changes).
- Create: `tests/e2e/test_e2b_smoke.py` — opt-in real E2B smoke (`AGNES_E2E_E2B=1` env).
- Modify: `tests/e2e/test_load.py`, `test_adversarial.py` — adversarial tests update to assert E2B network policy refuses (not iptables), drop nsjail-specific FS escape (E2B template's chroot handles it).
- Modify: `tests/e2e/test_workspace_init.py` (F.1) — workspace init now happens at sync time, not at directory creation. Assertion updates.
- Create: `.github/workflows/e2e-e2b.yml` — runs `pytest tests/e2e/` with `AGNES_E2E=1 AGNES_E2E_E2B=1` env (requires `E2B_API_KEY` GitHub Actions secret).

- [ ] Commit: `test(e2e): rewrite infra for E2B-only provider`

### Task H.11 — Update F.* E2E scenarios

- [ ] Adjust each F.x test that previously assumed docker_exec / in-container Python introspection (`F.6 BQ budget peek`). Replace with HTTP API calls to a new `/admin/chat/{id}/debug` endpoint (admin-only) that exposes the same data, OR drop the assertion.
- [ ] Verify F.10 Slack roundtrip still works (it shouldn't depend on the provider).
- [ ] Commit.

### Task H.12 — Final architect review + fix-it pass

- [ ] Dispatch architect (`Plan` agent) on the full Phase H diff against the foundation; verify the 6 original architect caveats survive under E2B.
- [ ] Apply punch list as a single commit.

### Task H.13 — Release-cut on the PR

- [ ] Once Phase H lands cleanly: version bump in `pyproject.toml`, CHANGELOG `[Unreleased]` → `[X.Y.Z+1] — YYYY-MM-DD`, new empty `[Unreleased]`. **LAST commit on the PR**, per `CLAUDE.md` § Release process.

---

## Acceptance criteria

- [ ] `chat.enabled: true` with `chat.provider: e2b` and a valid `E2B_API_KEY` + `chat.e2b_template_id` succeeds on macOS dev (E2B is cloud) and on Linux production.
- [ ] First-message latency on cold sandbox: ≤10 s (1 s E2B spawn + ≤5 s workspace sync + ≤2 s runner_ready emission).
- [ ] Subsequent messages reuse the sandbox (no respawn unless idle TTL fired). Sub-second.
- [ ] F.1 (cold-start workspace) passes with real E2B (`AGNES_E2E_E2B=1`).
- [ ] F.2–F.5 (catalog/schema/describe/query) pass with real E2B + `AGNES_E2E_ANTHROPIC=1`.
- [ ] F.10 (Slack roundtrip) unchanged behavior.
- [ ] No reference to `subprocess`, `nsjail`, `iptables`, or `sandbox_uid` in `app/chat/`, `app/api/`, `docs/cloud-chat.md`, `docs/DEPLOYMENT.md`, or `config/instance.yaml.example`.
- [ ] All 6 original architect caveats still satisfied under the new provider.
- [ ] CHANGELOG `### Changed` bullet documents the provider reversal.
- [ ] Smoke run on a real Keboola/Groupon Agnes instance (manual; not gated by tests).

---

## Out of scope (deferred to follow-up plans)

- Workspace sync diff-only mode (Q1 option B)
- Warm sandbox pool (Q3 option C)
- E2B team/org attribution per Slack user (Q5 option C)
- Graceful E2B-outage degradation (Q6 option C)
- Per-user E2B billing visibility in Agnes admin UI (currently lives in operator's E2B dashboard)
- E2B template versioning automation (currently operator runs `e2b template build` manually per Agnes release)

---

## Recap of what gets thrown away vs. preserved from #465

**Preserved (~80% of the foundation):**
- All of spec § Goals, § Non-goals, § Data model, § API surface, § Lifecycle, § Auth & RBAC, § Cost & isolation limits (some knobs renamed)
- All of `app/chat/{types,persistence,workdir,manager,runner,config,audit}.py`
- All of `app/api/{chat,slack,admin_chat}.py`
- All of `services/slack_bot/*`
- All of `app/initial_workspace_default/.claude/hooks/pre_tool_use.py`
- Database migration v60
- The 13 architect findings + their fixes (BQ budget wire, ChatConfig knobs, crash-respawn lifecycle, etc.)
- All of the F.* E2E test scenarios (minor refactor only)

**Thrown away (~20%):**
- nsjail config + isolation logic
- iptables setup
- host uid mapping
- `SubprocessProvider` (becomes `MockE2BProvider` with different intent)
- docker-compose env's complexity (FastAPI part stays, isolation/iptables stack goes)
- nsjail escape smoke tests

The architect's PR #465 verdict "approve for merge" remains correct for everything except the provider; replace the provider, re-review the provider boundary, and the rest holds.
