# Private sessions: when and how

Agnes uploads your Claude Code session JSONLs to the server so the platform can track usage, power the Most Popular skills view, and let your admin answer questions like "what tools are analysts using most?".

Sometimes you're working on something sensitive and don't want that session uploaded. This guide explains the opt-out.

## The only privacy knob: `agnes mark-private`

Run this command at any point during a Claude Code session:

```bash
agnes mark-private
```

This writes a marker file next to the session JSONL. When `agnes push` runs (either at `SessionEnd` or the next `SessionStart` self-heal), it checks for the marker and **skips the upload for this session entirely**.

The CLI statusline shows 🔒 when the current session is marked private.

## What "private" means

- The JSONL for **this session** is not sent to the server.
- The server never receives the tool calls, messages, or timing data from this session.
- Telemetry counts (skill invocations, Most Popular rankings) are not updated for work done in this session.

## What "private" does NOT mean

- It does **not** affect sessions you've already uploaded. Marking the current session private is not retroactive.
- It does **not** delete anything from the server. If you need a prior session removed, contact your admin.
- It does **not** prevent your queries from going through the server (remote queries still execute server-side). It only controls JSONL upload.
- It does **not** affect other analysts' sessions.

> **Important — `mark-private` is not retroactive.**
> - It prevents the **current** session from being uploaded by `agnes push`.
> - It does **not** remove previously-uploaded sessions from the server. Once a session reaches the server, the `UsageProcessor` will extract its events and admins can access it via `/admin/users/<id>/sessions`.
> - If you need to redact a previously-uploaded session, contact your operator — they can delete the JSONL from `${SESSION_DATA_DIR}/<user>/` **and** run `agnes admin usage reprocess` to wipe extracted events.

## When to use it

- You're exploring sensitive business data and don't want the tool-call trace logged.
- You're debugging a personal issue unrelated to your work.
- You're helping a colleague with something outside your normal workflow.

There is no always-private mode in v1. Each session is opt-in private. If you want every session private by default, talk to your admin — per-user opt-out is on the v2 roadmap.

## Undoing it

There is no `agnes unmark-private`. If you marked a session private by mistake, you cannot push it retroactively. Start a new session and push from there.

## Admin visibility

Your admin cannot see the content of sessions you've marked private — the JSONL is never sent. They can see that you ran sessions (last login time, etc.) but not the tool calls or messages within a private session.
