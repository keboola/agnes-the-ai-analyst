"""Render tests for the design-system component macros in
`app/web/templates/_components.html`. One test per macro added for the
#419 follow-up sweep — pins the public shape of each macro so a future
refactor that breaks the contract fails fast.
"""
from __future__ import annotations

import jinja2
from jinja2 import Environment, FileSystemLoader


class _SilentUndefined(jinja2.Undefined):
    """Mirror the silently-tolerant Undefined that app.web.router installs
    on the production Jinja env. Docstring-only macro examples inside
    `_components.html` reference `ds.tabs(...)` at body scope; with the
    default StrictUndefined those calls raise during module load. The
    production env returns empty string instead — replicate that here so
    these render tests exercise the same execution model."""
    def __str__(self): return ""
    def __iter__(self): return iter([])
    def __bool__(self): return False
    def __len__(self): return 0
    def __getattr__(self, name): return self
    def __getitem__(self, name): return self
    def __call__(self, *args, **kwargs): return self
    def __int__(self): return 0


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=True,
        undefined=_SilentUndefined,
    )


def _render(src: str) -> str:
    return _env().from_string(src).render()


# ---------- tabs_rich ----------


def test_tabs_rich_emits_mp_tabs_with_icons_and_counts() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.tabs_rich(
            items=[
                {'label': 'Curated', 'data_tab': 'curated', 'active': True,
                 'count_attr': 'data-count-curated',
                 'svg': '<svg class="tab-icon"></svg>'},
                {'label': 'Flea', 'data_tab': 'flea', 'active': False,
                 'count_attr': 'data-count-flea',
                 'svg': '<svg class="tab-icon"></svg>'},
            ],
            aria_label='Marketplace sections',
        ) }}
        """
    )
    assert 'class="mp-tabs"' in out
    assert 'role="tablist"' in out
    assert 'aria-label="Marketplace sections"' in out
    assert 'data-tab="curated"' in out
    assert 'aria-selected="true"' in out
    assert 'aria-selected="false"' in out
    assert 'data-count-curated' in out
    assert '<svg class="tab-icon">' in out  # autoescaped only if not |safe


def test_tabs_rich_stack_variant_emits_stack_tabs() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.tabs_rich(items=[{'label': 'X', 'data_tab': 'x', 'active': True}],
                        variant='stack') }}
        """
    )
    assert 'class="stack-tabs"' in out
    assert 'data-tab="x"' in out


# ---------- segmented_strip ----------


def test_segmented_strip_default_emits_os_tabs() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.segmented_strip(items=[
            {'label': 'macOS', 'value': 'mac', 'active': True},
            {'label': 'Linux', 'value': 'linux'},
        ]) }}
        """
    )
    assert 'class="os-tabs"' in out
    assert 'data-value="mac"' in out
    assert 'data-value="linux"' in out
    assert 'aria-selected="true"' in out


def test_segmented_strip_mode_variant() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.segmented_strip(items=[{'label': 'Admin', 'value': 'admin', 'active': True}],
                              variant='mode') }}
        """
    )
    assert 'class="mode-tabs"' in out
    assert 'data-value="admin"' in out


# ---------- pill_chip ----------


def test_pill_chip_renders_button_by_default() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.pill_chip(label='Trending', value='trending', active=True) }}
        """
    )
    assert '<button type="button"' in out
    # Class subset: 'pill' + 'is-active'
    assert 'class="pill is-active"' in out
    assert 'data-filter="trending"' in out
    assert '>Trending</button>' in out


def test_pill_chip_renders_anchor_when_href_given() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.pill_chip(label='All', href='/all') }}
        """
    )
    assert '<a class="pill" href="/all"' in out
    assert 'is-active' not in out


# ---------- kpi_card ----------


def test_kpi_card_renders_button_with_existing_obs_kpi_selectors() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.kpi_card(label='Active users', value='1,234', delta='+12%') }}
        """
    )
    assert '<button type="button" class="obs-kpi"' in out
    # Macro must align with the existing CSS selectors in
    # activity_center.css (`.obs-kpi-label/-value/-sub`) — using BEM-style
    # `__` modifiers would break visual styling.
    assert '<span class="obs-kpi-label">Active users</span>' in out
    assert '<span class="obs-kpi-value">1,234</span>' in out
    assert '<span class="obs-kpi-sub">+12%</span>' in out


def test_kpi_card_renders_anchor_when_href_given() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.kpi_card(label='Sessions', value='42', href='/admin/sessions') }}
        """
    )
    assert 'href="/admin/sessions"' in out
    assert 'role="button"' in out


# ---------- hero_search_btn ----------


def test_hero_search_btn_default_variant() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.hero_search_btn(label='Find a plugin') }}
        """
    )
    assert '<button type="submit" class="search-btn">' in out
    assert 'Find a plugin' in out


def test_hero_search_btn_stack_variant() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.hero_search_btn(label='Search stack', variant='stack', type='button') }}
        """
    )
    assert '<button type="button" class="stack-hero__search-btn">' in out


# ---------- info_panel_accent ----------


def test_info_panel_accent_emits_modifier_class_and_body() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {% call ds.info_panel_accent(title='Heads up', accent='warn') %}
          <p>Watch this.</p>
        {% endcall %}
        """
    )
    # Class subset check: macro must emit both the root and the modifier.
    assert "info-panel-accent" in out
    assert "info-panel-accent--warn" in out
    assert '<h3 class="info-panel-accent__title">Heads up</h3>' in out
    assert '<p>Watch this.</p>' in out
    assert 'role="note"' in out


def test_info_panel_accent_supports_four_canonical_accents() -> None:
    for accent in ("info", "warn", "success", "danger"):
        out = _render(
            "{% import '_components.html' as ds %}"
            f"{{{{ ds.info_panel_accent(accent='{accent}', title='X') }}}}"
        )
        assert f"info-panel-accent--{accent}" in out, accent


# ---------- code_chip ----------


def test_code_chip_renders_code_and_copy_button_by_default() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.code_chip(code='pip install agnes') }}
        """
    )
    assert '<code>pip install agnes</code>' in out
    assert 'class="btn-copy"' in out
    # The data-copy attribute carries the same payload (autoescape may
    # escape quotes but the literal "pip install agnes" still appears).
    assert 'pip install agnes' in out


def test_code_chip_can_render_without_copy_button() -> None:
    out = _render(
        """
        {% import '_components.html' as ds %}
        {{ ds.code_chip(code='ls -la', copy=False) }}
        """
    )
    assert '<code>ls -la</code>' in out
    assert 'btn-copy' not in out
