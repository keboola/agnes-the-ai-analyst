# Customising your skills

Agnes serves a curated set of Claude Code skills, agents, and plugins through your instance's marketplace. You can extend your personal stack in two ways: adding items from the Curated and Flea Market tabs, and uploading your own.

---

## Surface 1: Discovering and adding from the marketplace

You have two paths: the web UI at `/marketplace`, or the `agnes marketplace` CLI from any workspace.

### Curated tab (admin-managed)

Your admin has registered one or more git repos as marketplaces. The plugins from those repos appear on the **Curated** tab at `/marketplace` (only those visible to your RBAC groups).

- The **Most Popular** section (top of `/marketplace`) shows the 8 most-invoked plugins over the last 30 days.
- Sort options: **Recent** (default), **Most used (30d)**, **Trending (week-over-week)**.

### Flea tab (community uploads)

The **Flea Market** tab shows skills, agents, and plugins uploaded by any analyst on the instance, after admin approval.

### Using the CLI

```bash
# Search across Curated + Flea Market
agnes marketplace search -q "pdf"
agnes marketplace search --type skill --source curated

# Full detail — use cases, contents, examples
agnes marketplace detail <id>

# Add to / remove from your stack
agnes marketplace add <id>
agnes marketplace remove <id>

# What's currently in my stack?
agnes my-stack show
```

ID format: curated items are `marketplace_id/plugin_name`, Flea items are UUIDs.

### Applying changes to Claude Code

After `add` / `remove`, run inside Claude Code:

```
/update-agnes-plugins
```

That installs/updates/removes the corresponding plugins in your local Claude Code session. The `SessionStart` hook detects pending updates automatically and surfaces a hint, so you can wait for the next session if you prefer.

---

## Surface 2: Uploading your own plugin

If you've built a Claude Code plugin (a `.claude-plugin/` directory with `plugin.json` + commands), you can share it on the flea market.

### Step 1: Prepare your plugin

Your plugin directory should contain at minimum:
```
my-plugin/
└── .claude-plugin/
    ├── plugin.json       # name, description, version
    └── commands/         # one .md file per command
        └── my-command.md
```

### Step 2: Submit via the upload form

1. Navigate to `/store/new` on your Agnes instance.
2. Fill in the name, description, and upload the plugin archive (`.zip` or a git repo URL).
3. Submit. Your submission enters the approval queue.

### Step 3: Guardrails review

Submissions go through `src/store_guardrails/` — an LLM-gated review that checks for:
- Harmful or policy-violating content
- Malformed `plugin.json`
- Commands that look dangerous or misleading

If the guardrail rejects your submission, you'll see a reason. Fix and resubmit.

### Step 4: Admin approval

After guardrails pass, your submission lands in the admin approval queue at `/admin/store`. An admin reviews and approves or rejects it. There is no per-team or self-serve approval in v1 — admin sign-off is required.

Once approved, the plugin appears on the Flea tab and becomes installable by other analysts.

### After approval

```bash
# Add it to your stack (or do it from the Flea tab on the web)
agnes marketplace add <entity-id>

# Verify it's in your stack
agnes my-stack show
```

Then run `/update-agnes-plugins` inside Claude Code to install/activate the bundle.

---

## Removing items from your stack

If you no longer want a plugin/skill/agent in your stack:

```bash
agnes marketplace remove <id>
```

Or click "Remove from stack" on the marketplace detail page in the web UI.

System plugins (admin-pinned for the org) cannot be removed — the API returns 409. Your admin's RBAC grants are unaffected by your own add/remove choices.

---

## Notes

- **Same-named plugins** from two upstream marketplaces collide by design: RBAC decides which one your feed serves. If you see unexpected behaviour after an admin adds a second marketplace, check `/marketplace/info` — it exposes both `name` and `prefixed_name` for disambiguation.
- **No team-level ACL** in v1 for flea market plugins — guardrails + admin approval are the gatekeepers.
- **Curated > flea** precedence: if a curated plugin and a flea plugin share the same name, the curated one wins in your stack.
