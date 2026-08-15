# macOS desktop MVP v2 — CLI-backed Agent Runs

## Goal

Turn the v1 one-shot Ask panel into an honest, inspectable Agent Runs cockpit
without bypassing the Agnes CLI or inventing capabilities that its public
machine-readable contract does not provide.

Marketplace remains the discovery/install surface. Agent Runs demonstrates
the execution architecture needed for a richer desktop client: request-scoped
process ownership, structured event inspection, local lifecycle state, and
agent usage/budget visibility.

## Verified CLI boundary

The implementation uses only existing JSON-capable commands:

```text
agnes chat --agent <slug> --once <prompt> --json
agnes agent usage <slug> --json
```

`chat --once --json` returns one complete AG-UI event array and attempts
best-effort session cleanup when the process exits. It exposes no reconnectable
session handle, is not a multi-turn transport, and the UI labels every prompt
as an isolated run.

Two missing CLI capabilities are intentionally tracked outside this desktop
PR:

- [#1344](https://github.com/keboola/agnes-the-ai-analyst/issues/1344) —
  PAT-safe runnable-agent discovery with a stable JSON envelope.
- [#1345](https://github.com/keboola/agnes-the-ai-analyst/issues/1345) —
  machine-readable create/send/stream/cancel/delete session lifecycle.

The desktop must not call Agnes HTTP APIs directly to work around either gap.

## Architecture

1. `AgnesCLIProviding` is the typed capability boundary. UI and state code do
   not construct subprocesses or parse JSON.
2. `SystemAgnesCLIProcessRunner` owns a dictionary of subprocesses keyed by a
   desktop-generated request UUID. Marketplace reads can overlap an agent run;
   cancellation targets one UUID and termination cancels all owned children.
   It continuously drains both pipes while retaining at most 8 MiB stdout and
   1 MiB stderr per child; overflow is an explicit truncated/error state.
3. `AgnesCLIOutputParser` converts the complete AG-UI array into an
   `AgentRunResult` while retaining pretty-printed raw JSON. Structured
   `RUN_ERROR` and missing terminal events remain inspectable failed/truncated
   outcomes instead of disappearing into a generic exception.
4. `AppModel` owns local run lifecycle and selection. Run records are in
   memory only and do not claim server durability or context reuse.
5. `ChatWorkspaceView` is a three-pane cockpit: local run list and composer,
   transcript, and an inspector for run metrics, usage/runtime contract, and
   raw events.

## Scope delivered

- Manual agent slug and persistent composer with `⌘↩` run shortcut.
- One visible active run with request-scoped `⌘.` stop.
- Local run history, lifecycle, prompt, answer, duration, tools, event count,
  error/truncation details, and raw AG-UI JSON.
- Agent token usage and optional budget remaining through the CLI.
- Concurrent Marketplace refresh while an agent subprocess is active.
- Explicit UI links to the two CLI gap issues and a read-only JSON view of the
  effective subprocess contract.
- Unit coverage for argv safety, parsing, structured failures, truncation,
  request-scoped cancellation, usage decoding, state transitions, and
  Marketplace/run concurrency.

## Explicitly deferred

- Agent picker and metadata until #1344 is implemented.
- Durable multi-turn transcript, reconnect, live streaming, server-side
  cancellation, and session history until #1345 is implemented.
- Agent-as-code authoring, approvals, artifacts/evidence, and managed local
  Docker sandbox. Those releases are specified in the Agent Cockpit PRD.
- Credential ownership, signing, notarization, updater, and distribution.

## Acceptance criteria

- `swift test --disable-sandbox` passes from `clients/macos`.
- `scripts/build-app.sh` creates the unsigned local app bundle.
- Every prompt is passed as one direct `Process` argument and no shell is used.
- Stop cannot interrupt a concurrent Marketplace subprocess or a later run.
- `RUN_ERROR` and truncated streams retain their partial answer and events.
- CLI output above the retained byte limits is surfaced and cannot grow the
  app's retained memory without a bound.
- The UI never suggests that local run records share server-side context.
- README, docs index, changelog, desktop issue, and PR explain the same CLI
  contract and deferred gaps.
