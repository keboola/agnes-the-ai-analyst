# Installing skills in Claude Cowork (Claude Desktop)

Claude Cowork (the desktop app) can run the same marketplace plugins you use
in Claude Code — but it has **no sync**: you download a plugin package from
your Agnes instance and upload it into Cowork by hand, and repeat that after
the plugin is updated. This guide covers the whole loop.

For Claude Code the marketplace syncs automatically (`agnes update` /
[05-customizing-skills.md](05-customizing-skills.md)) — nothing here applies.

---

## Prerequisite: the plugin is in your stack

Cowork packages are offered for the plugins **installed in your stack** —
granted to one of your groups *and* subscribed on `/marketplace` (required-tier
plugins are always in). If the plugin you want isn't listed below, subscribe to
it first (see [05-customizing-skills.md](05-customizing-skills.md)) or ask your
admin for access.

## Step 1: Download the package

Two equivalent places, both on your instance's web UI:

- **`/me/ai-connector` → Plugin packages** — one `Download .zip` link per
  plugin in your stack.
- **`/marketplace` → plugin detail → `↓ Download for Cowork`** — same zip,
  one plugin at a time.

Always use these buttons. The server repackages the plugin into the exact
shape Cowork's upload validator accepts (single plugin at the zip root,
normalized manifest, description lengths capped, large `data/` trees
consolidated under Cowork's 5000-file limit). A hand-zipped plugin folder or
the aggregated `marketplace.zip` **will be rejected** by the validator.

## Step 2: Upload into Cowork

In Claude Desktop:

1. Open **Cowork** → **Customize** → **Personal plugins**.
2. **Create plugin** → **Upload plugin** → pick the downloaded `.zip`.
3. One package per zip — repeat for each plugin.

## Step 3: Verify

Start a new Cowork session and type `/` — the plugin's skills should appear
in the command list.

---

## Updates: re-download, re-upload

There is no sync channel to Cowork. When a plugin is updated on the
marketplace, download a fresh zip (the served file changes, so your browser
won't reuse a stale copy) and upload it again, replacing the old version.

## What Cowork packages do and don't carry

- **Carried:** skills, agents, commands, bundled reference data.
- **Not carried:** live data access. For catalog/query access from Cowork,
  connect the AI Connector instead (`/me/ai-connector`, "Control Agnes from
  your AI agent"). Note that Cowork/claude.ai connectors are called from
  Anthropic's servers — your instance must be reachable from the public
  internet for them to work. Instances on a private network can still use
  every MCP feature from Claude Code, which runs inside your network.

## Limitations

- **Flea-market skills and agents** are folded into the shared community
  bundle and have no standalone Cowork package — only curated plugins and
  flea-market *plugins* get a `Download for Cowork` button.
- If a plugin is too large even after consolidation (Cowork caps a zip at
  5000 files / 512 MB), the download returns an error instead of a zip the
  upload would silently reject. Ask the plugin's curator to slim it down.

## Troubleshooting

- **"Plugin failed validation" on upload** — make sure you uploaded a zip
  from the `Download for Cowork` button (not a hand-made zip), freshly
  downloaded from an up-to-date instance. If a fresh zip still fails, report
  the exact validator message to your admin: the validator's rules evolve,
  and the server-side packager may need a catch-up fix.
- **Plugin not in the Plugin packages list** — it isn't in your stack yet;
  see the prerequisite above.
