"""Custom design-system dropdown on My Workspace's "Sort your workspace"
control (#1055).

`app/web/templates/workspace.html` currently has no route wired up (no
`app.web.router` handler renders it), so — unlike the other #1055 dropdown
conversions, which are exercised end-to-end through `seeded_app["client"]` —
these tests extract the real `#ws-sort` toolbar markup straight out of the
template file and render just that snippet through a bare Jinja environment
(the same "render the macro standalone" pattern as
`tests/test_web_stack_card_macro.py` / `tests/test_web_components_macros.py`),
so a typo'd macro call is still caught by the actual expanded HTML rather
than by a source-text substring match.

`#ws-sort` stays a real `<select>` (existing `sortSelect`/`applySort()` JS
wiring is untouched) with a `ds.dropdown()` custom button+menu alongside it.
`#ws-owner` is intentionally left a plain `<select>`: its options are rebuilt
at runtime from the rendered rows (`rebuildOwners()`), including rows added
later by an async plugin fetch, so there is no fixed set of {value, label}
pairs to hand the `dropdown()` macro, and `ds_dropdown.js` reads its paired
select's option list once at bootstrap with no hook to resync when that list
is rebuilt.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
from jinja2 import Environment, FileSystemLoader

TEMPLATE = Path(__file__).resolve().parent.parent / "app" / "web" / "templates" / "workspace.html"


class _SilentUndefined(jinja2.Undefined):
    """Mirror the silently-tolerant Undefined app.web.router installs on the
    production Jinja env — see test_web_components_macros.py. Docstring-only
    macro examples inside `_components.html` reference `ds.tabs(...)` etc. at
    body scope; with the default StrictUndefined those calls raise on import
    of `_components.html` itself, even though our snippet never uses them."""

    def __str__(self):
        return ""

    def __iter__(self):
        return iter([])

    def __bool__(self):
        return False

    def __len__(self):
        return 0

    def __getattr__(self, name):
        return self

    def __getitem__(self, name):
        return self

    def __call__(self, *args, **kwargs):
        return self

    def __int__(self):
        return 0


def _source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _extract_sort_control(text: str) -> str:
    """Pull the `<label class="ws-select">…</label>` block that wraps
    `#ws-sort` (the second of the two `.ws-select` labels — the first is
    `#ws-owner`) out of the real template source."""
    marker = 'id="ws-sort"'
    idx = text.index(marker)
    start = text.rindex('<label class="ws-select">', 0, idx)
    end = text.index("</label>", idx) + len("</label>")
    return text[start:end]


def _render_sort_control() -> str:
    env = Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=True,
        undefined=_SilentUndefined,
    )
    snippet = _extract_sort_control(_source())
    tmpl = env.from_string("{% import '_components.html' as ds %}" + snippet)
    return tmpl.render()


class TestWorkspaceSortDropdown:
    def test_native_select_still_renders_for_existing_js_wiring(self) -> None:
        text = _render_sort_control()
        assert 'id="ws-sort" aria-label="Sort your workspace" class="ds-dropdown-native"' in text
        assert '<option value="added" selected>Recently updated</option>' in text

    def test_custom_dropdown_markup_present(self) -> None:
        text = _render_sort_control()
        assert 'class="ds-dropdown"' in text
        assert 'data-ds-dropdown-target="ws-sort"' in text
        assert 'id="ws-sort-dd-btn"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'aria-controls="ws-sort-dd-menu"' in text
        assert 'id="ws-sort-dd-menu"' in text
        assert 'role="menu"' in text
        assert 'role="menuitemradio"' in text
        for value in ("added", "name", "type"):
            assert f'data-value="{value}"' in text
        # Default selection (matches the native select's initial `selected`).
        assert 'aria-checked="true"' in text

    def test_dropdown_js_module_is_loaded(self) -> None:
        assert "js/components/ds_dropdown.js" in _source()

    def test_owner_select_is_left_alone(self) -> None:
        """`#ws-owner` has no fixed option list (populated by `rebuildOwners()`
        at runtime), so it is NOT converted — no paired ds-dropdown wrapper."""
        text = _source()
        assert '<select id="ws-owner" aria-label="Filter by owner">' in text
        assert 'data-ds-dropdown-target="ws-owner"' not in text
