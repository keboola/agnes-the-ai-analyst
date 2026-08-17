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
