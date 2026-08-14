# Listing the MCP server in the community registry

`server.json` at the repo root is the registry listing for this server. It is
published to the [community MCP Registry](https://modelcontextprotocol.io/registry),
which clients read to offer a browsable server list — VS Code's MCP browser
today, other IDEs as they adopt it.

## Why the URL has a variable in it

Agnes is self-hosted: every deployment answers on its own hostname, so there is
no single canonical URL to list. The registry schema handles this directly —
the `remotes[].url` is a **template**, and `remotes[].variables` declares what
the reader has to supply:

```json
"url": "https://{instance_host}/api/mcp/http",
"variables": {
  "instance_host": {
    "description": "Hostname of your Agnes instance, e.g. agnes.example.com",
    "isRequired": true
  }
}
```

Someone installing from the registry is prompted for their own host. One
listing therefore serves every deployment, and no instance is named in it.

This is worth stating because the two vendor connector directories work the
other way round: both Anthropic's and OpenAI's assume one canonical URL per
listing, and the self-hosted shape is the hard part of those submissions. Here
it is a first-class feature.

Authentication needs no manifest field. Clients discover OAuth 2.1 + PKCE from
the `WWW-Authenticate` header on a 401 (RFC 9728), which the server already
serves — see `app/auth/mcp_oauth.py`.

## Publishing

Publishing is **immediate and unreviewed** — there is no approval step between
running the command and the entry appearing. Treat it as a release action.

```bash
brew install mcp-publisher          # or the release tarball

mcp-publisher login github          # device flow; grants io.github.<org>/*
mcp-publisher publish               # reads ./server.json
```

The GitHub login proves ownership of the `io.github.<org>` namespace, which is
why the listing name is `io.github.keboola/agnes`. A domain-based namespace
(`com.example/*`) is also possible, via a DNS TXT record or a
`/.well-known/mcp-registry-auth` file, if the listing should ever be published
under the product domain instead.

### After publishing

- **Versions are immutable.** An update means publishing a new `version`.
  Keep it in step with `pyproject.toml`.
- **Unpublishing is possible but not a delete.** `mcp-publisher status` can
  move an entry to `deleted` (hidden from listings, still retrievable with
  `include_deleted=true`) or `deprecated`.
- **`github.com/mcp` is a separate, manual gate.** Publishing to the community
  registry does not put the server in GitHub's own curated registry; that
  requires emailing GitHub to request inclusion.

## Editing the manifest

`tests/test_mcp_registry_manifest.py` guards the constraints that otherwise
only surface at publish time. The one that bites is the **100-character cap on
`description`** — short enough that an ordinary rewrite walks past it. The
tests also fail if the URL stops being a template, since a hard-coded hostname
would point every reader at one company's instance.

---

## Which AI clients can connect (CON-5)

The connector picker at `/me/ai-connector` lists only clients that can complete
an OAuth handshake against a *third-party, self-hosted* MCP server on their
own. Gemini and Microsoft Copilot were removed from it in June 2026 for
failing that bar. Re-checked August 2026 — the picture has moved, but not
enough to put either back:

| Client | Can it connect? | Why it is / is not in the picker |
|---|---|---|
| Claude (Desktop, web, Code) | Yes | Listed |
| ChatGPT | Yes | Listed |
| Cursor, VS Code / GitHub Copilot | Yes | Listed |
| **Gemini app (Spark)** | Yes, with DCR | **Not listed** — needs a Gemini Spark entitlement, a *personal* Google account (explicitly unavailable on Workspace accounts), and is US-only. An enterprise reader following it would hit a wall none of the copy could usefully warn them about. |
| **Gemini Enterprise** | Yes, but | OAuth 2.0 + optional PKCE and **no** dynamic client registration — a *team administrator* registers the server and supplies a client id/secret by hand. Not a self-service picker path. Supporting it would mean accepting a statically registered OAuth client, which Agnes does not do today. |
| **Copilot Studio** | Yes, with DCR | A *maker* adds the server to an agent's Tools; it is an agent-building surface, not an end-user chat client, and access rides Power Platform DLP policy. |
| **Microsoft 365 Copilot** | No | Both routes (federated connector, or BYO MCP via Agent 365) are gated on a partner submission plus tenant-admin approval, and the federated path accepts read/search tools only. |

So: nothing in the UI changed. The distinction that decides it is **self-service
versus admin-gated** — a picker entry promises "paste your URL and go", and
only the first four keep that promise. Gemini Enterprise and Copilot Studio are
real integration paths for an operator who wants them, which is why they are
written down here rather than dropped.

---

## Forbidden-category audit for the OpenAI submission (CON-3)

OpenAI's app-review rules disallow returning PCI DSS data, PHI, government
ID/credentials, raw location fields, or raw chat transcripts without masking.
Walked all 67 tools registered in `app/api/mcp/foundation_tools.py` against
that list:

- **`query` / `describe` / `schema` / `catalog`** are generic pass-through
  onto whatever tables the operator has registered — Agnes has no semantic
  knowledge of a column's contents, so it cannot mask a category it cannot
  identify. Keeping any of the five categories out of these results is the
  operator's own data-pipeline responsibility, the same governance boundary
  the existing corporate policy already draws ("PII columns must be
  SHA-256 hashed in the analytics/reporting layer" applies *before* data
  reaches Agnes, not inside the MCP tool layer).
- **No tool exposes raw chat/session transcripts.** `agent_ask`/`agent_list`/
  `agent_usage` return only metadata (config, token usage) about the
  caller's own agents, never another user's conversation content.
- **`chat_upload_file`** reads only client-side (the server-hosted MCP
  surface refuses a `file_path` outright, precisely to prevent it from
  becoming an arbitrary-file-read primitive against the server).
- **Credential-bearing tools** (`data_app_git_credential`,
  `agnes_data_app_credentials`, `admin_analytics_migrate`, admin `*` tools)
  are scoped to the resource's owner/admin and out of scope for *this*
  check — that's the RBAC gate's job, not a data-category question.

No forbidden category is returned as a first-class Agnes concept by any
foundation tool. Nothing to fix in this codebase for this item; it is a
statement to carry into the OpenAI submission, not an open finding.

---

## GitHub Copilot: registry listing is not enough on its own (CON-6)

Registering `io.github.<org>` in the community registry (this document, above)
also makes the server discoverable through the **GitHub MCP Registry**
(`github.com/mcp`) — the same manifest, a second surface, no extra content.
But that only gets Agnes *listed*; it does not get it *usable*.

Copilot Business and Enterprise tenants have a separate admin policy toggle,
**"MCP servers in Copilot," off by default**. A registry listing does nothing
for a customer whose admin has not turned that on — the failure mode looks
identical to "the server isn't published," so it is worth spelling out for
customers up front rather than letting it surface as a support ticket:

1. Confirm the tenant admin has enabled the MCP servers policy for Copilot
   Business/Enterprise.
2. Only then does adding the Agnes MCP server (by registry name, or by URL as
   a manual fallback) do anything in a Copilot session.

There is currently no enterprise-level allowlist for *which* registries or
individual servers a Copilot user may add — it is an open community discussion
upstream, not something Agnes controls. Worth flagging to security-conscious
customers alongside the policy-toggle note, not as a gap in Agnes.
