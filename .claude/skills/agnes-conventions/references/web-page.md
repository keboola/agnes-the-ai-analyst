# Playbook: new HTML dashboard page (design-system shell)

## Which base

- **Extend `base_page.html`** for a standard page (hero strip + toolbar + body).
  It extends `base_ds.html` and gives you the three-section shell.
- Extend `base_ds.html` directly only for bespoke full-width layout (override
  `{% block layout %}`).
- **Never `base.html`** — legacy. `ds.*` macros are auto-imported
  (`app/web/templates/base_ds.html:78`) — no `{% import %}` needed.

## Files

1. `app/web/templates/<page>.html`
2. `app/web/router.py` — a route handler.

## Template skeleton

```html
{% extends "base_page.html" %}
{% block title %}My Page — {{ config.INSTANCE_NAME }}{% endblock %}
{% set page_hero_eyebrow = "Section" %}
{% set page_hero_title = "My Page" %}
{% set page_hero_subtitle = "One line." %}
{% block head_extra %}<style>/* page-local CSS, see rules */</style>{% endblock %}
{% block toolbar %}{{ ds.button('+ Add', variant='primary') }}{% endblock %}
{% block page %}<table class="data-table">…</table>{% endblock %}
{% block scripts %}<script>/* page JS */</script>{% endblock %}
```

Wider shell: `{% block container_modifier %}container--wide{% endblock %}`.

## Route (`app/web/router.py`)

```python
@router.get("/my-page", response_class=HTMLResponse)
async def my_page(request: Request, user: dict = Depends(require_admin),
                  conn: duckdb.DuckDBPyConnection = Depends(_get_db)):
    ctx = _build_context(request, user=user, conn=conn, my_data=...)
    return templates.TemplateResponse(request, "my_page.html", ctx)
```

Real pattern: `app/web/router.py` `admin_users_page` (~`:2409`).

## Visual standard

Read `references/design-system.md` before styling anything — tokens
(`--ds-*`), theme switch (`paper`), chrome layouts (topnav/rail), and
the accent vocabularies (brand vs kind vs assistant vs status) are
binding for all UI work.

## CSS rules (enforced by `tests/test_design_system_contract.py`)

Use canonical classes (`.btn`, `.btn-primary`, `.search-input`, `.data-table`,
`.empty-state`, …). **Banned:** `var(--primary)` → use `var(--ds-primary)`; raw
`#RRGGBB` hex → use `var(--ds-*)`; `.container:has(.X-page)` opt-out → use the
`container--wide`/`--narrow` modifier block; bare `:root{}` in a leaf template
(only base/theme files may); deprecated aliases (`.modal-btn`, `.users-table`,
`.btn-warning`).

## Steps

1. TDD: a route test asserting 200 + the page renders a key element; the
   design-system contract test will also run against the new template.
2. Create the template (extend `base_page.html`) + the route.
3. Green both.

## Anchors

- bases: `app/web/templates/base_ds.html:78`, `app/web/templates/base_page.html:33`
- real page: `app/web/templates/admin_users.html:1`
- route: `app/web/router.py:2409`
- contract: `tests/test_design_system_contract.py:397`
