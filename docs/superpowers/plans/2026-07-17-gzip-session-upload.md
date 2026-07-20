# Gzip Session Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `agnes push` gzip-compresses session transcript uploads when the server advertises support, and the server stream-decompresses them at ingest so the stored corpus stays plain JSONL.

**Architecture:** The wire protocol is the multipart part filename: `<sid>.jsonl.gz` means gzip content, `<sid>.jsonl` means plain (unchanged path). The server advertises the capability via a new `X-Agnes-Accepts: session-gzip` response header on `/api/*` (same middleware as the existing version headers); the client probes `GET /api/health` once per push run and falls back to plain on any doubt. Decompression streams chunk-by-chunk with the 50 MB cap enforced on **decompressed** bytes (zip-bomb guard).

**Tech Stack:** FastAPI/Starlette (server), stdlib `zlib`/`gzip`, httpx + Typer (CLI), pytest.

**Spec:** `docs/superpowers/specs/2026-07-17-gzip-session-upload-design.md`

## Global Constraints

- Redaction stays FIRST: `gzip(redact_bytes(raw))` — never compress unredacted bytes.
- Stored filename has `.gz` stripped; the on-disk corpus stays pure JSONL.
- `MAX_UPLOAD_SIZE` (50 MB) binds on decompressed output bytes; keep the raw-transfer counter as a second bound.
- Corrupt/truncated gzip → HTTP 400 (detail `invalid_gzip`) — a permanent failure for the client's classifier. Oversize → HTTP 413 (matches existing).
- Fail-open to plain: capability header absent, probe error, or `AGNES_PUSH_NO_GZIP=1` → upload uncompressed.
- No new CLI flags (command-UX standard: no new boolean flags for internals). Escape hatch is the env var only.
- No DB/schema changes, no repository work → no parity/migration surface.
- CHANGELOG bullet under `[Unreleased]` in the same PR. No AI attribution in commits. Vendor-agnostic wording everywhere.
- Full suite before push: `.venv/bin/pytest tests/ --tb=short -n auto -q`.

---

### Task 1: Server capability header

**Files:**
- Modify: `app/version.py` (add constant at the end)
- Modify: `app/main.py:1238-1245` (the `_add_version_headers` middleware)
- Test: `tests/test_upload_api.py` (new class `TestSessionGzipCapability`)

**Interfaces:**
- Produces: response header `X-Agnes-Accepts: session-gzip` on every `/api/*` response. Constant `app.version.SERVER_CAPABILITIES = "session-gzip"` (comma-separated list format for future entries). Task 3's client probe reads exactly this header name and token.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_upload_api.py`:

```python
class TestSessionGzipCapability:
    def test_api_responses_advertise_session_gzip(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/health")
        caps = resp.headers.get("X-Agnes-Accepts", "")
        assert "session-gzip" in [t.strip() for t in caps.split(",")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_upload_api.py::TestSessionGzipCapability -v`
Expected: FAIL — `X-Agnes-Accepts` header missing (`assert 'session-gzip' in ['']`).

- [ ] **Step 3: Implement**

In `app/version.py`, append at the end of the file:

```python
# Comma-separated list of opt-in wire-protocol capabilities the server
# accepts, advertised on /api/* responses as `X-Agnes-Accepts`. Clients
# treat an absent header as "none" and fall back to legacy formats.
# `session-gzip`: POST /api/upload/sessions accepts a gzip-compressed
# transcript when the part filename ends in `.gz`.
SERVER_CAPABILITIES = "session-gzip"
```

In `app/main.py`, the `_add_version_headers` middleware sets the two version headers inside the `/api/` branch. Add the capability header next to them:

```python
        if request.url.path.startswith("/api/"):
            response.headers["X-Agnes-Latest-Version"] = APP_VERSION
            response.headers["X-Agnes-Min-Version"] = MIN_COMPAT_CLI_VERSION
            response.headers["X-Agnes-Accepts"] = SERVER_CAPABILITIES
```

and extend the existing `from app.version import ...` import in `app/main.py` with `SERVER_CAPABILITIES` (find it with `grep -n "MIN_COMPAT_CLI_VERSION" app/main.py` — reuse that import line).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_upload_api.py::TestSessionGzipCapability -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/version.py app/main.py tests/test_upload_api.py
git commit -m "feat(upload): advertise session-gzip capability on /api/* responses"
```

---

### Task 2: Server gzip decompression path

**Files:**
- Modify: `app/api/upload.py` (new helper `_stream_to_temp_gunzip`, filename handling in `upload_session`)
- Test: `tests/test_upload_api.py` (new class `TestSessionGzipUpload`)

**Interfaces:**
- Consumes: nothing from Task 1 (independent — the endpoint accepts `.gz` regardless of the advertisement).
- Produces: `POST /api/upload/sessions` accepts a multipart part named `<name>.jsonl.gz` containing gzip bytes; stores decompressed content under `<name>.jsonl`; returns `{"status": "ok", "filename": "<name>.jsonl", "size": <decompressed bytes>}`. Errors: 400 `invalid_gzip` for corrupt/truncated streams, 413 for decompressed size over cap, 400 for filenames invalid after stripping `.gz`. Task 3's client relies on exactly these semantics.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_upload_api.py` (the file already has `import io`, `import pytest` and the `_auth` helper at the top; add `import gzip` and `import zlib` to the imports):

```python
class TestSessionGzipUpload:
    def _post(self, seeded_app, name, body):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        return c.post(
            "/api/upload/sessions",
            files={"file": (name, io.BytesIO(body), "application/gzip")},
            headers=_auth(token),
        )

    def test_gzip_roundtrip_stores_decompressed_jsonl(self, seeded_app):
        content = b'{"type": "message", "text": "hello"}\n' * 50
        resp = self._post(seeded_app, "sess_gz1.jsonl.gz", gzip.compress(content))
        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == "sess_gz1.jsonl"  # .gz stripped
        assert data["size"] == len(content)          # decompressed size
        # Stored file is byte-identical plain JSONL (admin user id is "admin1").
        from app.utils import get_data_dir
        stored = get_data_dir() / "user_sessions" / "admin1" / "sess_gz1.jsonl"
        assert stored.read_bytes() == content

    def test_corrupt_gzip_rejected_400(self, seeded_app):
        resp = self._post(seeded_app, "sess_gz2.jsonl.gz", b"not gzip at all")
        assert resp.status_code == 400
        assert "invalid_gzip" in resp.text

    def test_truncated_gzip_rejected_400(self, seeded_app):
        full = gzip.compress(b'{"type": "message"}\n' * 100)
        resp = self._post(seeded_app, "sess_gz3.jsonl.gz", full[: len(full) // 2])
        assert resp.status_code == 400
        assert "invalid_gzip" in resp.text

    def test_zip_bomb_rejected_413(self, seeded_app):
        # ~55 MB of zeros compresses to ~55 KB — decompressed cap must fire.
        bomb = gzip.compress(b"\x00" * (55 * 1024 * 1024))
        assert len(bomb) < 1024 * 1024  # sanity: transfer size is tiny
        resp = self._post(seeded_app, "sess_gz4.jsonl.gz", bomb)
        assert resp.status_code == 413
        from app.utils import get_data_dir
        assert not (get_data_dir() / "user_sessions" / "admin1" / "sess_gz4.jsonl").exists()

    def test_gz_only_filename_rejected_400(self, seeded_app):
        resp = self._post(seeded_app, ".gz", gzip.compress(b"x"))
        assert resp.status_code == 400

    def test_plain_jsonl_path_unchanged(self, seeded_app):
        content = b'{"type": "message"}\n'
        resp = self._post(seeded_app, "sess_plain.jsonl", content)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "filename": "sess_plain.jsonl", "size": len(content)}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_upload_api.py::TestSessionGzipUpload -v`
Expected: `test_plain_jsonl_path_unchanged` PASSES (regression guard); the gzip tests FAIL — today the server stores the compressed bytes under the `.gz` name, so `data["filename"]` is `sess_gz1.jsonl.gz` and the roundtrip assert breaks.

- [ ] **Step 3: Implement**

In `app/api/upload.py`, add `import zlib` to the imports, then add below `_stream_to_temp`:

```python
async def _stream_to_temp_gunzip(file: UploadFile) -> tuple[tempfile.NamedTemporaryFile, int]:
    """Stream-decompress a gzip upload with the size cap on DECOMPRESSED bytes.

    Zip-bomb guard: `MAX_UPLOAD_SIZE` binds on the decompressor's output, not
    the transfer size — a few KB on the wire must not expand into gigabytes
    on disk. The raw-transfer counter stays as a second bound. Corrupt or
    truncated streams (zlib error, or EOF before the gzip trailer) are a 400
    `invalid_gzip`: deterministic, so the client files them as permanent
    failures instead of retrying.
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tmp")
    decomp = zlib.decompressobj(wbits=31)  # 31 = gzip container
    total = 0      # decompressed bytes (the capped quantity)
    raw_total = 0  # compressed transfer bytes (secondary bound)
    try:
        while True:
            chunk = await file.read(_CHUNK_SIZE)
            if not chunk:
                break
            raw_total += len(chunk)
            if raw_total > MAX_UPLOAD_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)",
                )
            try:
                out = decomp.decompress(chunk)
            except zlib.error:
                raise HTTPException(status_code=400, detail="invalid_gzip")
            total += len(out)
            if total > MAX_UPLOAD_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"Decompressed content too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)",
                )
            tmp.write(out)
        try:
            out = decomp.flush()
        except zlib.error:
            raise HTTPException(status_code=400, detail="invalid_gzip")
        total += len(out)
        if total > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"Decompressed content too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)",
            )
        tmp.write(out)
        if not decomp.eof:
            # Stream ended before the gzip trailer — truncated upload.
            raise HTTPException(status_code=400, detail="invalid_gzip")
        tmp.flush()
    except BaseException:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise
    tmp.seek(0)
    return tmp, total
```

In `upload_session`, replace the filename/streaming section (currently: regex check → `sessions_dir` → `filename = file.filename` → `target` → `tmp, size = await _stream_to_temp(file)`) with:

```python
    if not _FILENAME_RE.match(file.filename or ""):
        raise HTTPException(
            status_code=400,
            detail="filename must match [A-Za-z0-9._-]{1,200}",
        )

    # A `.gz` suffix means the body is gzip-compressed (client capability
    # `session-gzip`). The stored name strips the suffix so the on-disk
    # corpus stays plain JSONL for every downstream reader.
    filename = file.filename  # already validated by regex above
    is_gzip = filename.endswith(".gz")
    if is_gzip:
        filename = filename[: -len(".gz")]
        if not _FILENAME_RE.match(filename):
            raise HTTPException(
                status_code=400,
                detail="filename must match [A-Za-z0-9._-]{1,200} before .gz",
            )

    sessions_dir = _get_data_dir() / "user_sessions" / user_id
    sessions_dir.mkdir(parents=True, exist_ok=True)
    target = sessions_dir / filename

    if is_gzip:
        tmp, size = await _stream_to_temp_gunzip(file)
    else:
        tmp, size = await _stream_to_temp(file)
```

The rest of the handler (move to `target`, audit row, return) is unchanged — `filename` and `size` now naturally carry the stripped name and decompressed size into the audit log and response.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_upload_api.py -v`
Expected: all PASS (new gzip class + every pre-existing upload test).

- [ ] **Step 5: Commit**

```bash
git add app/api/upload.py tests/test_upload_api.py
git commit -m "feat(upload): accept gzip-compressed session transcripts with decompressed-size cap"
```

---

### Task 3: Client — capability probe + gzip compression in `agnes push`

**Files:**
- Modify: `cli/commands/push.py` (new helper `_server_accepts_gzip`, extend `_upload_one`, wire into the real-run loop)
- Test: `tests/test_cli_push.py` (new tests at the end of the file)

**Interfaces:**
- Consumes: `X-Agnes-Accepts` header containing `session-gzip` (Task 1) via `GET /api/health`; server accepting `<sid>.jsonl.gz` parts (Task 2). `cli.client.api_get(path, *, timeout=30.0, **kwargs)` already exists.
- Produces: `_server_accepts_gzip() -> bool` and `_upload_one(transcript: Path, use_gzip: bool = False) -> tuple[bool, dict]` in `cli/commands/push.py`. Env kill-switch `AGNES_PUSH_NO_GZIP=1`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_push.py` (the file already defines `runner`, `_FakeResp`, `_stub_config`, `_stub_sessions`; add `import gzip` and `import os` to its imports). The helpers below patch `api_get` for the probe and record `api_post` calls including payload bytes:

```python
class _FakeProbeResp:
    def __init__(self, caps: str | None) -> None:
        self.status_code = 200
        self.headers = {} if caps is None else {"X-Agnes-Accepts": caps}


def _record_upload_bodies(monkeypatch) -> list[tuple[str, str, bytes]]:
    """Patch api_post to record (path, part_filename, part_bytes) and succeed."""
    calls: list[tuple[str, str, bytes]] = []

    def _fake(path, **kwargs):
        files = kwargs.get("files")
        if files:
            name, buf = files["file"]
            calls.append((path, name, buf.getvalue()))
        else:
            calls.append((path, "", b""))
        return _FakeResp(200)

    monkeypatch.setattr("cli.commands.push.api_post", _fake)
    return calls


def _one_transcript(tmp_path, monkeypatch, content: bytes):
    t = tmp_path / "sess-gz-test.jsonl"
    t.write_bytes(content)
    _stub_config(monkeypatch, tmp_path)
    _stub_sessions(monkeypatch, [t])
    return t


def test_push_gzips_when_server_advertises(tmp_path, monkeypatch):
    content = b'{"type": "message"}\n' * 20
    _one_transcript(tmp_path, monkeypatch, content)
    monkeypatch.setattr("cli.commands.push.api_get", lambda p, **kw: _FakeProbeResp("session-gzip"))
    calls = _record_upload_bodies(monkeypatch)
    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    session_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(session_calls) == 1
    _path, name, body = session_calls[0]
    assert name == "sess-gz-test.jsonl.gz"
    assert gzip.decompress(body) == content  # redaction is a no-op for this content


def test_push_plain_when_capability_absent(tmp_path, monkeypatch):
    content = b'{"type": "message"}\n'
    _one_transcript(tmp_path, monkeypatch, content)
    monkeypatch.setattr("cli.commands.push.api_get", lambda p, **kw: _FakeProbeResp(None))
    calls = _record_upload_bodies(monkeypatch)
    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    _path, name, body = [c for c in calls if c[0] == "/api/upload/sessions"][0]
    assert name == "sess-gz-test.jsonl"
    assert body == content


def test_push_plain_when_probe_fails(tmp_path, monkeypatch):
    content = b'{"type": "message"}\n'
    _one_transcript(tmp_path, monkeypatch, content)

    def _boom(p, **kw):
        raise RuntimeError("server unreachable")

    monkeypatch.setattr("cli.commands.push.api_get", _boom)
    calls = _record_upload_bodies(monkeypatch)
    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    _path, name, body = [c for c in calls if c[0] == "/api/upload/sessions"][0]
    assert name == "sess-gz-test.jsonl"
    assert body == content


def test_push_env_killswitch_skips_probe(tmp_path, monkeypatch):
    content = b'{"type": "message"}\n'
    _one_transcript(tmp_path, monkeypatch, content)
    monkeypatch.setenv("AGNES_PUSH_NO_GZIP", "1")

    def _must_not_probe(p, **kw):
        raise AssertionError("api_get must not be called with AGNES_PUSH_NO_GZIP=1")

    monkeypatch.setattr("cli.commands.push.api_get", _must_not_probe)
    calls = _record_upload_bodies(monkeypatch)
    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    _path, name, body = [c for c in calls if c[0] == "/api/upload/sessions"][0]
    assert name == "sess-gz-test.jsonl"
    assert body == content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli_push.py -k gz -v`
Expected: `test_push_gzips_when_server_advertises` FAILS (`name == "sess-gz-test.jsonl"`, not `.gz` — no compression exists yet). The three fallback tests may already pass (plain is today's behavior) — that is fine; they are regression guards for the fallback matrix.

- [ ] **Step 3: Implement**

In `cli/commands/push.py`:

Add to the imports:

```python
import gzip
import os
```

and extend the existing `from cli.client import api_post` line to:

```python
from cli.client import api_get, api_post
```

Add below the imports (near `_is_permanent_failure`):

```python
_GZIP_CAPABILITY = "session-gzip"


def _server_accepts_gzip() -> bool:
    """One capability probe per push run: does the server accept gzip uploads?

    Fail-open to the legacy plain format — env kill-switch set, probe error,
    or an old server without the `X-Agnes-Accepts` header all mean "no".
    A new client must NEVER send `.gz` to an old server: it would store the
    compressed bytes verbatim and silently corrupt the session corpus.
    """
    if os.environ.get("AGNES_PUSH_NO_GZIP") == "1":
        return False
    try:
        resp = api_get("/api/health", timeout=10.0)
    except Exception:
        return False
    caps = resp.headers.get("X-Agnes-Accepts", "")
    return _GZIP_CAPABILITY in [t.strip() for t in caps.split(",")]
```

Change `_upload_one` to take the flag and compress after redaction:

```python
def _upload_one(transcript: Path, use_gzip: bool = False) -> tuple[bool, dict]:
    """Upload a single session jsonl. Returns (success, error_or_meta).

    The on-disk bytes are redacted (JWT-shaped tokens stripped, #753) into an
    in-memory buffer before upload — transcripts are bounded in size, so
    holding a redacted copy in memory is fine. With ``use_gzip`` the redacted
    buffer is additionally gzip-compressed and the part filename gains a
    ``.gz`` suffix (server capability ``session-gzip``); redaction ALWAYS
    happens first, on the raw bytes. The ledger records the on-disk size
    (see the caller), not this buffer's size.
    """
    if not transcript.exists():
        return False, {"file": transcript.name, "error": "file not found on disk"}
    try:
        raw = transcript.read_bytes()
        payload = redact_bytes(raw)
        name = transcript.name
        if use_gzip:
            payload = gzip.compress(payload)
            name = f"{name}.gz"
        buf = BytesIO(payload)
        resp = api_post("/api/upload/sessions", files={"file": (name, buf)})
    except Exception as exc:
        return False, {"file": transcript.name, "error": str(exc)}
    if resp.status_code == 200:
        return True, {"file": transcript.name}
    return False, {"file": transcript.name, "status": resp.status_code}
```

In the real-run section of `push()`, probe once — only when there is something to upload — and thread the flag through. Replace:

```python
        for sid, p, size in to_upload:
            ok, info = _upload_one(p)
```

with:

```python
        # One capability probe per run, and only when there is work — a
        # no-change push (the common SessionEnd case) makes no extra request.
        use_gzip = bool(to_upload) and _server_accepts_gzip()

        for sid, p, size in to_upload:
            ok, info = _upload_one(p, use_gzip=use_gzip)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli_push.py -v`
Expected: all PASS — the four new tests plus every pre-existing push test (they patch `api_post` only; the probe falls back to plain via the `api_get` failure path, which keeps their recorded filenames `.jsonl`).

- [ ] **Step 5: Commit**

```bash
git add cli/commands/push.py tests/test_cli_push.py
git commit -m "feat(push): gzip session uploads when the server advertises session-gzip"
```

---

### Task 4: CHANGELOG, spec status, full suite

**Files:**
- Modify: `CHANGELOG.md` (bullet under `## [Unreleased]`)
- Modify: `docs/superpowers/specs/2026-07-17-gzip-session-upload-design.md:4` (status line)

**Interfaces:**
- Consumes: Tasks 1–3 merged into the working tree.
- Produces: release-ready branch.

- [ ] **Step 1: Add the CHANGELOG bullet**

Under `## [Unreleased]` in `CHANGELOG.md`, in the `### Changed` group (create the group if absent, keeping the Added/Changed/Fixed/Removed/Internal order used by the surrounding entries):

```markdown
- `agnes push` now gzip-compresses session transcript uploads (~10x smaller transfers) when the server advertises the `session-gzip` capability; older client/server combinations keep the plain format automatically. Escape hatch: `AGNES_PUSH_NO_GZIP=1`.
```

- [ ] **Step 2: Update the spec status line**

In `docs/superpowers/specs/2026-07-17-gzip-session-upload-design.md`, change:

```markdown
**Status:** Draft (verified against the codebase, pending approval)
```

to:

```markdown
**Status:** Implemented (see docs/superpowers/plans/2026-07-17-gzip-session-upload.md)
```

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: green (or only failures reproducible on a clean branch via `git stash` — note those in the PR body, do not fix here).

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md docs/superpowers/specs/2026-07-17-gzip-session-upload-design.md
git commit -m "docs: changelog + spec status for gzip session uploads"
```

---

## Self-Review

- **Spec coverage:** wire format + redaction order (Task 3), server decompression + stored-name stripping + zip-bomb/corrupt handling (Task 2), capability negotiation + fail-open + probe-per-run (Tasks 1+3), escape hatch (Task 3), compatibility matrix (fallback tests in Task 3 + plain-path regression in Task 2), testing section (all mapped), rollout/CHANGELOG (Task 4). Out-of-scope items (local-md, artifacts, incremental) correctly have no tasks.
- **Note on the spec's test list:** the spec names `foo..gz` as invalid-after-strip; `foo.` actually passes `_FILENAME_RE`, so the plan tests the genuinely invalid case (`.gz` → empty name) instead.
- **Type consistency:** `_server_accepts_gzip() -> bool`, `_upload_one(transcript, use_gzip=False)`, `_stream_to_temp_gunzip(file) -> (tempfile, int)`, header `X-Agnes-Accepts`, token `session-gzip` — used identically across tasks.
