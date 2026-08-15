# Agnes for macOS (MVP)

Agnes for macOS is a deliberately small, Marketplace-first native client. It
uses an already configured Agnes CLI on the same Mac; it does not manage
accounts, tokens, or server configuration itself.

## What it demonstrates

- Finds `agnes`, shows its version, and gives a clear CLI-path/health state
  when the executable or a Marketplace request is unavailable.
- Browses Marketplace results, searches by keyword/type/source, and shows an
  item's details before any change. **My Stack** loads the account's actual
  stack membership through the CLI instead of filtering the current search.
- Adds or removes a Marketplace item only after an explicit confirmation, then
  shows the existing Claude Code activation step while the detail sheet is open.
- Sends one Ask request to a manually entered agent slug and can stop the
  in-flight CLI process with SIGINT.

The MVP is intentionally not a general chat or administration client: it has
no multi-turn history, uploads, agent-profile management, local
authentication/token storage, signing, notarization, or auto-update mechanism.

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

### PAT constraint

`agnes agent list --json` is an owner-management command that requires an
interactive web session; a PAT receives `403`. The desktop client therefore
never uses it to populate a picker. Ask takes a manually entered agent slug
and runs it through the configured CLI credential. The app neither reads nor
persists that credential.

When opened from Finder, the MVP uses the CLI's default on-disk configuration
under `~/.config/agnes`. Shell-only overrides such as `AGNES_CONFIG_DIR`,
`AGNES_SERVER`, and `AGNES_TOKEN` are inherited only when the app is launched
from that shell; the desktop app never stores them.

## Marketplace actions

The client invokes existing CLI commands directly, with argument arrays and no
shell. Read operations use JSON so the native UI can render their data:

```text
agnes marketplace search [<query>] [--type skill|agent|plugin] \
  [--source curated|flea] [--sort recent|most_used|trending] \
  [--limit <1..100>] --json
agnes marketplace detail --json <item-id>
agnes my-stack show --json
```

The item's `id` from search is passed unchanged to the state-changing command:

```text
agnes marketplace add <item-id>
agnes marketplace remove <item-id>
```

Add and Remove always show a native confirmation naming the item and action
before the CLI is invoked. A successful add means the item is in the Agnes
stack. The result remains visible in the open detail sheet and the app can copy
`/update-agnes-plugins`, the existing Claude Code activation step; it does not
claim that stack membership alone installs a plugin into a project.

Ask uses the CLI's unambiguous agent escape hatch:

```text
agnes chat --agent <slug> --once <prompt> --json
```

CLI output is treated as data, not instructions. The app never uses `sh -c`,
does not interpolate a shell command, and does not log secrets.

## Run locally

From this directory:

```bash
swift test
swift run
```

`swift run` launches the development executable. Ensure the same shell can
find `agnes` on `PATH`; the app also checks common Homebrew locations and lets
you enter an absolute executable path. Its unavailable state explains what to
fix if it cannot find or query the CLI.

## Build a local app bundle

```bash
scripts/build-app.sh
```

The script writes an unsigned local `.app` bundle under `clients/macos/dist/`.
It is intended for development and manual evaluation only; signing,
notarization, installer packaging, and distribution are outside this MVP.
