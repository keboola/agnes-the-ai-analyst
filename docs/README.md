# Agnes documentation

Index of all documentation, organized by who needs it. New here? Start with the
row that matches your role.

| You are… | Start with |
|----------|-----------|
| **Analyst** — using Agnes to query data | [`QUICKSTART.md`](QUICKSTART.md), then [`HOWTO/`](HOWTO/) |
| **Operator** — deploying & running an instance | [`PLATFORM_SETUP.md`](PLATFORM_SETUP.md) |
| **Developer** — working on Agnes itself | [`../ARCHITECTURE.md`](../ARCHITECTURE.md) + [`architecture.md`](architecture.md) |

---

## For analysts

Using the platform to analyze data.

- [`QUICKSTART.md`](QUICKSTART.md) — local setup + first sync
- [`HOWTO/`](HOWTO/) — task-oriented cookbook (querying, snapshots, common workflows)
- [`DATA_SOURCES.md`](DATA_SOURCES.md) — data source connectors (Keboola, BigQuery, CSV) and how tables surface
- [`metrics/`](metrics/) — canonical business-metric definitions (YAML)
- [`HEADLESS_USAGE.md`](HEADLESS_USAGE.md) — PAT auth for CI / headless clients

## For operators

Deploying, configuring, and running an Agnes instance.

- [`PLATFORM_SETUP.md`](PLATFORM_SETUP.md) — **the consolidated operator playbook** (bootstrap, TLS, marketplaces, scheduler, telemetry)
- [`ecosystem-map.md`](ecosystem-map.md) — bird's-eye view of all 5 tiers (OSS + infra + marketplace + initial-workspace + legacy)
- [`ONBOARDING.md`](ONBOARDING.md) — end-to-end Terraform deployment into a new GCP project
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — picks between the Terraform and Docker Compose paths
- [`CONFIGURATION.md`](CONFIGURATION.md) — `instance.yaml`, env vars, per-instance options
- [`state-dir.md`](state-dir.md) — persistent data layout (`data` + `state` tiers, mount layouts, migration)
- [`RBAC.md`](RBAC.md) — access control: groups, members, resource grants
- [`auth-google-oauth.md`](auth-google-oauth.md) — Google OAuth setup + operator gotchas
- [`auth-groups.md`](auth-groups.md) — Google Workspace group sync
- [`admin/query-modes.md`](admin/query-modes.md) — table registration query modes
- [`agent-setup-prompt.md`](agent-setup-prompt.md) — customize the `/setup` page banner
- [`agent-workspace-prompt.md`](agent-workspace-prompt.md) — customize the generated analyst `CLAUDE.md`
- [`initial-workspace-override.md`](initial-workspace-override.md) — per-instance analyst-workspace skeleton override
- [`curated-marketplace-format.md`](curated-marketplace-format.md) — authoring `marketplace-metadata.json` for curated marketplaces
- [`observability.md`](observability.md) — PostHog integration (exceptions, tracing, session replay)
- [`operator/news-content-guide.md`](operator/news-content-guide.md) — editorial guidelines for in-app news content

## For developers

Working on the Agnes codebase.

- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — high-level system overview (the summary)
- [`architecture.md`](architecture.md) — detailed architecture reference (module map, extract.duckdb contract, components)
- [`../CLAUDE.md`](../CLAUDE.md) — project instructions for AI agents working in this repo
- [`development.md`](development.md) — logging, request correlation, debug toolbar
- [`local-development.md`](local-development.md) — `LOCAL_DEV_MODE` setup (what's mocked vs. real)
- [`RELEASING.md`](RELEASING.md) — release process, deploy workflows, CI quirks
- [`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md) — pre-merge checks for bootstrap-path changes
- [`testing/`](testing/) — test plans (clean-analyst bootstrap, VM test)
- [`marketplace.md`](marketplace.md) — Claude Code marketplace ingestion + re-serving internals
- [`STORE_GUARDRAILS.md`](STORE_GUARDRAILS.md) — flea-market upload guardrails (static checks + LLM review)
- [`corporate-memory-governance.md`](corporate-memory-governance.md) — knowledge-distribution governance design
- [`ADR-corporate-memory-v1.md`](ADR-corporate-memory-v1.md) — ADR: corporate-memory v1 decisions
- [`llm-routing.md`](llm-routing.md) — design: provider-agnostic LLM routing
- [`sample-data.md`](sample-data.md) — sample data generator (e-commerce schema, size presets)
- [`theme-reference.html`](theme-reference.html) — web UI theme/color reference
- [`../dev_docs/`](../dev_docs/) — **server/developer-internal docs** (not synced to analyst machines): server ops, disaster recovery, security audit, desktop app, design system, Telegram bot

Code-adjacent READMEs: [`../connectors/jira/README.md`](../connectors/jira/README.md),
[`../services/corporate_memory/README.md`](../services/corporate_memory/README.md),
[`../scripts/README.md`](../scripts/README.md).
Agent skill files: [`../cli/skills/`](../cli/skills/).

## Other

- [`../CHANGELOG.md`](../CHANGELOG.md) — full change history (Keep-a-Changelog format)
- [`archive/`](archive/) — historical planning artifacts and superseded docs; not maintained, see [`archive/README.md`](archive/README.md)
