---
description: Update Agnes marketplace plugins to latest versions
---

Run the following command to refresh the Agnes marketplace plugins
for this workspace:

```bash
agnes refresh-marketplace
```

Stream the output to me as it runs. If any plugins were installed
or updated, remind me to run `/reload-plugins` to load the changes
into this session — no Claude Code restart needed.

If the command fails, report the exact error so we can diagnose it
together (common causes: marketplace clone missing — fix with
`agnes refresh-marketplace --bootstrap`; expired PAT — fix with
`agnes auth import-token`).
