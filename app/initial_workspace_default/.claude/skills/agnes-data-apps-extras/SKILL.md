---
name: agnes-data-apps-extras
description: Agnes-specific cadence for AI-authored hosted data apps — scaffold-first, draft-branch discipline, in-chat preview, and promote flow. Use whenever a chat user asks you to build, edit, or ship a dashboard/web app; loads alongside the general dataapp-development skill, never instead of it.
---

# Agnes data-apps extras

This skill is the **Agnes-specific overlay** on top of the general
`dataapp-development` skill (vendored from the upstream `keboola/ai-kit`
marketplace). Load both — `dataapp-development` owns the `keboola-config/`
runtime contract, port wiring (nginx `:8888` in front of the app on `:3000`),
storage-access patterns, and troubleshooting; this skill never re-derives any
of that. This skill only adds: which scaffold to start from, which git branch
to touch, how to drive the in-chat preview, and how Agnes deploys/promotes
differ from the other harnesses `dataapp-development` already knows about.

If you haven't loaded `dataapp-development` yet, load it now. If you're
unsure which deployment path applies, see `references/path-d.md` — Agnes is
detected by the presence of the `data_app_*` MCP tools.

## 1. Scaffold-first, custom-code-second

Never start from a blank repo. `cp -R` the baked scaffold at
`scaffolds/nodejs-dashboard/` (sibling of this skill's session workspace)
into the app's managed repo **before** writing a single line of real code,
then immediately call `data_app_deploy(draft_slug, mode="dev")`. This boots
the container and warms `npm install` while you write the real
`src/App.tsx` / `server/index.ts` — HMR picks up your edits from there. Do
not wait for "real" code before the first deploy; a cold first deploy racing
`npm install` is exactly the failure mode this cadence avoids.

## 2. Draft-branch discipline

Every app-code change happens on the draft's pinned branch — **never** push
to `main`. `main` only gains code at promote time (see
`references/promote-flow.md`). If you need to resume work on an existing
draft, mint a fresh credential with `data_app_git_credential(slug)` rather
than reusing a stale one.

## 3. Preview cadence

Call `agnes_data_app_preview(slug)` with no `url` as soon as the dev deploy
starts — this opens a placeholder pane immediately so the user isn't staring
at nothing. Hold the **live** call (`agnes_data_app_preview(slug,
url="/apps/<draft_slug>/")`) until the real dashboard is pushed *and* the dev
deploy is healthy: poll `data_app_get(draft_slug)` in short steps (≤5s), not
one long sleep. Once the live preview is up, ask the user via
`AskUserQuestion`: **"Publish" or "Make changes"** — and stop there. Never
promote on your own initiative; promotion is always an explicit user choice.
If the user asks for changes, keep iterating on the draft branch and re-open
the live preview when ready. If they publish, follow
`references/promote-flow.md`.

Use `agnes_data_app_refresh(slug)` after a dependency change or a `mode=dev`
redeploy (HMR does not pick those up); ordinary `src/**`/`server/**` edits
don't need it. Call `agnes_data_app_close(slug)` before tearing down a draft
(`data_app_delete_draft`) so the pane never points at a deleted app.

## 4. Visual-quality bar, chat voice, jargon ban

- Real React + Vite + Tailwind. Charting libraries come from npm
  (`package.json`), never a CDN `<script>` tag.
- Chat messages are 1–2 short sentences. No walls of text.
- Never leak implementation jargon to the user: say "setting up your app",
  not "pushing the scaffold to the draft branch"; say "app is ready to look
  at", not "dev deploy reports healthy". Git, HMR, supervisord, and deploy
  mechanics are your concern, not the user's.

## 5. Debugging

The only observable signal is `data_app_logs(slug)`. Use it to explain
failures in terms of row counts, redacted SQL, and which code path ran —
never surface cell values or PII from the logs back to the user.

## 6. Context persistence

The managed repo's root `CLAUDE.md` carries one maintained section:
`# App context (maintained by Agnes)` (Purpose / Data sources / Key
decisions / Iterate safely — see the scaffold's `CLAUDE.md.tmpl` for the
skeleton). Write/update it at promote time; read it back at the start of any
fresh conversation that edits an existing app, so you inherit prior
decisions instead of re-deriving them.

## 7. Reading Agnes data

The scaffold's `server/agnesQuery.ts` wraps the app's injected
`AGNES_TOKEN` / `AGNES_URL` env vars against the Agnes REST API
(`runQuery(sql)` over `/api/query`, plus catalog/table lookups). See
`references/agnes-query.md` for the exact helper shape. This is the *only*
way the app reads Agnes data — never hardcode credentials, never bypass the
owner-scoped token.

## References

- `references/path-d.md` — how Agnes shows up as a deployment path in the
  general skill's detection logic (interim overlay pending upstream merge).
- `references/agnes-query.md` — the `agnesQuery.ts` helper contract.
- `references/promote-flow.md` — the exact draft→prod git sequence.
