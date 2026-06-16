---
name: agnes-operator
description: How to configure and customize THIS Agnes instance — the init prompt, the analyst workspace, branding / instance.yaml, and connectors — across its environments. Use when an operator or admin asks where the instance's content/config lives, how to change the init prompt or landing page, how to rebrand, or how a config value resolves. Triggers on "change the init prompt", "edit branding", "where does the config live", "how do I customize this instance", "change /home content", "initial workspace template".
---

# Configuring this Agnes instance

Use this when someone wants to change an Agnes instance's content or
configuration. The customizable surface spans a few independent layers; identify
the layer first, and read the instance's **live** state instead of guessing.

## Always start: read this instance's live config surface

Do NOT hardcode or assume where things live — every instance is registered
differently. Read the actual state first:

```bash
agnes admin config-surface --json
```

It returns, for THIS instance: every config knob with its resolved value and
source (env / yaml / default), the registered **Initial Workspace Template**
repo URL + branch, the registered **marketplaces**, and `infra_repo_url`. Use
those concrete values in your answer. (Same data via `GET /api/admin/config-surface`
or the `admin_config_surface` MCP tool.)

## Layer 1 — Init prompt & analyst workspace (Initial Workspace Template)

The init prompt shown on `/home` and the workspace an analyst receives from
`agnes init` come from the registered **Initial Workspace Template** (IWT) seed
repo — its URL is in the config surface above.

- **Init prompt** = `install-prompt/template.md.tmpl` in that repo.
- **Analyst workspace** = the `workspace/` subtree in that repo (only `workspace/`
  ships to analysts).

To change either: edit the file in the seed repo, then click **Sync now** under
`/admin/server-config` → *Initial Workspace Template* (the deploy is per-instance
and manual — merging the repo alone does nothing). When no IWT is registered, a
bundled snapshot inside the wheel is used instead.

**Gotcha:** when the seed repo owns the template, the `/admin/agent-prompt`
editor is read-only and saving there returns `409 iwt_seed_owns_template`. Edit
the repo file + Sync now; never the admin textbox.

See `docs/initial-workspace-override.md` and `docs/seed-repo-contract.md`.

## Layer 2 — Branding, theme, landing route, data source (instance.yaml)

These are config knobs (name, subtitle, brand, theme, `home_route`, data source,
…). Each resolves **env var > `instance.yaml` > built-in default** — the
`source` column in the config surface tells you which tier is currently winning.

**Footgun:** the env tier shadows the YAML tier. If a knob is pinned via an
`AGNES_*` env var, changing it in `/admin/server-config` (which writes YAML) does
nothing. Pin via env **or** manage via the admin UI, not both.

The full knob reference — env var ↔ `instance.yaml` path ↔ default for every
resolver — is `docs/CONFIGURATION.md`.

## Layer 3 — Connectors & marketplaces

Registered marketplaces (curated content served to users) are listed in the
config surface. Connector pre-provisioning (shared OAuth clients, base URLs) are
config knobs in Layer 2.

## Where the app vs the infra lives

- The **app** (the `/home` page, routes, the `agnes` binary) is the Agnes
  product itself — change its behavior in the application codebase, shipped as a
  new image.
- The **infrastructure** that provisions this instance is NOT tracked by the app
  unless an operator set `infra_repo_url` (shown in the config surface). If it's
  empty, the instance does not know its provisioning repo — ask the operator or
  check the deployment.

## Rule of thumb

Content (init prompt, workspace) → IWT seed repo + Sync now (easy). Branding /
config → `instance.yaml` knobs (mind the env-shadows-yaml footgun). Always read
the live config surface first so your answer names this instance's real pointers.
