# Setup page banner

The setup banner is a block of HTML (or plain text) shown **above** the
auto-generated bootstrap commands on the `/setup` page. Use it for
org-specific operational notes that analysts need before they install the
client: VPN requirements, support channel, data-classification policy,
platform prerequisites, etc.

The banner is empty by default — no content is shown until an admin sets one.

## How to edit

- **Admin UI:** `/admin/setup-banner` — split-pane editor with a placeholder
  cheatsheet and a live HTML preview. Click **Save banner** to persist,
  **Remove banner** to clear.
- **REST API:**
  - `GET /api/admin/setup-banner` — returns `{content, updated_at, updated_by}`.
    `content` is `null` when no banner is set.
  - `PUT /api/admin/setup-banner` with body `{"content": "..."}` — validates
    Jinja2 syntax and stores the banner.
  - `DELETE /api/admin/setup-banner` — clears the banner; `/setup` shows no
    banner until one is set again.
  - `POST /api/admin/setup-banner/preview` with body `{"content": "..."}` —
    renders arbitrary content against the calling admin's context without
    persisting. Backs the editor's live preview.

The banner lives in `system.duckdb` (table `setup_banner`, singleton row id=1).

## Available placeholders

| Placeholder | Type | Notes |
|---|---|---|
| `instance.name` | string | `instance.name` in `instance.yaml` |
| `instance.subtitle` | string | `instance.subtitle` in `instance.yaml` |
| `server.url` | string | full origin of the Agnes server |
| `server.hostname` | string | host part only (no port or path) |
| `user.email` | string | logged-in user, or `null` for anonymous visitors |
| `user.name` | string | logged-in user display name |
| `user.is_admin` | bool | `true` when the visitor is in the Admin group |
| `now` | datetime (UTC, tz-aware) | server time at render |
| `today` | string (`YYYY-MM-DD`) | server date at render |

> **`user` may be `null`** — `/setup` is partly public (anonymous visitors
> get the install one-liner). Always guard user-specific placeholders:
>
> ```jinja2
> {% if user %}Welcome back, {{ user.name }}!{% endif %}
> ```

## Autoescape semantics

The Jinja2 environment runs with `autoescape=True`, which means template
**variable output** (`{{ ... }}`) is HTML-escaped automatically. Literal HTML
in the template source is passed through unchanged — that is how the banner
outputs `<p>` tags, `<strong>`, etc.

To output a literal `<` or `&` from a variable, use the `| safe` filter only
when you are certain the value is trusted:

```jinja2
{# Safe — admin-authored constant: #}
{{ "<strong>VPN required</strong>" | safe }}

{# Dangerous — never pipe user-controlled values through | safe: #}
{{ user.name | safe }}   {# do NOT do this #}
```

## Security note

Admin-authored banner content is rendered for **all `/setup` visitors**,
including anonymous users. As a defense-in-depth measure, inline `<script>`
tags, `<iframe>` blocks, `on*=` event handlers, and `javascript:`/`data:`
URI schemes are stripped from the rendered output before it reaches the
browser.

This is **not a full sandbox** — a determined admin can still author arbitrary
HTML with CSS tricks or external resource loads. The stripping is a safety net
against accidental inclusion of dangerous markup (copy-paste from an untrusted
source, etc.), not a substitute for trust in your admin users.

For a stricter posture, add a `Content-Security-Policy` header that disallows
inline scripts and restricts `connect-src`.

## Difference from the welcome template

| | Setup banner | Welcome template |
|---|---|---|
| Location | `/setup` page (partly public) | `CLAUDE.md` in analyst workspace |
| Format | HTML (rendered in browser) | Markdown (consumed by Claude Code) |
| Default | No banner | Ships a default at `config/claude_md_template.txt` |
| Context | `instance`, `server`, `user` (nullable), `now`, `today` | All of the above plus `tables`, `metrics`, `marketplaces`, `sync_interval`, `data_source` |
| RBAC filtering | None — same for all visitors | `marketplaces` filtered per user's group memberships |

See `docs/welcome-template.md` for the welcome-template reference.
