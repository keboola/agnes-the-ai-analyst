# Observability — PostHog integration

Optional integration that wires four signals into a single PostHog project:

1. **Backend exceptions** — every unhandled FastAPI exception, plus rebuild
   failures from `src/orchestrator.py` and HTTP-job failures from
   `services/scheduler/`.
2. **LLM tracing** — every Anthropic / OpenAI-compat call emits a
   `$ai_generation` event with provider, model, latency, and token counts.
3. **Frontend errors + pageviews** — `window.error` /
   `unhandledrejection` forwarded via `posthog.captureException`; automatic
   `$pageview` and `$pageleave`.
4. **Session replay (masked) + feature flags** — both gated behind the same
   single `POSTHOG_API_KEY`.

The integration ships **off by default**. Setting one environment variable
turns everything on.

## Enabling the integration

```bash
# Required — the only switch that controls on/off.
# Use a PROJECT key (publishable phc_…), never a personal API key.
POSTHOG_API_KEY=phc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

That's the entire minimum. Defaults will:

- Send to `https://eu.i.posthog.com` (override with `POSTHOG_HOST`).
- Identify logged-in users by id + email (override with `POSTHOG_IDENTIFY_PII`).
- Record session replay with all inputs and known data surfaces masked
  (override with `POSTHOG_REPLAY=false` or
  `POSTHOG_REPLAY_MASK_SELECTOR=…`).
- Skip prompt / completion bodies in LLM events; emit token counts + latency
  only (override with `POSTHOG_LLM_PAYLOADS=1` if you accept the privacy
  trade-off — LLM prompts in this product routinely include customer SQL
  and data).

## All knobs

| Variable | Default | Notes |
|---|---|---|
| `POSTHOG_API_KEY` | unset | **The on/off switch.** Unset = integration is fully off. Project key only. |
| `POSTHOG_HOST` | `https://eu.i.posthog.com` | Full URL. Use `https://us.i.posthog.com` for the US region or your own host. |
| `POSTHOG_IDENTIFY_PII` | `email` | `none` / `id` / `email` / `full`. |
| `POSTHOG_REPLAY` | `true` | Disable replay only, keeping errors / events / flags. |
| `POSTHOG_REPLAY_MASK_SELECTOR` | empty | CSS selector appended to the default mask list. |
| `POSTHOG_LLM_PAYLOADS` | `0` | `1` adds `$ai_input` + `$ai_output_choices` to LLM events. Off by default. |

## Privacy posture

- The PostHog **project key** is publishable — it's safe in browser HTML.
  PostHog uses a separate **personal API key** for admin operations. This
  integration only ever exposes the project key. Treat the personal key like
  any other secret and never set it as `POSTHOG_API_KEY`.
- Session replay defaults: `maskAllInputs: true`, plus a CSS-selector mask
  for known data-bearing classes (`.data-cell`, `.query-result`,
  `.sql-output`, plain `<code>` and `<pre>`, and any element marked
  `data-sensitive`). Add your own with `POSTHOG_REPLAY_MASK_SELECTOR`.
- LLM payloads are **off by default** because the prompts and completions
  in this product include customer SQL, query results, and table samples.
  Token counts and latency are always sent (no payload contents in them).
- `person_profiles: 'identified_only'` — anonymous visits do not create
  person records.

## Where the events come from

| Event | Code path |
|---|---|
| `$exception` (unhandled 500) | `app/main.py:_unhandled_exception_handler` |
| `$exception` (orchestrator rebuild) | `src/orchestrator.py:_capture_orchestrator_exception` |
| `$exception` (scheduler job) | `services/scheduler/__main__.py:_call_api` |
| `$exception` (CLI uncaught) | `cli/main.py:main` |
| `$ai_generation` | `src/observability/llm_tracing.py:trace_generation` wrapped at `connectors/llm/anthropic_provider.py:_attempt_extraction` and `connectors/llm/openai_compat.py` |
| `$pageview`, `$pageleave`, JS errors | injected into every `text/html` response by `app/middleware/posthog_inject.py` |

## CLI coverage

The `da` CLI (`cli/main.py:main`) catches every uncaught exception from a
command, forwards it to PostHog with `component=cli` and the invoked
command name, then flushes the client before re-raising for Typer's
default error printer. Normal Typer / Click exits, `SystemExit`, and
`KeyboardInterrupt` are intentionally skipped.

Operators must surface `POSTHOG_API_KEY` (and any other `POSTHOG_*` knob)
into the shell that runs `da` — typically by sourcing the same `.env` the
server uses, or by setting the variable in their shell profile. The CLI
respects exactly the same env-var contract as the server.

LLM calls made by CLI commands (`da query`, `da explore`, etc.) flow
through the provider wrappers in `connectors/llm/` and therefore emit
`$ai_generation` events via the same tracing path the server uses.

## Testing the integration

Boot the app with the key set, hit `/`, then provoke a 500 (e.g. via a
debug-only route). One **Errors** event should arrive within seconds along
with one `$pageview` per page load. Open **Session replay** and pick the
session — every `<input>` should show as a masked rectangle.

The unit tests in `tests/test_posthog_*.py` cover the disabled and enabled
configurations; `tests/test_llm_tracing.py` exercises the success and error
variants of the LLM event.

## Self-hosting note

PostHog is itself open source — operators with a self-hosted PostHog instance
just point `POSTHOG_HOST` at their endpoint. No code changes required.
