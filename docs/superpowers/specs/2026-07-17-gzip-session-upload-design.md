# Gzip-compressed session transcript uploads — design

**Date:** 2026-07-17
**Status:** Implemented (see docs/superpowers/plans/2026-07-17-gzip-session-upload.md)

## Problem

`agnes push` uploads every new/grown Claude Code session transcript to
`POST /api/upload/sessions` as raw JSONL (`cli/commands/push.py`,
`app/api/upload.py`). Transcripts are highly redundant text (repeated JSON
keys, tool schemas, file snapshots) and typically compress ~10× with gzip.
Uploading them uncompressed wastes bandwidth on the analyst side (hotel
Wi-Fi, metered links, VPNs) and transfer volume on the server side.

This is an efficiency improvement, not a scalability fix: the server ingest
already streams to a temp file in 64 KB chunks under a 50 MB cap, so CPU and
memory are not a concern today. The only meaningful lever left is transfer
size — hence compression.

## Goal

Compress the session transcript payload with gzip on the client when — and
only when — the server is known to accept it, with byte-identical stored
results and no behavior change for any old client or old server.

Non-goals:

- Compressing the `CLAUDE.local.md` upload (`POST /api/upload/local-md`, a
  small JSON body; whole-request `Content-Encoding` decompression would need
  a Starlette middleware and is not worth it here).
- Compressing `POST /api/upload/artifacts` (can adopt the same mechanism
  later if ever needed; out of scope to keep the change reviewable).
- Incremental/append uploads (rejected separately: redaction changes byte
  offsets and a token spanning a chunk boundary would evade the redaction
  regex — a security-relevant edge case with low ROI).
- Any change to redaction, the upload ledger, dedup semantics, or storage
  layout.

## Design

### Wire format

The multipart upload shape stays identical; only the file part changes:

- **Filename:** `<session_id>.jsonl.gz` (today: `<session_id>.jsonl`).
- **Content:** `gzip(redact_bytes(raw))` — redaction stays first, applied to
  the raw on-disk bytes exactly as today (`cli/commands/push.py`
  `_upload_one`), then the redacted buffer is gzip-compressed in memory.
  Transcripts are size-bounded, so an in-memory compressed copy is fine (the
  redacted copy is already held in memory today).

No new endpoint, no `Content-Encoding` header games: the `.gz` suffix on the
part filename is the whole protocol. This keeps the change visible in audit
logs and trivially debuggable with `curl`.

### Server: decompression in `/api/upload/sessions`

In `app/api/upload.py`:

1. If the (regex-validated) filename ends with `.gz`, strip the suffix and
   re-validate the remaining name against `_FILENAME_RE`. The **stored**
   filename is the stripped one — the on-disk corpus stays pure JSONL and
   every downstream consumer (memory mining, admin session views) is
   unaffected.
2. Stream-decompress while writing to the temp file: a sibling of
   `_stream_to_temp` that pipes each 64 KB chunk through
   `zlib.decompressobj(wbits=31)` (gzip container) and counts
   **decompressed** bytes against `MAX_UPLOAD_SIZE`.
   - Decompressed total over the cap → HTTP 413, temp file unlinked. This is
     the zip-bomb guard: the cap must bind on output bytes, not transfer
     bytes. (Compressed input is implicitly capped too — keep the existing
     raw-byte counter as a second bound.)
   - Truncated or corrupt gzip stream (`zlib.error`, or EOF before the
     decompressor reports completion) → HTTP 400 with detail
     `invalid_gzip`. 400 is deliberate: the client's permanent-failure
     classifier (`_is_permanent_failure`) files it to the forensic
     failed-log instead of retrying a payload that will never parse.
3. Everything after the temp file (move into
   `user_sessions/<user_id>/`, audit row) is unchanged; the audit `bytes`
   field records the decompressed (stored) size.

Plain `.jsonl` uploads take the existing code path untouched.

### Capability negotiation (the correctness core)

A new client must never send `.gz` to an old server: the current handler
would happily store the compressed bytes under `<sid>.jsonl.gz`, silently
corrupting the session corpus for every downstream reader. Version-header
infrastructure already exists (`app/version.py` middleware emits
`X-Agnes-Latest-Version` / `X-Agnes-Min-Version` on responses; the CLI reads
them in `cli/client.py` `_check_version_headers`), so we extend it:

- **Server** adds one response header in the same middleware:
  `X-Agnes-Accepts: session-gzip`. A comma-separated capability list, so
  future opt-in wire changes reuse the header instead of minting version
  arithmetic.
- **Client** (`agnes push`): once per push run, before the upload loop,
  issue a single `GET /api/health` and read `X-Agnes-Accepts`.
  - Header contains `session-gzip` → compress this run's uploads.
  - Header absent (old server) or the probe fails for any reason → upload
    plain. Fail-open to the legacy format, never the other way around.
  - The probe result is held in-process for the run only — no config cache
    to go stale across server upgrades/downgrades.

Explicit capability advertisement is chosen over "compare
`X-Agnes-Latest-Version` ≥ first-shipping-version" because it survives
backports, forks, and downgraded servers without the client embedding a
version table.

### Escape hatch

Environment variable `AGNES_PUSH_NO_GZIP=1` forces plain uploads regardless
of the capability probe. No new CLI flag: this is an operational kill
switch, not a user-facing choice (per the command-UX standard, we do not add
new boolean flags for internals).

### Compatibility matrix

| Client | Server | Behavior |
|---|---|---|
| new | new | probe sees `session-gzip` → gzip upload, server stores decompressed JSONL |
| new | old | probe sees no capability header → plain upload (today's path) |
| old | new | plain upload, untouched code path |
| old | old | unchanged |

### What deliberately does not change

- **Redaction** order and scope (`cli/lib/transcript_redact.py`): applied to
  raw bytes before compression; compression is transparent to it.
- **Upload ledger** (`cli/lib/upload_log.py`): still records the on-disk
  raw size — dedup/grow detection is independent of the wire encoding.
- **Idempotency**: server still overwrites by (stripped) filename; re-upload
  of a grown transcript behaves exactly as today.
- **No DB or schema change** — no migration, no repository work, no
  DuckDB↔Postgres parity surface.

## Testing

Server (`tests/` next to existing upload tests):

- Gzip round-trip: upload `sid.jsonl.gz`, assert stored file is
  `user_sessions/<uid>/sid.jsonl` with byte-identical decompressed content
  and a correct audit `bytes` value.
- Zip bomb: small compressed body expanding past `MAX_UPLOAD_SIZE` → 413,
  no temp file left behind, nothing stored.
- Corrupt/truncated gzip → 400 `invalid_gzip`, nothing stored.
- `.gz`-only filename (`.gz`, `foo..gz` → invalid after strip) → 400.
- Plain `.jsonl` regression: existing tests keep passing unmodified.

Client:

- Probe advertises `session-gzip` → request body is gzip, part filename has
  the `.gz` suffix.
- Probe header absent / probe request raises → plain upload.
- `AGNES_PUSH_NO_GZIP=1` → plain upload even when advertised.
- 400 on a gzip upload lands in the permanent-failure log (existing
  `_is_permanent_failure` behavior, asserted for the new path).

## Rollout

Single PR: server capability header + decompression path, client probe +
compression, tests, CHANGELOG bullet under `[Unreleased]` (Changed). Ships
in whatever patch version the PR earns; no coordination needed — the
capability probe makes deployment order irrelevant.
