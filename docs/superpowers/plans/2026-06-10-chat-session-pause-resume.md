# Chat Session Pause/Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple chat-session lifecycle from the WebSocket: turns always complete and persist, orphaned sessions pause their E2B sandbox (memory snapshot) instead of dying, and reconnects/Slack messages resume them with the agent's full in-memory context.

**Architecture:** The manager owns session tasks (today `attach()` blocks for the session's lifetime and the WS route kills on disconnect). `attach()` becomes a fast "ensure running + seat sink" call with a decision tree (live → seat; paused → resume; gone → spawn). A per-turn frame buffer enables mid-turn replay and partial-save. The provider grows `pause`/`resume`/`keepalive`. Spec: `docs/superpowers/specs/2026-06-10-chat-session-pause-resume-design.md`. The E2B reattach mechanics were validated by `tests/e2e/spike_e2b_pause_resume.py` (PASS 2026-06-10, e2b 2.27.1).

**Tech Stack:** Python/FastAPI, DuckDB + Postgres (dual-backend parity), e2b SDK ≥2.0 (`pause()`, `AsyncSandbox.connect()`, `commands.connect(pid)`, `lifecycle={"on_timeout": "pause"}`), vanilla JS frontend.

**Verified SDK facts (do not re-derive):** e2b 2.27.1 has instance `pause()` (stable) and `beta_pause()`; `AsyncSandbox.connect(sandbox_id, api_key=…)` auto-resumes; `sandbox.commands.connect(pid, on_stdout=…, timeout=0)` reattaches streaming callbacks to a running process whose memory survives; `commands.send_stdin(pid, data)` works post-resume; `AsyncSandbox.create(…, lifecycle={"on_timeout": "pause"})` pauses instead of kills on timeout; `set_timeout` exists (instance + static). There is no `beta_create` in 2.27.x.

**Hard constraint (DuckDB 1.5.3 FK+index bug):** UPDATE-ing indexed columns of `chat_sessions` after any `chat_messages` INSERT raises a false FK violation (see comments at `src/db.py:1125` and `app/chat/persistence.py` `_SESSION_SELECT`). The three new columns (`sandbox_id`, `runner_pid`, `sandbox_paused_at`) MUST stay un-indexed; un-indexed column UPDATEs work (proof: `set_title`). The paused-TTL reaper query is a plain scan — fine at chat-session cardinality.

---

### Task 1: Types — `SessionState.PAUSED` + `ChatSession` sandbox fields

**Files:**
- Modify: `app/chat/types.py` (SessionState at :16, ChatSession at :24)
- Test: `tests/test_chat_persistence.py` (extend)

- [ ] **Step 1: Add enum value and dataclass fields**

In `app/chat/types.py`:

```python
class SessionState(str, Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    PAUSED = "PAUSED"
    DEAD = "DEAD"
```

and at the end of `ChatSession` (after `ephemeral: bool = False`):

```python
    # Sandbox lifecycle refs (pause/resume). Nullable; cleared on real kill.
    # NOTE: never index these columns — DuckDB 1.5.3 FK+index bug (src/db.py).
    sandbox_id: Optional[str] = None
    runner_pid: Optional[int] = None
    sandbox_paused_at: Optional[datetime] = None
```

- [ ] **Step 2: Run the chat type/persistence tests, expect green (defaults make this additive)**

Run: `.venv/bin/pytest tests/test_chat_persistence.py -q`

- [ ] **Step 3: Commit** — `git commit -m "feat(chat): PAUSED session state + sandbox ref fields on ChatSession"`

---

### Task 2: Schema — DuckDB DDL + `_v72_to_v73` + Alembic `0020` (BOTH ladders, same task)

**Files:**
- Modify: `src/db.py` (`SCHEMA_VERSION = 72` at :50 → 73; `chat_sessions` DDL at :1125; migration ladder)
- Create: `migrations/versions/0020_chat_sandbox_refs_v73.py`
- Test: `tests/test_db_schema_version.py` (existing integration gate), `tests/test_chat_db_migration.py`

- [ ] **Step 1: Write the failing migration test**

In `tests/test_chat_db_migration.py` add:

```python
def test_v73_adds_sandbox_ref_columns(tmp_path):
    """A v72 DB migrated to current schema has the three sandbox columns."""
    conn = _make_db_at_version(tmp_path, 72)  # use this file's existing helper pattern
    from src.db import migrate
    migrate(conn)
    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='chat_sessions'").fetchall()}
    assert {"sandbox_id", "runner_pid", "sandbox_paused_at"} <= cols
    # regression: the new columns must not be indexed (DuckDB 1.5.3 bug)
    idx = conn.execute("SELECT sql FROM duckdb_indexes() WHERE table_name='chat_sessions'").fetchall()
    assert not any("sandbox" in (r[0] or "") for r in idx)
```

(Adapt the v72-fixture helper to however `tests/test_chat_v70_migration.py` builds old-version DBs — reuse, don't reinvent.)

- [ ] **Step 2: Run it, expect FAIL** — `.venv/bin/pytest tests/test_chat_db_migration.py -q`

- [ ] **Step 3: Implement**

`src/db.py`: bump `SCHEMA_VERSION = 73`; extend the `chat_sessions` CREATE TABLE (fresh installs) with:

```sql
    sandbox_id        VARCHAR,
    runner_pid        INTEGER,
    sandbox_paused_at TIMESTAMP
```

and add the ladder step next to `_v71_to_v72` (follow its registration pattern exactly):

```python
def _v72_to_v73(conn: duckdb.DuckDBPyConnection) -> None:
    """Sandbox pause/resume refs on chat_sessions (un-indexed — 1.5.3 FK bug)."""
    for ddl in (
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS sandbox_id VARCHAR",
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS runner_pid INTEGER",
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS sandbox_paused_at TIMESTAMP",
    ):
        conn.execute(ddl)
```

`migrations/versions/0020_chat_sandbox_refs_v73.py` (mirror `0019_system_secrets_v72.py`'s header/down_revision style):

```python
def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("sandbox_id", sa.String(), nullable=True))
    op.add_column("chat_sessions", sa.Column("runner_pid", sa.Integer(), nullable=True))
    op.add_column("chat_sessions", sa.Column("sandbox_paused_at", sa.DateTime(timezone=True), nullable=True))

def downgrade() -> None:
    op.drop_column("chat_sessions", "sandbox_paused_at")
    op.drop_column("chat_sessions", "runner_pid")
    op.drop_column("chat_sessions", "sandbox_id")
```

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_chat_db_migration.py tests/test_db_schema_version.py -q` — PASS

- [ ] **Step 5: Commit** — `git commit -m "feat(db): v73 — sandbox pause/resume refs on chat_sessions (DuckDB + Alembic)"`

---

### Task 3: Repo methods — DuckDB + PG siblings + contract test (same task, sync-map coupled)

**Files:**
- Modify: `app/chat/persistence.py` (`_row_to_session` :30, `_SESSION_SELECT`), `src/repositories/chat_sessions_pg.py` (`_row_to_session` :29, `ChatSessionPgRepository` :46)
- Test: `tests/db_pg/test_chat_sessions_contract.py` (extend the existing chat contract module; if the cluster file is named differently, extend that one)

- [ ] **Step 1: Write the failing contract test (parametrized over both backends, same pattern as the file's neighbors)**

```python
def test_sandbox_ref_roundtrip(repo_both_backends):
    repo = repo_both_backends
    s = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    assert repo.get_session(s.id).sandbox_id is None

    repo.set_sandbox_ref(s.id, sandbox_id="sbx_1", runner_pid=413)
    got = repo.get_session(s.id)
    assert (got.sandbox_id, got.runner_pid, got.sandbox_paused_at) == ("sbx_1", 413, None)

    ts = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    repo.set_sandbox_paused_at(s.id, ts)
    assert repo.get_session(s.id).sandbox_paused_at is not None
    assert s.id in {x.id for x in repo.list_paused_sessions(paused_before=ts + timedelta(seconds=1))}

    repo.set_sandbox_paused_at(s.id, None)          # resume clears the marker
    repo.clear_sandbox_ref(s.id)                     # real kill clears everything
    got = repo.get_session(s.id)
    assert (got.sandbox_id, got.runner_pid, got.sandbox_paused_at) == (None, None, None)
```

CRITICAL extra case (the 1.5.3 bug): same roundtrip but **after** `append_message()` has inserted rows — this is the production order (pause always happens after messages exist) and is exactly what the FK bug would break if the columns were indexed.

- [ ] **Step 2: Run, expect FAIL (method missing)** — `.venv/bin/pytest tests/db_pg/ -k sandbox -q`

- [ ] **Step 3: Implement in BOTH repos**

DuckDB (`app/chat/persistence.py`) — extend `_SESSION_SELECT` with `s.sandbox_id, s.runner_pid, s.sandbox_paused_at`, extend `_row_to_session` (indices 12-14), then:

```python
def set_sandbox_ref(self, session_id: str, *, sandbox_id: str, runner_pid: int) -> None:
    self._conn.execute(
        "UPDATE chat_sessions SET sandbox_id = ?, runner_pid = ?, sandbox_paused_at = NULL WHERE id = ?",
        [sandbox_id, runner_pid, session_id],
    )

def clear_sandbox_ref(self, session_id: str) -> None:
    self._conn.execute(
        "UPDATE chat_sessions SET sandbox_id = NULL, runner_pid = NULL, sandbox_paused_at = NULL WHERE id = ?",
        [session_id],
    )

def set_sandbox_paused_at(self, session_id: str, paused_at: Optional[datetime]) -> None:
    self._conn.execute(
        "UPDATE chat_sessions SET sandbox_paused_at = ? WHERE id = ?",
        [paused_at, session_id],
    )

def list_paused_sessions(self, *, paused_before: datetime) -> list[ChatSession]:
    rows = self._conn.execute(
        _SESSION_SELECT + " WHERE s.sandbox_paused_at IS NOT NULL AND s.sandbox_paused_at < ? "
        + _SESSION_GROUP_BY,  # match the file's actual SELECT/GROUP BY composition
        [paused_before],
    ).fetchall()
    return [_row_to_session(r) for r in rows]
```

PG (`src/repositories/chat_sessions_pg.py`): same four methods in SQLAlchemy-text style matching `set_title` / `archive_session` there; extend its `_row_to_session` and the SELECT column list identically.

- [ ] **Step 4: Run** `.venv/bin/pytest tests/db_pg/ -k sandbox tests/test_chat_persistence.py -q` — PASS

- [ ] **Step 5: Commit** — `git commit -m "feat(chat): sandbox-ref repo methods, DuckDB + PG parity + contract test"`

---

### Task 4: Config — `on_detach`, `detach_linger_seconds`, `paused_ttl_seconds`; deprecate `e2b_kill_on_ws_disconnect`

**Files:**
- Modify: `app/chat/config.py`
- Test: `tests/test_chat_config.py`

- [ ] **Step 1: Failing tests**

```python
def test_detach_defaults():
    cfg = load_chat_config(Path("/nonexistent"))
    assert cfg.on_detach == "pause"
    assert cfg.detach_linger_seconds == 60
    assert cfg.paused_ttl_seconds == 7 * 24 * 3600

def test_legacy_kill_knob_maps_to_on_detach_kill(tmp_path, caplog):
    p = tmp_path / "instance.yaml"
    p.write_text("chat:\n  enabled: true\n  e2b_kill_on_ws_disconnect: true\n")
    cfg = load_chat_config(p)
    assert cfg.on_detach == "kill"
    assert "deprecated" in caplog.text.lower()

def test_explicit_on_detach_wins_over_legacy_knob(tmp_path):
    p = tmp_path / "instance.yaml"
    p.write_text("chat:\n  enabled: true\n  e2b_kill_on_ws_disconnect: true\n  on_detach: pause\n")
    assert load_chat_config(p).on_detach == "pause"

def test_unknown_on_detach_normalizes_to_pause(tmp_path):
    p = tmp_path / "instance.yaml"
    p.write_text("chat:\n  enabled: true\n  on_detach: explode\n")
    assert load_chat_config(p).on_detach == "pause"
```

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/pytest tests/test_chat_config.py -q`

- [ ] **Step 3: Implement**

Dataclass fields (keep `e2b_kill_on_ws_disconnect` parsing for back-compat echo, but new code never reads it):

```python
    # Lifecycle when the last sink detaches: "pause" (E2B snapshot, resumable)
    # or "kill" (legacy cost-minimizing behavior).
    on_detach: str = "pause"
    detach_linger_seconds: int = 60
    paused_ttl_seconds: int = 7 * 24 * 3600
```

Loader logic (presence-checked legacy mapping, mirroring `_parse_slack_config`'s normalize-and-warn style):

```python
    on_detach = str(raw.get("on_detach", "")).strip().lower()
    if on_detach not in ("pause", "kill"):
        if on_detach:
            logger.warning("unknown chat.on_detach %r — falling back to 'pause'", on_detach)
        if "e2b_kill_on_ws_disconnect" in raw and bool(raw["e2b_kill_on_ws_disconnect"]):
            logger.warning(
                "chat.e2b_kill_on_ws_disconnect is deprecated; use chat.on_detach: kill")
            on_detach = "kill"
        else:
            on_detach = "pause"
```

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_chat_config.py -q` — PASS

- [ ] **Step 5: Commit** — `git commit -m "feat(chat): on_detach/linger/paused-ttl knobs; deprecate e2b_kill_on_ws_disconnect"`

---

### Task 5: Provider protocol + shared FakeProvider test helper

**Files:**
- Modify: `app/chat/provider.py`
- Create: `tests/chat_fakes.py` (FakeHandle exists inline in `tests/test_chat_manager.py:147` — lift it, add FakeProvider; leave a thin import alias behind so existing tests keep passing)
- Test: `tests/test_chat_manager.py` imports keep working

- [ ] **Step 1: Extend the Protocols**

```python
@runtime_checkable
class SandboxHandle(Protocol):
    pid: int
    sandbox_id: str          # NEW — provider-scoped id used for resume
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader

    async def wait(self) -> int: ...
    async def kill(self, *, grace_sec: float = 5.0) -> None: ...


@runtime_checkable
class SandboxProvider(Protocol):
    async def spawn(self, *, workdir: Path, env: dict[str, str], argv: list[str]) -> SandboxHandle: ...

    async def pause(self, handle: SandboxHandle) -> None:
        """Snapshot the sandbox (memory + fs + running processes) and detach."""
        ...

    async def resume(self, *, sandbox_id: str, runner_pid: int, env: dict[str, str]) -> SandboxHandle:
        """Reconnect a paused sandbox and reattach to the still-running runner."""
        ...

    async def keepalive(self, handle: SandboxHandle, *, timeout_seconds: int) -> None:
        """Extend the sandbox's external timeout. No-op for local providers."""
        ...
```

- [ ] **Step 2: Build `tests/chat_fakes.py`**

`FakeHandle`: move verbatim from `tests/test_chat_manager.py:147-200`, add `sandbox_id: str = "fake-sbx"`. `FakeProvider`:

```python
class FakeProvider:
    """In-memory SandboxProvider: spawn/pause/resume with state retention.

    pause() parks the handle; resume() returns the SAME handle (mirrors E2B
    semantics where the process and its memory survive). Set
    ``fail_resume=True`` to exercise the resume-failure fallback path.
    """
    def __init__(self) -> None:
        self.spawned: list[FakeHandle] = []
        self.paused: dict[str, FakeHandle] = {}
        self.fail_resume = False
        self.keepalive_calls: list[int] = []

    async def spawn(self, *, workdir, env, argv) -> FakeHandle:
        h = FakeHandle()
        h.sandbox_id = f"fake-sbx-{len(self.spawned)}"
        self.spawned.append(h)
        return h

    async def pause(self, handle) -> None:
        self.paused[handle.sandbox_id] = handle

    async def resume(self, *, sandbox_id, runner_pid, env) -> FakeHandle:
        if self.fail_resume or sandbox_id not in self.paused:
            raise RuntimeError(f"sandbox {sandbox_id} gone")
        return self.paused.pop(sandbox_id)

    async def keepalive(self, handle, *, timeout_seconds) -> None:
        self.keepalive_calls.append(timeout_seconds)
```

- [ ] **Step 3: Run the whole chat test cluster, expect green** — `.venv/bin/pytest tests/test_chat_manager.py tests/test_chat_multisink.py -q`

- [ ] **Step 4: Commit** — `git commit -m "feat(chat): provider pause/resume/keepalive protocol + shared fakes"`

---

### Task 6: E2B provider — `sandbox_id`, `pause()`, `resume()`, `keepalive()`, `lifecycle on_timeout=pause`

**Files:**
- Modify: `app/chat/e2b_provider.py` (`E2BSandboxHandle` :160, `E2BProvider.spawn` :243)
- Test: `tests/test_chat_e2b_provider.py` (mocked-SDK unit tests, follow the file's existing stubbing pattern) + one gated real-E2B test

- [ ] **Step 1: Failing unit tests (mock the SDK objects as the file already does)**

Cover: (a) `spawn()` passes `lifecycle={"on_timeout": "pause"}` to `AsyncSandbox.create`; (b) handle exposes `sandbox_id` from the SDK object; (c) `pause()` calls `sandbox.pause()`; (d) `resume()` calls `AsyncSandbox.connect(sandbox_id, api_key=…)` then `commands.connect(pid, on_stdout=…, on_stderr=…, timeout=0)` and returns a handle whose adapters feed the new callbacks; (e) `keepalive()` calls `sandbox.set_timeout(timeout_seconds)`.

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/pytest tests/test_chat_e2b_provider.py -q`

- [ ] **Step 3: Implement**

`E2BSandboxHandle`: add `sandbox_id: str` (constructor arg, `sandbox.sandbox_id`). `E2BProvider`:

```python
    async def spawn(self, *, workdir, env, argv) -> E2BSandboxHandle:
        ...
        sandbox = await AsyncSandbox.create(
            template=self._template_id,
            api_key=self._api_key,
            envs=dict(env),
            timeout=self._timeout,
            allow_internet_access=True,
            lifecycle={"on_timeout": "pause"},   # crash-safety net: timeout pauses, never kills
        )
        ...  # (rest unchanged)

    async def pause(self, handle: E2BSandboxHandle) -> None:
        await handle._sandbox.pause()

    async def resume(self, *, sandbox_id: str, runner_pid: int, env: dict[str, str]) -> E2BSandboxHandle:
        sandbox = await AsyncSandbox.connect(sandbox_id, api_key=self._api_key)
        stdout_adapter = _StreamReaderAdapter()
        stderr_adapter = _StreamReaderAdapter()
        cmd_handle = await sandbox.commands.connect(
            runner_pid,
            on_stdout=lambda c: stdout_adapter.feed(_coerce_to_bytes(c)),
            on_stderr=lambda c: stderr_adapter.feed(_coerce_to_bytes(c)),
            timeout=0,
        )
        return E2BSandboxHandle(
            pid=runner_pid,
            sandbox_id=sandbox_id,
            stdin=_StreamWriterAdapter(sandbox, runner_pid),
            stdout=stdout_adapter,
            stderr=stderr_adapter,
            _sandbox=sandbox,
            _cmd_handle=cmd_handle,
        )

    async def keepalive(self, handle: E2BSandboxHandle, *, timeout_seconds: int) -> None:
        await handle._sandbox.set_timeout(timeout_seconds)
```

(`env` is accepted for protocol symmetry; E2B resume restores the original process env from the snapshot.)

- [ ] **Step 4: Gated real-E2B test** — add `test_e2b_pause_resume_real()` to `tests/test_chat_e2b_provider.py` with the file's existing `E2B_API_KEY` skip-gate: provider.spawn of `python3 -u`-echo argv → pause → resume → send line → assert echo arrives via the resumed handle's `stdout.readline()`. (The raw-SDK variant already lives in `tests/e2e/spike_e2b_pause_resume.py`; this one goes through the provider classes.)

- [ ] **Step 5: Run** `.venv/bin/pytest tests/test_chat_e2b_provider.py -q` (gated test skips without key) — PASS

- [ ] **Step 6: Commit** — `git commit -m "feat(chat): E2B provider pause/resume/keepalive + on_timeout=pause lifecycle"`

---

### Task 7: Manager — turn buffer, sink replay, partial save

**Files:**
- Modify: `app/chat/manager.py` (`LiveSession` :54, `_pump_subprocess_to_ws` :445, `add_sink` :298, `send_user_message` :568, `kill` :757)
- Test: `tests/test_chat_manager.py`

This task is independent of pause/resume and ships standalone value.

- [ ] **Step 1: Failing tests**

```python
def test_midturn_sink_gets_buffered_frames_replayed(manager):
    # seat ws1, send user_msg, emit two token frames (NO assistant_message yet),
    # then add_sink(ws2): ws2 must receive exactly those two token frames, once,
    # before any new frames; ws1 must NOT see duplicates.

def test_turn_buffer_cleared_after_assistant_message(manager):
    # emit token + assistant_message; a sink added afterwards receives no token replay.

def test_kill_midturn_persists_partial_assistant_message(manager):
    # emit token frames "Hel", "lo" (no assistant_message), then mgr.kill(...,
    # reason="idle_ttl"); repo.list_messages must contain an assistant row whose
    # content == "Hello" and whose tool_calls metadata marks interrupted=True.

def test_kill_between_turns_persists_nothing_extra(manager):
    # complete one full turn, then kill: exactly one assistant row, no interrupted row.
```

(Use the existing `FakeWS`/`FakeHandle.emit()` machinery from `tests/chat_fakes.py`.)

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/pytest tests/test_chat_manager.py -k "buffer or partial" -q`

- [ ] **Step 3: Implement**

`LiveSession` additions:

```python
    # Frames of the in-progress turn (token/tool_call/...), replayed to
    # late-seated sinks and persisted as an interrupted message on forced
    # death. Cleared when the turn's assistant_message lands.
    turn_buffer: list[dict] = field(default_factory=list)
    turn_in_flight: bool = False
```

`send_user_message`: after the stdin write succeeds, `live.turn_buffer.clear(); live.turn_in_flight = True`.

`_pump_subprocess_to_ws`: after `await self._broadcast(live, frame)`:

```python
            ftype = frame.get("type")
            if ftype in ("token", "tool_call"):
                live.turn_buffer.append(frame)
            elif ftype in ("assistant_message", "done"):
                live.turn_buffer.clear()
                live.turn_in_flight = False
```

(`assistant_message` keeps its existing persistence block unchanged.)

`add_sink` (and the Task-8 seat path): between history replay and appending to `live.sinks`, replay `list(live.turn_buffer)` to the new sink — same serialization argument as the existing replay-then-append comment at :305.

`kill()`: before tearing down, partial-save:

```python
        if live.turn_buffer:
            partial = "".join(
                f.get("text", "") for f in live.turn_buffer if f.get("type") == "token"
            ).strip()
            if partial:
                self._repo.append_message(
                    session_id=live.chat_id, role="assistant", content=partial,
                    tool_calls=[{"interrupted": True, "reason": reason}],
                    tokens_in=None, tokens_out=None, model=None,
                )
```

- [ ] **Step 4: Run the full chat cluster** — `.venv/bin/pytest tests/ -k chat -q` — PASS

- [ ] **Step 5: Commit** — `git commit -m "feat(chat): per-turn frame buffer — mid-turn sink replay + interrupted-turn partial save"`

---

### Task 8: Manager — detach/linger/pause, attach decision tree, resume (the core)

**Files:**
- Modify: `app/chat/manager.py` (`attach` :242, new methods, `send_user_message` :568, `kill` :757)
- Test: `tests/test_chat_manager.py`, `tests/test_chat_multisink.py`

- [ ] **Step 1: Failing tests**

```python
def test_detach_last_sink_does_not_kill(manager):           # session stays ACTIVE through linger
def test_linger_then_pause_persists_refs(manager):          # linger=0 cfg → provider.pause called,
                                                            # repo row has sandbox_id/pid/paused_at, state PAUSED
def test_reattach_during_linger_cancels_pause(manager):     # seat new sink inside linger window → never paused
def test_pause_waits_for_inflight_turn(manager):            # turn_in_flight → pause deferred until done-frame
def test_attach_to_paused_resumes_same_handle(manager):     # provider.resume returns parked handle; state ACTIVE;
                                                            # paused_at cleared; pump works (emit frame → sink sees it)
def test_attach_to_live_session_does_not_spawn_second_runner(manager):
def test_resume_failure_falls_back_to_fresh_spawn(manager): # fail_resume=True → clear refs + spawn + last-turns replay
def test_send_user_message_resumes_paused_session(manager): # Slack path: message to PAUSED session resumes first
def test_attach_after_restart_resumes_from_repo_row(manager):
    # simulate restart: pause, then mgr._live.clear(); attach() must resume purely
    # from the persisted sandbox_id/runner_pid.
def test_on_detach_kill_preserves_legacy_behavior(manager_kill_cfg):  # cfg on_detach="kill" → kill, no pause
```

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/pytest tests/test_chat_manager.py -k "detach or pause or resume" -q`

- [ ] **Step 3: Implement — the structural change**

`attach()` splits into a fast seat call + manager-owned supervision. Replace the body's tail (the `gather`/`finally: kill` at :293-296) and the unconditional spawn:

```python
    async def attach(self, chat_id: str, ws, *, is_primary: bool = True) -> None:
        live = self._live.get(chat_id)
        if live is not None and live.state == SessionState.ACTIVE:
            self._cancel_linger(live)
            await self._seat_sink(live, ws, is_primary=is_primary)   # history+turn-buffer replay, then append
            return
        if live is not None and live.state == SessionState.PAUSED:
            await self._resume_live(live)
            await self._seat_sink(live, ws, is_primary=is_primary)
            return
        session = self._repo.get_session(chat_id)
        if session is None:
            raise SessionNotFound(chat_id)
        if session.sandbox_id is not None and session.runner_pid is not None:
            live = await self._resume_from_row(session)              # post-restart path
            if live is not None:
                await self._seat_sink(live, ws, is_primary=is_primary)
                return
            # resume failed → refs cleared, fall through to fresh spawn
        live = await self._spawn_live(session)                        # today's spawn body, factored out;
        await self._seat_sink(live, ws, is_primary=is_primary)        # persists set_sandbox_ref(handle.sandbox_id, pid)
```

`_spawn_live` ends with `self._repo.set_sandbox_ref(chat_id, sandbox_id=handle.sandbox_id, runner_pid=handle.pid)` and starts pump/wait tasks WITHOUT awaiting them (no `gather`, no `finally: kill` — task failure handling stays in `_wait_for_exit_and_respawn`).

Detach + linger (called by the WS route on disconnect and by `_broadcast`'s dead-sink sweep):

```python
    async def detach_sink(self, chat_id: str, ws) -> None:
        live = self._live.get(chat_id)
        if live is None:
            return
        live.sinks = [e for e in live.sinks if e.sink is not ws]
        if not live.sinks:
            self._on_all_sinks_gone(live)

    def _on_all_sinks_gone(self, live: LiveSession) -> None:
        if self._config.on_detach == "kill":
            asyncio.create_task(self.kill(live.chat_id, reason="ws_disconnect"))
            return
        self._cancel_linger(live)
        live.linger_task = asyncio.create_task(self._linger_then_pause(live))

    async def _linger_then_pause(self, live: LiveSession) -> None:
        while live.turn_in_flight:                 # a turn always finishes first
            await asyncio.sleep(1.0)
        await asyncio.sleep(self._config.detach_linger_seconds)
        if live.sinks or live.state != SessionState.ACTIVE:
            return                                  # someone came back / state changed
        await self._pause_live(live)
```

Pause (order matters — cancel the wait task FIRST so the severed connection isn't treated as a crash, same race `_respawn_co_runner` guards at :657):

```python
    async def _pause_live(self, live: LiveSession) -> None:
        live.state = SessionState.PAUSED
        for t in live.tasks:
            t.cancel()
        live.tasks = []
        live.current_pump = live.current_wait = None
        try:
            await self._provider.pause(live.handle)
        except Exception:
            logger.exception("pause failed for %s — falling back to kill", live.chat_id)
            live.state = SessionState.ACTIVE        # kill() handles partial-save + teardown
            await self.kill(live.chat_id, reason="pause_failed")
            return
        live.handle = None
        self._repo.set_sandbox_paused_at(live.chat_id, datetime.now(timezone.utc))
```

Resume (both the warm `_resume_live(live)` and cold `_resume_from_row(session)` build the same way):

```python
    async def _resume_live(self, live: LiveSession) -> None:
        session = self._repo.get_session(live.chat_id)
        try:
            handle = await self._provider.resume(
                sandbox_id=session.sandbox_id, runner_pid=session.runner_pid, env={},
            )
        except Exception:
            logger.warning("resume failed for %s — fresh spawn fallback", live.chat_id)
            self._repo.clear_sandbox_ref(live.chat_id)
            await self._respawn_fresh(live)          # spawn + last-3-user-turn replay,
            return                                   # factored from _wait_for_exit_and_respawn :526-549
        live.handle = handle
        live.state = SessionState.ACTIVE
        live.tasks = [
            asyncio.create_task(self._pump_subprocess_to_ws(live)),
            asyncio.create_task(self._wait_for_exit_and_respawn(live, self._session_dir_for(live))),
        ]
        live.current_pump, live.current_wait = live.tasks[0], live.tasks[1]
        self._repo.set_sandbox_paused_at(live.chat_id, None)
```

`send_user_message` (:568-570): when `live` exists with `state == PAUSED` → `await self._resume_live(live)` before the stdin write; when `live is None` but the repo row has sandbox refs → `_resume_from_row` first (covers Slack DM after hours AND post-restart web `user_msg` racing `attach`). `kill()` additionally calls `self._repo.clear_sandbox_ref(chat_id)`.

`LiveSession` gains `linger_task: Optional[asyncio.Task] = None` and a `session_dir: Path` field (set at spawn/resume so `_session_dir_for` is trivial — today the dir only lives as a local in `attach`).

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_chat_manager.py tests/test_chat_multisink.py -q` — PASS

- [ ] **Step 5: Commit** — `git commit -m "feat(chat): manager owns session lifecycle — detach/linger/pause, resume, spawn decision tree"`

---

### Task 9: Reaper, active-time limit, keepalive heartbeat, shutdown-pauses

**Files:**
- Modify: `app/chat/manager.py` (`shutdown` :232, `_reap_once` :845)
- Test: `tests/test_chat_manager.py`

- [ ] **Step 1: Failing tests**

```python
def test_idle_ttl_pauses_instead_of_kills(manager):         # reaper on idle ACTIVE session → PAUSED, sandbox alive
def test_paused_ttl_really_kills(manager):                  # repo row paused_before cutoff → provider kill via resume-less
                                                            # teardown + clear_sandbox_ref (works with NO LiveSession)
def test_max_session_seconds_counts_active_time_only(manager):
    # active 1h → paused 10h → resumed: not reaped at the 4h wall-clock mark
def test_shutdown_pauses_active_sessions(manager):          # shutdown() → provider.pause called, refs persisted
def test_keepalive_heartbeat_extends_timeout_while_sinks_attached(manager):
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

- `LiveSession` gains `active_seconds_accum: float = 0.0` and `active_since: float` (monotonic, reset in `_spawn_live`/`_resume_live`; `_pause_live` folds `time.monotonic() - active_since` into the accumulator). `_reap_once`'s `max_session_seconds` check uses `active_seconds_accum + (now - active_since)`.
- `_reap_once`: for `idle_ttl` victims, call `_pause_live` when `on_detach == "pause"` (kill otherwise — legacy). Add a second sweep over `self._repo.list_paused_sessions(paused_before=now - paused_ttl_seconds)`: for each, best-effort sandbox deletion through a new provider call (`resume`-then-`kill` is wasteful — add `async def destroy(self, *, sandbox_id: str) -> None` to the protocol, E2B impl `await AsyncSandbox.kill(sandbox_id, api_key=…)` via the static class-kill `_cls_kill` public equivalent, FakeProvider impl pops `self.paused`), then `clear_sandbox_ref` + drop any `_live` entry. The sweep works purely from repo rows so it also collects pre-restart leftovers.
- `shutdown()`: `_pause_live` for every ACTIVE session (pause stops billing AND preserves the conversation across deploys); `kill` only when `on_detach == "kill"`.
- Heartbeat: in `_idle_reaper_loop`'s existing periodic tick, for every ACTIVE session with sinks call `await self._provider.keepalive(live.handle, timeout_seconds=self._config.idle_ttl_seconds + 300)` — the sandbox's external timeout always exceeds the in-process reaper horizon, and `lifecycle on_timeout=pause` catches anything the heartbeat misses (e.g. a crashed server).

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_chat_manager.py -q` — PASS

- [ ] **Step 5: Commit** — `git commit -m "feat(chat): reaper pauses idle sessions, paused-TTL GC, active-time cap, shutdown pauses"`

---

### Task 10: API — `ws_stream` decoupling, paused flag in session listings

**Files:**
- Modify: `app/api/chat.py` (`ws_stream` :233-238, the sessions list endpoint), `services/slack_bot/events.py` + `commands.py` (only if their `_schedule(mgr.attach(...))` needs the new fast-return semantics — `_is_attached` keeps working since `_live` registry semantics are unchanged)
- Test: `tests/test_chat_api.py`, `tests/test_chat_web_route.py`

- [ ] **Step 1: Failing test**

```python
def test_ws_disconnect_detaches_but_does_not_kill(client_with_fake_provider):
    # open ws → send user_msg → close ws → manager session still in _live,
    # state ACTIVE (linger pending), runner handle not killed.
def test_sessions_list_exposes_paused(client_with_fake_provider):
    # paused session row → GET /api/chat/sessions includes "paused": true
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

`ws_stream` tail becomes:

```python
    try:
        await mgr.attach(chat_id_v, ws)          # fast: seat sink (+ spawn/resume if needed)
        await reader_loop()
    except SessionNotFound:
        await ws.close(code=4404, reason="session_not_found")
    finally:
        await mgr.detach_sink(chat_id_v, ws)
```

(The 30 s `user_msg` retry loop at :212 stays — it now also covers the resume window.) Sessions list endpoint: include `"paused": s.sandbox_paused_at is not None` in the per-session dict. Slack call sites: `_schedule(mgr.attach(session.id, sink))` still correct — attach completing quickly only removes the old need to never-await it; do not change `_is_attached`.

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_chat_api.py tests/test_chat_web_route.py tests/test_slack* -q` — PASS

- [ ] **Step 5: Commit** — `git commit -m "feat(chat): ws disconnect detaches sink instead of killing the session"`

---

### Task 11: Frontend — resuming status + paused badge

**Files:**
- Modify: `app/web/static/js/chat.js` (`openSession` :409, session-list rendering), session list template if titles/badges render server-side
- Test: `tests/test_chat_web_route.py` (server-rendered bits only; JS is untested in this repo)

- [ ] **Step 1: Implement**

- In `openSession`, between WS open and the `ready` frame, set status `"Resuming session…"` (the existing `setStatus` helper) — today it shows a generic connecting state; resume takes ~1-2 s.
- In the session-list rendering, when the API row has `paused: true`, append a `⏸` badge (use the existing badge/`ds.*` styling used by the surface badge from `tests/test_chat_surface_badge.py`'s feature).

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_chat_web_route.py tests/test_chat_surface_badge.py -q` — PASS

- [ ] **Step 3: Commit** — `git commit -m "feat(chat): resuming status + paused badge in web UI"`

---

### Task 12: Docs + CHANGELOG

**Files:**
- Modify: `docs/cloud-chat.md` (lifecycle section around :144 — rewrite the disconnect/idle story), `tests/e2e/acceptance/MANUAL_RUNBOOK.md` (assertion 7 inverts: disconnect now leaves the session resumable; add pause/resume + Slack-resume walkthrough), `CHANGELOG.md` (`## [Unreleased]`)
- Test: none (docs)

- [ ] **Step 1: CHANGELOG bullets (Added/Changed/Deprecated grouping)**

```markdown
### Added
- Chat sessions survive browser disconnects: in-flight turns always complete and persist;
  idle/orphaned sessions pause their sandbox (memory snapshot) and resume with full agent
  context on reconnect or on the next Slack message. New knobs: `chat.on_detach`
  (`pause`|`kill`, default `pause`), `chat.detach_linger_seconds` (60),
  `chat.paused_ttl_seconds` (7 days). Mid-turn reconnects replay the in-progress turn;
  force-killed mid-turn output is persisted as an interrupted assistant message.

### Deprecated
- `chat.e2b_kill_on_ws_disconnect` — use `chat.on_detach: kill`.
```

- [ ] **Step 2: Commit** — `git commit -m "docs(chat): pause/resume lifecycle docs + changelog"`

---

## Post-build verification (not part of /agnes-build tasks)

1. Full suite: `.venv/bin/pytest tests/ --tb=short -n auto -q` — green before push.
2. Gated E2B: run `tests/test_chat_e2b_provider.py -k real` and `tests/e2e/spike_e2b_pause_resume.py` with `E2B_API_KEY` (note: the project venv must hold `e2b>=2.0.0`; stale venvs have 1.x).
3. Dev-instance manual acceptance (MANUAL_RUNBOOK additions): reload mid-answer → streaming continues; close tab >linger → return → "resuming…" → context-recall question answers correctly; Slack DM to a paused session resumes it; `on_detach: kill` config restores legacy behavior.
