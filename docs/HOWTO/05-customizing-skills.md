# Customising your skills

Agnes serves a curated set of Claude Code skills (plugins) through your instance's marketplace. You can extend your personal stack in two ways: installing from the curated/flea tabs, and uploading your own.

---

## Surface 1: Installing from the marketplace

### Curated tab (admin-managed)

Your admin has registered one or more git repos as marketplaces. The plugins from those repos appear on the **Curated** tab at `/marketplace`.

- Browse and install plugins from the curated tab.
- After installing, sync your local Claude Code marketplace:
  ```bash
  agnes refresh-marketplace --quiet
  ```
  This is also wired to run automatically at `SessionStart` (via the hook `agnes init` installs).
- To see what's in your current stack:
  ```bash
  agnes my-stack
  ```
- The **Most Popular** section (top of `/marketplace`) shows the 8 most-invoked plugins over the last 30 days — a quick signal for what your teammates find useful.
- Sort options: **Recent** (default), **Most used (30d)**, **Trending (week-over-week)**.

### Flea tab (community uploads)

The **Flea** tab shows plugins uploaded by any analyst on the instance, after admin approval. Browse and install the same way as curated plugins.

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
# Sync your local marketplace to pick up the new plugin
agnes refresh-marketplace

# Verify it's in your stack
agnes my-stack
```

---

## Opt-outs

If a plugin appears in your marketplace feed (curated or flea) but you don't want it:
- Uninstall from the `/marketplace` UI — this records a `user_plugin_optouts` row.
- The opted-out plugin will no longer appear in your served ZIP or git feed.

This is per-user, per-plugin. Your admin's grants are unaffected.

---

## Notes

- **Same-named plugins** from two upstream marketplaces collide by design: RBAC decides which one your feed serves. If you see unexpected behaviour after an admin adds a second marketplace, check `/marketplace/info` — it exposes both `name` and `prefixed_name` for disambiguation.
- **No team-level ACL** in v1 for flea market plugins — guardrails + admin approval are the gatekeepers.
- **Curated > flea** precedence: if a curated plugin and a flea plugin share the same name, the curated one wins in your stack.
