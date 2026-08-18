---
name: Local Agnes Chat UI testing
description: How to run the Agnes FastAPI server locally so the chat UI and onboarding rail render without real auth or LLM/E2B credentials.
---

# Local Agnes Chat UI testing

Use this when you need to test `/chat`, the onboarding rail/panel, or any UI that depends on `can_chat`.

## Start the server

```bash
cd <install-dir>   # your local checkout of this repository
LOCAL_DEV_MODE=1 TESTING=1 AGNES_CHAT_ENABLED=true .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9000
```

- `LOCAL_DEV_MODE=1` auto-authenticates every request as `dev@localhost` (admin).
- `TESTING=1` bypasses the `JWT_SECRET_KEY`, `ANTHROPIC_API_KEY`, and `E2B_API_KEY` startup checks.
- `AGNES_CHAT_ENABLED=true` enables chat; without it the rail panel is never rendered.
- No `instance.yaml` is required; the app falls back to built-in defaults.

## Enable the onboarding rail panel

The rail row is gated by `can_chat = chat_config.enabled && has_explicit_grant(user["id"], "chat", "chat")`. In local dev the dev user belongs to the seeded `Admin` and `Everyone` groups, but there is no `chat` grant by default. Create one via the admin REST API after startup:

```bash
curl -s http://127.0.0.1:9000/api/admin/groups
# Find the `Everyone` group id, then:
curl -X POST http://127.0.0.1:9000/api/admin/grants \
  -H 'Content-Type: application/json' \
  -d '{"group_id": "<everyone-group-id>", "resource_type": "chat", "resource_id": "chat"}'
```

Without this grant the "Set up Agnes" row will not appear in the rail and `chat_onboarding.js` cannot be exercised end-to-end.

## Useful checks

- `GET http://127.0.0.1:9000/api/chat/journey` returns the current onboarding state.
- `PUT http://127.0.0.1:9000/api/chat/journey` with any subset of the journey flags updates it; all six flags set to `true` marks onboarding as complete (`6/6`), and setting all to `false` plus `onboarded: false` re-arms it.
- The local dev user is `dev@localhost`; the UI profile menu contains "Start over onboarding" once the checklist has been completed or skipped.

## Browser notes

Chrome launched with `--no-sandbox` may show an unsupported command-line banner; it does not affect the test. The page can be driven at `http://127.0.0.1:9000/chat`. The rail's "Set up Agnes" popover opens via `#rail-getstarted-toggle` and is rendered by `chat_onboarding.js`.

## Devin Secrets Needed

None for this local-only flow.
