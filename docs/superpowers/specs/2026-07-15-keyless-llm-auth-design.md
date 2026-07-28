# Keyless LLM auth (Workload Identity Federation) — design

**Status:** proposed
**Author:** (design doc)
**Date:** 2026-07-15
**Relates to:** chat sandbox secret broker (INC-01572 hardening), `app/api/broker.py`

## Goal

Add an **optional** authentication mode for the chat LLM path that carries **no
long-lived Anthropic API key**. Instead of a static `ANTHROPIC_API_KEY`, the
Agnes server authenticates to Anthropic with a **short-lived token minted from
the workload's own identity** (OIDC), via Anthropic's native **Workload Identity
Federation (WIF)**. The static-key mode stays the default; keyless is opt-in.

## Why

1. **No static secret to leak, fund, or rotate.** The chat sandbox secret broker
   already keeps the real credential out of the sandbox (INC-01572). Keyless
   goes one step further server-side: there is no durable Anthropic key on the
   host at all — only a ~1h federated access token, minted on demand from the
   workload's identity and auto-refreshed.
2. **Removes a recurring operational failure.** A static key that expires or is
   left unfunded takes chat down with an opaque `401`. Keyless removes the key
   from the failure surface entirely — auth is tied to the workload's IAM
   identity, not a secret an operator has to keep alive.
3. **Consistency with existing keyless auth.** The BigQuery connector already
   authenticates keyless via the cloud metadata server
   (`connectors/bigquery/auth.py`). This applies the same pattern to the LLM leg.

## Verified capability (the key finding)

Anthropic's **first-party API supports Workload Identity Federation natively —
GA, no third-party platform (Vertex/Bedrock) required.** (The WIF *feature* is
GA; the short-lived OAuth-style token it mints still travels with the
`anthropic-beta: oauth-2025-04-20` request header — see "The broker change"
below. "No beta" refers to enrollment/platform, not the per-request header.)
The official client auto-detects WIF when these are set and no static
credential outranks them:

- `ANTHROPIC_FEDERATION_RULE_ID`
- `ANTHROPIC_ORGANIZATION_ID`
- `ANTHROPIC_SERVICE_ACCOUNT_ID`
- `ANTHROPIC_IDENTITY_TOKEN_FILE` **or** `ANTHROPIC_IDENTITY_TOKEN`
- (`ANTHROPIC_WORKSPACE_ID` only when the federation rule spans multiple workspaces)

It exchanges the workload's OIDC identity JWT at `/v1/oauth/token` for a
short-lived access token and auto-refreshes it. A set `ANTHROPIC_API_KEY` /
`ANTHROPIC_AUTH_TOKEN` (even empty) or `ANTHROPIC_PROFILE` outranks WIF — those
must be unset for the federation path to activate.

**Consequence:** keyless does **not** require routing Claude through a cloud
provider (Vertex/Bedrock). It stays on `api.anthropic.com`, the same Messages
API, same model IDs, same request/response shape. Only the auth header changes:
`x-api-key: <static>` → `Authorization: Bearer <federated token>` (with the
`anthropic-beta: oauth-2025-04-20` header that OAuth-style tokens require). This
makes the change small and fully contained in the broker.

## The identity token

The "identity token" is a signed OIDC JWT proving the workload's identity, for a
configured audience. Sources are pluggable and vendor-neutral:

- **Cloud VM/serverless metadata identity endpoint** — e.g. a GCE/GKE instance's
  metadata server issues an OIDC identity token for a given `audience`
  (distinct from the OAuth *access* token `connectors/bigquery/auth.py` fetches;
  the identity endpoint returns a signed JWT, not a bearer access token).
- **Projected service-account token file** — e.g. a Kubernetes projected volume;
  point `ANTHROPIC_IDENTITY_TOKEN_FILE` at it.
- **Any OIDC provider** the operator configures a federation rule to trust.

The Anthropic **federation rule** (configured once, operator-side, in the
Anthropic Console) trusts that issuer + audience and maps it to an Anthropic
service account. No app code owns this — it is deployment configuration.

## Design

### Config

A single opt-in switch in `instance.yaml`, default preserves today's behavior:

```yaml
chat:
  llm:
    auth: api_key            # api_key (default) | workload_identity
    # workload_identity mode reads the ANTHROPIC_FEDERATION_* env (see above);
    # identity_token_audience is the audience the identity token is minted for.
    identity_token_audience: "<anthropic-federation-audience>"
```

`api_key` is the default so non-cloud / self-hosted installs are unaffected.

### Broker change (the whole code change)

`app/api/broker.py::anthropic_proxy` is the single credential-injection point.
Today it sets `headers["x-api-key"] = os.environ["ANTHROPIC_API_KEY"]` and
forwards to the pinned host. Make injection provider-switched:

- **`api_key` mode (default):** unchanged — inject `x-api-key`.
- **`workload_identity` mode:** inject
  `Authorization: Bearer <federated access token>` +
  `anthropic-beta: oauth-2025-04-20`; do **not** set `x-api-key`.

The federated token is obtained by a small **server-side** helper that:

1. Fetches the workload's OIDC identity token for the configured audience
   (metadata identity endpoint, or reads `ANTHROPIC_IDENTITY_TOKEN_FILE`).
2. Exchanges it for a short-lived access token via the documented WIF exchange
   (`/v1/oauth/token`).
3. Caches the access token with its expiry and refreshes before it expires.

This mirrors `connectors/bigquery/auth.py` (module-level cache + refresh +
`clear_token_cache()` on a 401). Generalize/reuse that pattern rather than
duplicating it.

### What does NOT change

- **URL, model IDs, request/response shape** — still `api.anthropic.com`, still
  the native Messages API. No Vertex translation, no model-id map.
- **The sandbox** — still points at the loopback relay with a dummy key, still
  credential-free. INC-01572 isolation is preserved and strengthened: there is
  no static key anywhere, and the only server-side credential is a ~1h federated
  token, never sent into the sandbox. The
  `test_no_real_anthropic_key_in_process_memory` red-team assertion continues to
  hold (there is no long-lived key to find).
- **The relay / runner** — no change.

### Relationship to a future central gateway ("Token Arbitrage")

If a central LLM gateway later owns provider selection / cost routing, keyless
composes cleanly: the broker's `_ANTHROPIC_BASE_URL` re-points at the gateway and
the same workload-identity token authenticates Agnes → gateway. The broker stays
the single credential boundary either way. This mode is the correct interim (and
a valid standalone deployment) that does not conflict with a later gateway.

## Acceptance criteria

| # | Criterion | Tier |
|---|---|---|
| AC-1 | With `auth: api_key`, behavior is byte-for-byte unchanged (regression). | unit |
| AC-2 | With `auth: workload_identity`, the broker injects `Authorization: Bearer …` + the oauth beta header and does **not** send `x-api-key`. | unit (capture headers, mock token helper) |
| AC-3 | The token helper caches and refreshes; a forced 401 clears the cache and re-mints. | unit |
| AC-4 | The identity token / federated access token never appears in any sandbox process env, argv, filesystem, or memory (extends the INC-01572 red-team). | e2b-tier (`@pytest.mark.e2b`) |
| AC-5 | Live: a real completion succeeds in `workload_identity` mode against a configured federation rule (operator gate — needs a real rule). | manual |

## Out of scope

- Setting up the Anthropic federation rule + the workload's IAM identity — these
  are operator/Console/IaC steps, documented in deployment docs, not app code.
- Vertex / Bedrock provider modes — explicitly not needed for keyless (this
  design supersedes the earlier Vertex-translation sketch).
- The central gateway ("Token Arbitrage") — separate effort; this composes with
  it.

## Risks / notes

- **Precedence trap:** a set `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` (even
  empty) or `ANTHROPIC_PROFILE` outranks WIF. In `workload_identity` mode the
  startup path must ensure those are unset for the broker's token source, or the
  federation silently won't activate.
- **Audience mismatch** between the identity token and the federation rule is the
  most likely misconfiguration → surface a clear error from the token helper.
- **Vendor-neutral:** default stays `api_key`; keyless is opt-in and provider is
  described generically (any OIDC identity source). Cloud-specific federation
  config lives in the private infra repos that consume this one, not here.

## Effort

Small, localized: the broker provider-switch + a generalized metadata/identity
token-exchange helper + a config flag + unit tests. The long pole is
operational (creating the federation rule and wiring the identity-token source),
which is deployment configuration, not code.
