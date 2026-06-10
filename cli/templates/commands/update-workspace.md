---
description: Safely update this workspace from the Initial Workspace Template (backs up your changes)
---

This workspace was set up from an Initial Workspace Template. This command
re-applies the latest template **without losing your edits** — any file you
changed is backed up to `<name>.bak.<timestamp>` before it's replaced, and
files you added that aren't in the template are left untouched.

First, preview what would change (writes nothing):

```bash
agnes update-workspace --dry-run
```

Show me the preview. It lists three groups:

- **Would back up + update** — files you changed; your version is saved to
  `.bak` and then refreshed from the template.
- **Would update in place** — files you hadn't changed; refreshed silently.
- **Would create** — new files the template adds.

Then **ask me to confirm** before changing anything. Only if I confirm, run:

```bash
agnes update-workspace --yes
```

Report the final summary, including the list of files that were backed up
(`~ original  →  .bak` lines), so I know exactly where my previous versions
went.

If the output says the instance has no Initial Workspace Template configured,
or that the workspace already matches the template, just tell me — there's
nothing to do and nothing was touched.
