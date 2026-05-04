# Release Checklist

Pre-merge checks for changes that touch sensitive paths. Each section below
applies only when the PR diff intersects the listed files.

## Bootstrap path changes (mandatory pre-merge)

For any PR touching the analyst-bootstrap path (`agnes init`, `cli/lib/pull.py`,
`cli/lib/hooks.py`, `app/web/setup_instructions.py`, `/api/welcome`,
`config/agnes_workspace_template.txt`), run this protocol locally before
requesting review:

1. `git clean -fdx` in the repo (no build artifacts).
2. Boot FastAPI locally against a clean test instance state.
3. Empty terminal in `/tmp/test-analyst-1`. From the web `/setup?role=analyst`,
   click the analyst tile and copy the paste prompt.
4. Paste into Claude Code and let it run. `tree -a /tmp/test-analyst-1` and
   compare with the expected tree from the design spec
   (`docs/superpowers/specs/2026-05-04-clean-analyst-bootstrap-design.md` §5.2).
5. `claude` in that folder. Three queries:
   - "What tables can I see?"
   - "SELECT count(\*) FROM <t>" (a table from the catalog)
   - "Show me last 5 rows of <t>"
   All must work without further intervention.
6. `/exit`. Verify SessionEnd hook ran (server-side audit log shows `agnes push`;
   `du -sh /tmp/test-analyst-1/user/sessions/` non-empty).
7. Second `claude` in same folder. Verify SessionStart hook fires
   (`agnes pull` request in audit log).
8. Second workspace `/tmp/test-analyst-2` with the same PAT (within TTL).
   Repeat steps 5-7. Verify global `~/.config/agnes/` is not duplicated;
   the second workspace has its own DuckDB.
