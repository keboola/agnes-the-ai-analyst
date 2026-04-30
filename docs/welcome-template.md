# Welcome prompt customization

The welcome prompt is the `CLAUDE.md` file generated in an analyst's local
workspace by `da analyst setup`. It instructs Claude Code on how to behave in
that workspace — which commands to use, where to read schema metadata, what
metrics exist, what plugins are available.

## Defaults

The OSS distribution ships a generic welcome prompt at
`config/claude_md_template.txt`. Every Agnes instance starts with this default;
no admin action is required.

## Customizing per instance

Admins can override the template via:

- **Admin UI:** `/admin/welcome` — textarea editor with placeholder cheatsheet
  and live preview button. Save sends a `PUT` to `/api/admin/welcome-template`.
- **REST API:**
  - `GET /api/admin/welcome-template` — returns `{content, default, updated_at, updated_by}`. `content` is `null` when no override is set.
  - `PUT /api/admin/welcome-template` with body `{"content": "..."}` — validates Jinja2 syntax, stores the override.
  - `DELETE /api/admin/welcome-template` — clears the override; renderer falls back to the shipped default.
  - `POST /api/admin/welcome-template/preview` with body `{"content": "..."}` — renders arbitrary content against the calling admin's live context without persisting. Used by the editor's Preview button.

The override lives in `system.duckdb` (table `welcome_template`, singleton
row id=1). Resetting via the UI or `DELETE` simply NULL-s `content` — the
audit trail (`updated_at`, `updated_by`) is preserved.

## Template language

[Jinja2](https://jinja.palletsprojects.com/) with `StrictUndefined`. Any
typo in a placeholder name raises an error at render time rather than
silently emitting an empty string. Server returns HTTP 500 with a hint
pointing at `/admin/welcome`; the admin UI rejects syntax errors AND
undefined-placeholder errors with HTTP 400 on save (validated by rendering
the template against a stub context before persisting).

## Available placeholders

| Placeholder | Type | Source |
|---|---|---|
| `instance.name` | string | `instance.name` in `instance.yaml` |
| `instance.subtitle` | string | `instance.subtitle` in `instance.yaml` |
| `server.url` | string | passed by the CLI (`?server_url=` query) |
| `server.hostname` | string | parsed from `server.url` |
| `sync_interval` | string | `instance.sync_interval` in `instance.yaml` (default `"1 hour"`) |
| `data_source.type` | string | `keboola` \| `bigquery` \| `local` |
| `tables` | list | rows from `table_registry`, each `{name, description, query_mode}` |
| `metrics.count` | int | total rows in `metric_definitions` |
| `metrics.categories` | list[str] | distinct categories from `metric_definitions` |
| `marketplaces` | list | RBAC-filtered for the calling user, each `{slug, name, plugins:[{name}]}` |
| `user.email` | string | calling user |
| `user.name` | string | calling user |
| `user.is_admin` | bool | calling user |
| `user.groups` | list[str] | calling user's group names |
| `now` | datetime (UTC, tz-aware) | server time at render |
| `today` | string (`YYYY-MM-DD`) | server date |

> **Timezone caveat:** `now` is tz-aware UTC, while DB-sourced timestamps elsewhere in the codebase are naive (DuckDB stores `TIMESTAMP`, not `TIMESTAMPTZ`). Don't subtract or compare `now` with naive timestamps inside templates without normalising first.

## RBAC

`marketplaces` is filtered through `src.marketplace_filter.resolve_allowed_plugins`
— the same logic that gates `/marketplace.zip`. Two analysts with different
group memberships will see different plugin lists in their `CLAUDE.md`.

> **Admin self-view caveat:** `Admin` group is treated like any other group for marketplace filtering — there is no god-mode shortcut. An admin viewing the editor's Preview will see an empty `marketplaces` list unless the admin's groups have plugin grants. To populate the list, grant plugins to the `Admin` group (or any group the admin is a member of).

## Example: minimal override

```jinja2
# {{ instance.name }}

This workspace is connected to {{ server.url }}.
You have access to {{ tables | length }} dataset(s):
{% for t in tables %}
- `{{ t.name }}`{% if t.description %}: {{ t.description }}{% endif %}
{%- endfor %}
```

## Falling back to the default

Click **Reset to default** in the admin UI or `DELETE
/api/admin/welcome-template`. The shipped default is always available as
`response.default` in the GET endpoint, so admins can copy-paste it into
the editor as a starting point for a new override.

## Older-server compatibility

The CLI (`da analyst setup`) tolerates older servers that don't yet
implement `/api/welcome` — on a 404, it writes a minimal embedded fallback
`CLAUDE.md` and prints a stderr warning on any other failure mode (5xx,
network, auth). Upgrade the server to get the full feature.
