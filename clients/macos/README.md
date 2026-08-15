# Agnes for macOS (MVP v2)

Agnes for macOS is a native, CLI-backed Marketplace and Agent Runs cockpit. It
uses an already configured Agnes CLI on the same Mac; it does not manage
accounts, tokens, or server configuration itself.

## What it demonstrates

- Finds `agnes`, shows its version, and gives a clear executable-path/health
  state when the CLI or a request is unavailable.
- Browses Marketplace results, searches by keyword/type/source, inspects an
  item, and confirms add/remove actions. **My Stack** comes from the account's
  authoritative CLI response rather than filtering the current search.
- Runs a manually entered agent slug as an isolated one-shot task and retains a
  local, in-memory run record with answer, lifecycle, tool activity, duration,
  raw AG-UI JSON, token usage, and budget.
- Renders agent Markdown as native selectable macOS content: headings, inline
  emphasis/code/links, lists, quotes, fenced code blocks, dividers, and
  horizontally scrollable tables.
- Keeps Marketplace and agent execution in separate process lanes. Stop sends
  SIGINT only to the selected run's subprocess, not to an unrelated CLI read.
- Shows the exact, read-only CLI runtime contract in the UI so the current
  limitations remain inspectable.

The MVP is intentionally not a general chat or administration client. It has
no durable/multi-turn sessions, agent picker, uploads, profile management,
local Docker sandbox, credential storage, signing, notarization, or updater.
The longer product direction is captured in the
[Agent Cockpit PRD](../../docs/superpowers/specs/2026-08-15-agnes-desktop-agent-cockpit-prd.md).

## Prerequisites

- macOS with Xcode or the Xcode Command Line Tools (Swift 6).
- Agnes CLI installed and configured for an Agnes instance. Verify the CLI
  before launching the app:

  ```bash
  agnes --version
  agnes marketplace search --limit 1 --json
  agnes my-stack show --json
  ```

The app reuses that CLI configuration. Do not pass a token to the app or put a
token in a command line.

### Current CLI gaps

`agnes agent list --json` is an owner-management command that requires an
interactive web session; a PAT receives `403`. The desktop therefore never
uses it as a runnable-agent picker. The user enters a slug manually. A stable,
PAT-safe discovery command is tracked in
[#1344](https://github.com/keboola/agnes-the-ai-analyst/issues/1344).

`agnes chat --agent <slug> --once <prompt> --json` emits a complete AG-UI event
array only after the child process exits. The CLI then attempts best-effort
server-session cleanup, but exposes no reconnectable session handle. Each
desktop prompt is consequently an honest isolated run; local run records do
not imply shared model context. A machine-readable multi-turn lifecycle is tracked in
[#1345](https://github.com/keboola/agnes-the-ai-analyst/issues/1345).

When opened from Finder, the MVP uses the CLI's default on-disk configuration
under `~/.config/agnes`. Shell-only overrides such as `AGNES_CONFIG_DIR`,
`AGNES_SERVER`, and `AGNES_TOKEN` are inherited only when the app is launched
from that shell; the desktop app never stores them.

## CLI contract

The client invokes existing commands directly with argument arrays and no
shell. Read operations use JSON so the native UI can render their data:

```text
agnes marketplace search [<query>] [--type skill|agent|plugin] \
  [--source curated|flea] [--sort recent|most_used|trending] \
  [--limit <1..100>] --json
agnes marketplace detail --json <item-id>
agnes my-stack show --json
agnes agent usage <agent-slug> --json
agnes chat --agent <agent-slug> --once <prompt> --json
```

The item's `id` from search is passed unchanged to the state-changing command:

```text
agnes marketplace add <item-id>
agnes marketplace remove <item-id>
```

Add and Remove always show a native confirmation naming the item and action.
After a successful add, the detail sheet can copy `/update-agnes-plugins`, the
existing Claude Code activation step; stack membership is not presented as
immediate project installation.

CLI output is treated as data, never as instructions. The process adapter keeps
draining both pipes but retains at most 8 MiB stdout and 1 MiB stderr per
command; an overflow is surfaced as an incomplete/error state instead of
growing desktop memory without a bound. The app never uses
`sh -c`, does not interpolate a shell command, and does not log secrets. Raw
run JSON is displayed only for inspection and remains in memory for the life
of the app process.

## Run locally

From this directory:

```bash
swift test --disable-sandbox
swift run
```

`swift run` launches the development executable. Ensure the same shell can
find `agnes` on `PATH`; the app also checks common Homebrew locations and lets
you enter an absolute executable path.

## Build a local app bundle

```bash
scripts/build-app.sh
```

The script writes an unsigned local `.app` bundle under `clients/macos/dist/`.
It is intended for development and manual evaluation only; signing,
notarization, installer packaging, and distribution remain out of scope.
