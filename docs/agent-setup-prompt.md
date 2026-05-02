# Agent Setup Prompt

The agent setup prompt is an HTML banner shown **above the bash setup commands**
on the `/setup` page. It is intended for organisation-specific operational notes
that every new analyst should read before running the bootstrap script —
for example: VPN requirements, support channel, data classification reminder,
or platform-specific prerequisites.

## Default behaviour

No banner is shown by default. The `/setup` page renders only the standard
install steps until an admin configures an override.

## Customising per instance

Admins configure the banner via:

- **Admin UI:** `/admin/agent-prompt` — Jinja2 HTML editor with a placeholder
  cheatsheet, live preview, and save/reset actions.
- **REST API:**
  - `GET /api/admin/welcome-template` — returns `{content, updated_at, updated_by}`.
    `content` is `null` when no override is set (default = no banner).
  - `PUT /api/admin/welcome-template` with body `{"content": "..."}` — validates
    Jinja2 syntax and renders against a stub context before persisting.
    Returns `400` on syntax errors or unknown placeholders.
  - `DELETE /api/admin/welcome-template` — clears the override; no banner shown.
  - `POST /api/admin/welcome-template/preview` with body `{"content": "..."}` —
    renders arbitrary content against the calling admin's live context without
    persisting. Used by the editor's Preview button.

The override lives in `system.duckdb` (table `welcome_template`, singleton
row id=1). The `DELETE` endpoint NULLs `content`; the audit trail
(`updated_at`, `updated_by`) is preserved.

## Template language

[Jinja2](https://jinja.palletsprojects.com/) with `autoescape=True` and
`StrictUndefined`. Autoescape is on because the output is rendered into HTML.
Any typo in a placeholder name raises an error at PUT validation time rather
than silently emitting an empty string — the editor reports the error
immediately so the admin can fix it before saving.

## Available placeholders

| Placeholder | Type | Notes |
|---|---|---|
| `instance.name` | string | `instance.name` in `instance.yaml` |
| `instance.subtitle` | string | `instance.subtitle` in `instance.yaml` |
| `server.url` | string | Full server URL at render time |
| `server.hostname` | string | Host part only |
| `user` | object or `null` | `null` for anonymous `/setup` visitors |
| `user.id` | string | Authenticated user ID |
| `user.email` | string | Authenticated user email |
| `user.name` | string | Authenticated user display name |
| `user.is_admin` | bool | Whether the user is in the Admin group |
| `user.groups` | list[str] | User's group names |
| `now` | datetime (UTC, tz-aware) | Server time at render |
| `today` | string (`YYYY-MM-DD`) | Server date |

**Anonymous visitors:** `user` is `null` on `/setup` when the visitor is not
signed in. Guard any user-specific content with `{% if user %}…{% endif %}`.

## Security

Output is HTML-sanitized after Jinja2 render as a defense-in-depth measure:

- `<script>…</script>` blocks are stripped.
- `<iframe>…</iframe>` elements are stripped.
- `on*=` event handler attributes (e.g. `onclick=`, `onload=`) are stripped.
- `javascript:` and `data:` URI schemes in `href`/`src`/`action` attributes
  are replaced with `#`.

Admins are trusted, but this prevents accidental XSS from copy-pasted snippets
reaching the public `/setup` page.

## Example: VPN and support banner

```html
<strong>Before you start:</strong> This server is on the corporate VPN.
Connect to <code>vpn.example.com</code> before running the install command.
{% if user %}
  <br>Signed in as <strong>{{ user.email }}</strong> —
  <a href="https://support.example.com">open a ticket</a> if you need help.
{% endif %}
```

## Resetting to no banner

Click **Reset to default** in the admin UI, or call
`DELETE /api/admin/welcome-template`. The `/setup` page will show only the
standard install steps with no banner above them.
