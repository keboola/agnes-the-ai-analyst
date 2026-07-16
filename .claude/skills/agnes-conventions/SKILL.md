---
name: agnes-conventions
description: Agnes implementation playbooks + non-negotiables. Use when implementing a feature in this repo — adding a data-source connector, REST API endpoint, HTML dashboard page, repository method/repo, or schema migration. Routes to per-task reference playbooks verified against the codebase.
---

# Agnes conventions

The non-negotiables (what must change together) live in `CONTRIBUTING.md` →
**Sync-map**. This skill holds the step-by-step playbooks. Read `CONTRIBUTING.md`
first, then load the one playbook matching your task:

- `references/connector.md` — new data-source connector (the `extract.duckdb` contract)
- `references/endpoint-rbac.md` — new REST endpoint + the correct RBAC gate
- `references/web-page.md` — new HTML dashboard page (design-system page shell)
- `references/design-system.md` — visual standard for ANY UI work: `--ds-*` tokens, theme switch (`paper` etc.), chrome layouts (topnav/rail), accent vocabularies, scoping rules
- `references/repo-parity.md` — new repository / method with DuckDB↔Postgres parity
- `references/migration.md` — schema migration on both the DuckDB and Alembic ladders
- `references/command-ux.md` — new/changed CLI command, MCP tool, or search surface (scope model, flag vocabulary, error hints, transport parity)

Each playbook cites `file:line` anchors verified against the current codebase.
