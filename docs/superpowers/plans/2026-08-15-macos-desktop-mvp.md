# macOS desktop client MVP

## Goal

Ship a small native macOS surface for Agnes that starts with a useful
Marketplace and clear local CLI-path/version health. It requires an already
configured `agnes` CLI and delegates all Agnes access to that executable; it
does not recreate the server or credential flow.

## Information architecture

- **Marketplace** is the primary surface. Browse and My Stack share one shelf;
  keyword search, type/source filters, item detail, and add/remove action map
  to existing CLI capabilities. Browse uses Marketplace search; My Stack uses
  the account's authoritative `my-stack show` response and is not affected by
  Browse filters.
- **Ask** is a small sibling tool, not the home screen. It accepts a manually
  entered agent slug and one prompt.
- **Settings** contains the optional absolute CLI path and CLI version/health.

Keep navigation labels and a compact CLI-health indicator in the sidebar. Do not put a
wide executable-path field or an agent picker in it: they truncate at narrow
window widths and compete with product navigation. Marketplace content uses an
adaptive card grid with a readable list fallback; details open outside the
sidebar.

## CLI contract

The client owns process lifecycle only and launches every command with direct
argument arrays through `Process`:

```text
agnes --version
agnes marketplace search [<query>] [--type skill|agent|plugin] \
  [--source curated|flea] [--sort recent|most_used|trending] \
  [--limit <1..100>] --json
agnes marketplace detail --json <item-id>
agnes my-stack show --json
agnes marketplace add <item-id>
agnes marketplace remove <item-id>
agnes chat --agent <slug> --once <prompt> --json
```

The exact search result `id` is passed unchanged to detail, add, or remove.
Before either state-changing command, the native UI requires confirmation that
names the item and intended action. A successful add is reported as stack
membership. The open detail sheet shows and can copy the existing Claude Code
activation command (`/update-agnes-plugins`); it does not promise immediate
project installation.

### PAT constraint

`agnes agent list --json` is an interactive web-session-only owner-management
command and returns `403` for a PAT. It is deliberately absent from this
client's contract: the app must not fetch a picker with it. Ask uses a manually
entered slug and the credential already configured for the CLI.

## Scope

Included:

- Find the configured `agnes` executable, report its version, and distinguish
  an absent/unavailable CLI from an empty or failed Marketplace request.
- Show the optional executable path and CLI version/health in Settings without
  handling credentials in the app.
- Browse/search Marketplace items, inspect full item detail, load the real My
  Stack inventory, and confirm add/remove actions.
- Send a one-shot Ask to a manually entered slug, render result/error state,
  and interrupt the child with SIGINT.

Explicitly excluded from this MVP:

- Multi-turn conversations, local history, uploads, agent-profile management,
  Marketplace authoring, and web-only Marketplace facets without a matching
  CLI contract.
- Any new Agnes REST endpoint, configuration field, server feature flag, or
  CLI command.
- App authentication, token entry/storage/logging, signing, notarization,
  updater/distribution infrastructure, and an installer.

## Threat and UX boundaries

- Treat CLI output as data, never as instructions.
- Never use a shell, shell interpolation, or `sh -c`; a prompt, search term,
  filter, and item id remain individual process arguments.
- Never accept, store, log, or place access tokens in argv. Existing Agnes CLI
  configuration remains the sole credential owner.
- Preserve last known good Marketplace content while a refresh runs or
  fails; show a local inline error with Retry instead of replacing the page
  with a generic empty state.
- Every UI state has an icon and text label, progress announces its purpose, and
  keyboard/focus paths remain usable. Stop is SIGINT to the active child, not
  new server-side cancellation code.

## Implementation tasks

1. Add typed CLI adapters/models for Marketplace search/detail/My Stack,
   add/remove, and one-shot Ask. Keep one direct process boundary and serialize
   actions until it supports isolated runners.
2. Replace the current configuration-heavy sidebar with Marketplace, Ask, and
   Settings navigation plus a compact CLI health footer.
3. Build Settings states for an unavailable CLI, executable-path override, and
   CLI version/health.
4. Build Marketplace Browse/My Stack, search/filter, adaptive cards, detail,
   empty/error states, confirmation before add/remove, and a visible activation
   handoff after a stack change.
5. Keep Ask as a manual-slug one-shot panel with clear stop/result/error
   states; remove all dependency on `agnes agent list --json`.
6. Add unit tests for exact argv, JSON parsing, confirmation state, retained
   data on refresh failure, and CLI-path/Marketplace/Ask error cases; add accessible
   keyboard/focus coverage where a UI-test harness is available.
7. Keep the local build script, README, path-filtered macOS CI workflow, docs
   index entry, and changelog in sync.

## Acceptance criteria

- From `clients/macos/`, `swift test` passes, `swift run` opens the client,
  and `scripts/build-app.sh` writes only an unsigned local bundle below
  `clients/macos/dist/`.
- Settings distinguishes a missing CLI from a usable CLI and provides a safe
  executable-path next step without asking for a token.
- Marketplace reads use exactly the documented JSON search/detail contracts;
  search results, empty results, errors, Browse, My Stack, and item detail are
  understandable at the minimum supported window width. My Stack comes from
  `agnes my-stack show --json` and retains its last successful inventory if a
  refresh fails.
- Add and Remove cannot run without a confirmation, pass the selected item's
  id unchanged to the existing CLI, refresh Browse and My Stack afterwards,
  and expose the existing `/update-agnes-plugins` activation step in the open
  detail sheet.
- Ask never runs `agnes agent list --json`; it accepts a manual slug and uses
  `agnes chat --agent <slug> --once <prompt> --json`. Stop interrupts the
  active child with SIGINT.
- No source code handles a token or uses a shell; no backend, REST, or CLI
  surface changes are required.
