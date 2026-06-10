"""Design-system invariants. Fails if a future PR undoes the design-pass."""

from pathlib import Path
import re


TEMPLATES = Path("app/web/templates")
STATIC = Path("app/web/static")


def _all_html() -> list[Path]:
    """All HTML- or JINJA-templated files that ship with the app and may
    reference design-system tokens. Includes `*.jinja` (e.g.
    `_claude_setup_cta.jinja`) so the token-sweep regression guards
    cover them too."""
    return sorted(
        list(TEMPLATES.rglob("*.html")) + list(TEMPLATES.rglob("*.jinja"))
    )


# Match every class="..." or class='...' attribute, possibly multi-line.
# Jinja templates frequently break class attributes across lines for the
# {% if … %}is-active{% endif %} pattern, so re.DOTALL is required.
_CLASS_ATTR_RE = re.compile(r"""class\s*=\s*(["'])(.*?)\1""", re.DOTALL)


def _classes_in_template(text: str) -> set[str]:
    """Extract every literal class token used in the template. Tokenizes the
    class attribute on whitespace so multi-class attrs ("btn btn-primary")
    and multi-line attrs split cleanly. Jinja conditionals (tokens that
    contain `{{`, `{%`, `}`) are skipped — only authors' literal class
    names are returned, since constructed names can't be statically
    audited without a render."""
    tokens: set[str] = set()
    for match in _CLASS_ATTR_RE.finditer(text):
        attr_value = match.group(2)
        for tok in attr_value.split():
            if "{" in tok or "}" in tok:
                continue
            tokens.add(tok)
    return tokens


# Single class tokens. Multi-token patterns (like "modal-btn primary") are
# caught by the single-token entry (.modal-btn) — no need to special-case.
DEPRECATED_CLASSES = {
    "btn-primary-v2": "btn-primary",
    "btn-secondary-v2": "btn-secondary",
    "btn-warning": "btn-danger",
    "modal-btn": "btn + .btn-primary / .btn-secondary",
    "users-table": "data-table",
    "gp-table": "data-table",
    "marketplaces-table": "data-table",
    "audit-table": "data-table",
    "stats-table": "data-table",
    "users-search": "search-input",
    "marketplaces-search": "search-input",
    "kb-search": "search-input",
    "filters-card": "filter-bar",
}


def test_style_css_deleted() -> None:
    """style.css must stay deleted — all rules live in style-custom.css."""
    assert not (STATIC / "style.css").exists(), (
        "style.css must stay deleted — all rules live in style-custom.css"
    )


def test_no_template_references_style_css() -> None:
    """No template should link the deleted stylesheet."""
    offenders: list[str] = []
    for path in _all_html():
        text = path.read_text(encoding="utf-8")
        if "static_url('style.css')" in text or 'static_url("style.css")' in text:
            offenders.append(str(path))
    assert not offenders, f"templates still link style.css: {offenders}"


def test_style_custom_has_single_root_block() -> None:
    """Exactly one :root { … } block (plus optional :root[data-theme] siblings).
    Multiple bare :root blocks signal a merge gone wrong — the cascade order
    becomes load-bearing for tokens, which we don't want."""
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    # Match :root { (no attribute selectors after it).
    bare_root = re.findall(r"^:root\s*\{", css, flags=re.MULTILINE)
    assert len(bare_root) == 1, (
        f"expected exactly one bare :root block, found {len(bare_root)}"
    )


def test_canonical_primitives_defined() -> None:
    """Every primitive the design-pass migration produces must be declared
    in style-custom.css. Tasks 4–7 introduce them; this test starts failing
    after Task 3 lands and goes green when the last primitive lands."""
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    required = [
        # buttons
        ".btn",
        ".btn-primary",
        ".btn-secondary",
        ".btn-ghost",
        ".btn-danger",
        ".btn-required",
        # form controls
        ".search-input",
        ".filter-bar",
        ".filter-pill",
        # page header
        ".page-header",
        ".page-header__title",
        ".page-header__subtitle",
        ".page-header__actions",
        # data display
        ".data-table",
        ".empty-state",
        # global feedback
        ".toast",
    ]
    missing = [sel for sel in required if sel not in css]
    assert not missing, f"missing canonical primitive selectors: {missing}"


def test_no_deprecated_class_in_templates() -> None:
    """Templates must use canonical primitives, not legacy aliases.

    Migration tasks (8–15) drive this to green by sweeping each page; Task
    16 removes the supporting CSS aliases. A regression that re-adds one of
    these class names fails the build.
    """
    offenders: dict[str, list[str]] = {}
    for path in _all_html():
        text = path.read_text(encoding="utf-8")
        used = _classes_in_template(text)
        for cls in DEPRECATED_CLASSES:
            if cls in used:
                offenders.setdefault(cls, []).append(path.name)
    assert not offenders, (
        "deprecated classes found in templates:\n"
        + "\n".join(
            f"  .{cls} → use {DEPRECATED_CLASSES[cls]} ({sorted(files)})"
            for cls, files in offenders.items()
        )
    )


_LEGACY_TOKEN_FALLBACK_ALLOWLIST: set[str] = set()
# Allowlist drained — every template now references --ds-primary explicitly
# (#419 follow-up sweep). The stricter
# `test_no_unprefixed_primary_token_in_templates` guards regressions; the
# old `var(--primary, #hex)` fallback pattern this test catches is no
# longer present in any tracked file. Re-populate if a future PR
# legitimately needs an interim fallback.


def test_no_legacy_primary_token_with_hex_fallback() -> None:
    """var(--primary, #XXXXXX) encodes the old blue colour as a fallback.
    If the compat shim in design-tokens.css is ever removed the fallback
    fires and the element reverts to blue. Use var(--ds-primary) instead.

    Files in _LEGACY_TOKEN_FALLBACK_ALLOWLIST are known-unconverted templates
    tracked for cleanup in dedicated follow-up PRs — remove from the list
    as each template is converted."""
    pattern = re.compile(r"var\(--primary\s*,\s*#")
    offenders: list[str] = []
    for path in _all_html():
        if path.name in _LEGACY_TOKEN_FALLBACK_ALLOWLIST:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert not offenders, (
        "var(--primary, #<hex>) found — use var(--ds-primary) instead:\n"
        + "\n".join(f"  {p}" for p in offenders)
    )


_NO_RAW_HEX_TEMPLATES = (
    "profile.html",
    "setup.html",
    "me_activity.html",
)


def test_swept_templates_use_no_raw_hex() -> None:
    """The #419 follow-up sweep targets three templates that previously
    inlined raw `#RRGGBB` color literals. After conversion every colour
    must reference a `--ds-*` token instead — adding a new raw hex regress
    the sweep silently otherwise."""
    pattern = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")
    offenders: dict[str, list[str]] = {}
    for name in _NO_RAW_HEX_TEMPLATES:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        hexes = pattern.findall(text)
        if hexes:
            offenders[name] = hexes
    assert not offenders, (
        "raw hex literals found in swept templates:\n"
        + "\n".join(f"  {n}: {hs}" for n, hs in offenders.items())
    )


def test_no_unprefixed_primary_token_in_templates() -> None:
    """`var(--primary)` (no `--ds-` prefix) rides the legacy blue token via
    the compat shim in design-tokens.css. Explicit `var(--ds-primary)`
    reads self-documenting in code review and survives a future shim
    removal.

    Per #419 follow-up sweep: every template MUST reference `--ds-primary`
    explicitly. `base.html` and `base_ds.html` are exempt — both only
    mention `--primary` inside CSS-comment blocks documenting the legacy
    compat shim, not as live token references.
    """
    pattern = re.compile(r"var\(\s*--primary[-)\s,]")
    exempt = {"base.html", "base_ds.html"}
    offenders: list[str] = []
    for path in _all_html():
        if path.name in exempt:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert not offenders, (
        "`var(--primary…)` found — use `var(--ds-primary…)` instead:\n"
        + "\n".join(f"  {p}" for p in offenders)
    )


_COMPONENTS_HTML = TEMPLATES / "_components.html"

# Button macro composes ['btn', 'btn-' ~ variant] + optional 'btn-' ~ size
# + 'btn--icon' (see _components.html:44). These are the variants actually
# emitted across the codebase today — re-survey if a new variant is added.
_BUTTON_VARIANTS = ("primary", "secondary", "ghost", "danger", "google", "required")
_BUTTON_SIZES = ("sm", "lg")

# CSS files where canonical rules live. Class-coverage is satisfied if the
# selector appears in ANY of these. The four sheets imported by base.html
# and base_ds.html (style-custom + components + design-tokens + stack_card)
# are globally loaded; the per-page sheets under `css/*.css` ship with the
# pages whose macros use them — coverage is still satisfied because the
# macro emits the class only on pages that load the matching sheet.
_CANONICAL_CSS = (
    STATIC / "style-custom.css",
    *sorted((STATIC / "css").glob("*.css")),
)


def test_component_macros_emit_only_classes_with_css_rules() -> None:
    """Every class token a macro in `_components.html` emits MUST resolve
    to a CSS rule in one of the canonical sheets (style-custom.css,
    components.css, design-tokens.css). A typo'd class on a macro renders
    nothing — this contract catches it before the macro ships.

    Approach: static extraction (no Jinja render). Literal classes are
    pulled from `class="…"` attribute values in `_components.html`,
    Jinja-templated portions (`{{ … }}` / `{% … %}`) skipped, and the
    button macro's computed `btn-<variant>` / `btn-<size>` classes are
    enumerated from the documented variant tuples above.
    """
    text = _COMPONENTS_HTML.read_text(encoding="utf-8")

    # Strip Jinja blocks/expressions/comments before tokenising — we only
    # want the literal class strings the author wrote, not Jinja runtime
    # gunk or comment-block examples (`{# … class="…" … #}`).
    jinja_free = re.sub(
        r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", " ", text, flags=re.DOTALL,
    )

    static_classes: set[str] = set()
    for m in _CLASS_ATTR_RE.finditer(jinja_free):
        for token in m.group(2).split():
            if "{" in token or "}" in token:
                continue
            static_classes.add(token)

    # Button macro variants + sizes that get composed at runtime.
    button_classes = {"btn", "btn--icon"}
    button_classes.update(f"btn-{v}" for v in _BUTTON_VARIANTS)
    button_classes.update(f"btn-{s}" for s in _BUTTON_SIZES)

    # T11-T17 macros compose variant-driven root classes (variant arg ⇒
    # different selector) and bespoke accent modifiers. Enumerate the
    # documented variant values explicitly so a typo in the macro fails
    # this contract loudly.
    variant_classes: set[str] = {
        # tabs_rich
        "mp-tabs", "stack-tabs",
        # segmented_strip
        "os-tabs", "mode-tabs",
        # hero_search_btn
        "search-btn", "stack-hero__search-btn",
        # info_panel_accent — all four canonical accents
        "info-panel-accent",
        "info-panel-accent--info", "info-panel-accent--warn",
        "info-panel-accent--success", "info-panel-accent--danger",
    }

    expected = static_classes | button_classes | variant_classes
    assert expected, "extracted no classes from _components.html — extraction broken"

    # Load every canonical sheet once.
    css_blob = "\n".join(p.read_text(encoding="utf-8") for p in _CANONICAL_CSS)

    missing: list[str] = []
    for cls in sorted(expected):
        # Selector match: `.cls` followed by a non-class-name char so
        # `.btn` doesn't match `.btn-primary`. CSS rules also appear in
        # compound selectors (`.btn.is-active`) — the simple lookahead
        # is enough because we only need ONE occurrence.
        if not re.search(r"\." + re.escape(cls) + r"(?![\w-])", css_blob):
            missing.append(cls)
    assert not missing, (
        f"_components.html emits classes with no CSS rule in any of "
        f"{[str(p) for p in _CANONICAL_CSS]}:\n"
        + "\n".join(f"  .{m}" for m in missing)
    )


def test_app_js_referenced_by_base_only() -> None:
    """app.js carries dropdown wiring scoped to the authed nav. base_login.html
    has no nav, so it must NOT load app.js — that would let login pages call
    window.appUI / window.appToast (defined later), which is not their
    contract. The opposite (base.html missing app.js) would break the
    Admin dropdown."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    base_login = (TEMPLATES / "base_login.html").read_text(encoding="utf-8")
    assert "app.js" in base, "base.html must load app.js"
    assert "app.js" not in base_login, "base_login.html must not load app.js"


# Helper-level unit tests for the class-tokenizer itself — keeps the
# audit logic honest as the design-pass evolves.

def test_classes_helper_multiline_attr() -> None:
    """class= attributes split across lines (typical Jinja conditional
    pattern) must still tokenize cleanly."""
    sample = '''
    <a class="app-nav-link
        is-active"
       href="/">Home</a>
    '''
    assert _classes_in_template(sample) == {"app-nav-link", "is-active"}


def test_classes_helper_skips_jinja_tokens() -> None:
    """Jinja-constructed class fragments don't get audited (can't be statically
    resolved). Verify the {% if %}, {{ … }} pieces are filtered out, real
    literal tokens around them stay."""
    sample = '''<button class="btn {% if active %}is-active{% endif %} btn-primary">Go</button>'''
    tokens = _classes_in_template(sample)
    assert "btn" in tokens
    assert "btn-primary" in tokens
    # Jinja control-flow tokens get skipped — they contain `{` or `}`.
    for tok in tokens:
        assert "{" not in tok and "}" not in tok


def test_classes_helper_compound_match_is_not_false_positive() -> None:
    """Prose containing the word 'pill' or 'btn' in a comment should NOT be
    detected as a deprecated class. Only class= attribute values count."""
    sample = '''
    <!-- this is the filter pill row -->
    <p>The button (btn) below opens the menu.</p>
    <span class="badge">x</span>
    '''
    assert _classes_in_template(sample) == {"badge"}


# --------------------------------------------------------------------------- #
# #367 — page-shell layout guards.
#
# Leaf templates must not re-introduce the per-page chrome drift the
# design-system page-shell (`base_page.html` / `base_ds.html` + `.container`)
# exists to remove: container opt-outs and bare `:root` token-shadow blocks.
# The canonical bases + theme shim are exempt.
#
# NOTE: a broad `.X-page { max-width }` fence is intentionally NOT added yet —
# many pages still carry legitimate inner-width wrappers pending the full
# base_page.html migration (tracked as a #367 follow-up). It would false-
# positive today.
# --------------------------------------------------------------------------- #

# Templates allowed to own layout/theme CSS (the canonical bases + theme shim).
_CANONICAL_LAYOUT_FILES = {"base.html", "base_ds.html", "base_page.html", "_theme.html"}

_JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)
_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL | re.IGNORECASE)


def _inline_styles_in_template(text: str) -> str:
    """Concatenated content of a template's inline <style> blocks, with Jinja
    expressions / blocks / comments stripped first so documented examples
    inside `{# … #}` don't trip the scanners."""
    jinja_free = _JINJA_RE.sub(" ", text)
    return "\n".join(_STYLE_BLOCK_RE.findall(jinja_free))


def test_no_container_has_optout_in_leaf_templates() -> None:
    """`.container:has(.X-page) { max-width: none }` is the per-page container
    opt-out the page-shell replaced (#367). Width changes belong on the
    canonical `.container--wide/--narrow/--full` modifiers, not a leaf opt-out."""
    offenders: list[str] = []
    for path in _all_html():
        if path.name in _CANONICAL_LAYOUT_FILES:
            continue
        if ".container:has(" in _inline_styles_in_template(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert not offenders, (
        "`.container:has(` opt-out found in a leaf template — use a canonical "
        "`.container--wide/--narrow/--full` modifier instead:\n"
        + "\n".join(f"  {p}" for p in offenders)
    )


def test_no_bare_root_block_in_leaf_templates() -> None:
    """A bare `:root { … }` block in a leaf template shadows the canonical
    design tokens (cf. the `admin_tables :root{--primary:…}` collapse, #367).
    Only `_theme.html` and the bases may declare `:root`."""
    pattern = re.compile(r":root\s*\{")
    offenders: list[str] = []
    for path in _all_html():
        if path.name in _CANONICAL_LAYOUT_FILES:
            continue
        if pattern.search(_inline_styles_in_template(path.read_text(encoding="utf-8"))):
            offenders.append(str(path))
    assert not offenders, (
        "bare `:root {` block found in a leaf template — design tokens live in "
        "design-tokens.css / _theme.html, not per-page:\n"
        + "\n".join(f"  {p}" for p in offenders)
    )


def test_base_ds_carries_operator_custom_scripts() -> None:
    """`base_ds.html` (and thus `base_page.html`) must fire all three operator
    `custom_scripts` placements like `base.html` does. Without them every page
    migrated onto the design-system base silently drops operator-injected
    analytics / feedback widgets (#367 base_ds parity; surfaced during the #482
    page migration). `test_custom_scripts_render.py` proves the loop mechanism
    renders; this guard just keeps the loops present in base_ds."""
    text = (TEMPLATES / "base_ds.html").read_text(encoding="utf-8")
    missing = [
        p for p in ("head_start", "head_end", "body_end")
        if f"s.placement == '{p}'" not in text
    ]
    assert not missing, (
        f"base_ds.html is missing operator custom_scripts loop(s): {missing} — "
        "pages migrated onto base_ds will drop operator scripts"
    )


# Bases that legitimately own the <html>/<head>/<body> scaffold. Every other
# page-level template must `{% extends %}` one of these (directly, or via the
# base_page → base_ds chain) rather than ship its own standalone document.
_PAGE_SCAFFOLD_BASES = {
    "base.html",
    "base_ds.html",
    "base_page.html",
    "base_login.html",
}

# Leaf pages still on a standalone scaffold, pending migration onto the
# design-system base. Entries are tolerated ONLY so the guard locks in today's
# state — drop an entry when its page is migrated, and never add a new one (a
# fresh standalone is exactly the regression this guard exists to block).
_STANDALONE_ALLOWLIST = {"admin_chat.html"}

_EXTENDS_RE = re.compile(r"\{%-?\s*extends")
_SCAFFOLD_RE = re.compile(r"<!DOCTYPE|<html[\s>]", re.IGNORECASE)


def test_no_new_standalone_page_templates() -> None:
    """Page-level templates must extend a design-system base, not ship their
    own <html>/<head>/<body>. Anti-regression guard for the standalone→base_ds
    migration (#284/#481/#482): before it, shared infrastructure (app.js, the
    theme include, the nav, the Inter font) lived only in base.html, so any
    standalone page silently lost it — the original dead Admin-dropdown bug on
    /catalog, /admin/tables, /corporate-memory. Partials (`_`-prefixed) and the
    bases themselves are exempt; known-standalone leaves sit in
    _STANDALONE_ALLOWLIST until migrated."""
    offenders: list[str] = []
    for path in _all_html():
        name = path.name
        if (
            name.startswith("_")
            or name in _PAGE_SCAFFOLD_BASES
            or name in _STANDALONE_ALLOWLIST
        ):
            continue
        text = path.read_text(encoding="utf-8")
        if _EXTENDS_RE.search(text):
            continue
        # No `{% extends %}`: a regression only if it ships a real page
        # scaffold. A non-`_` include fragment without one is harmless.
        if _SCAFFOLD_RE.search(text):
            offenders.append(str(path.relative_to(TEMPLATES)))
    assert not offenders, (
        "standalone page template(s) found — extend a design-system base "
        "(base_page.html / base_ds.html) instead of shipping your own "
        "<html>/<head>/<body>:\n" + "\n".join(f"  {o}" for o in offenders)
    )


def test_setup_html_uses_design_system_base() -> None:
    """The first-time-setup wizard (`setup.html`, served at /first-time-setup)
    must ride the canonical design-system base, not the bespoke
    `base_login.html` card chrome (#586). It opts into the 800px narrow shell
    via `.container--narrow` and carries none of the login-card wrapper divs
    or their hardcoded `max-width: 520px` inline widths."""
    text = (TEMPLATES / "setup.html").read_text(encoding="utf-8")
    # (a) extends the design-system base, not base_login.
    assert '{% extends "base_ds.html" %}' in text, (
        "setup.html must extend base_ds.html"
    )
    assert "base_login.html" not in text, (
        "setup.html must no longer reference base_login.html"
    )
    # (b) the hardcoded card width is gone (both inline occurrences).
    assert "max-width: 520px" not in text, (
        "setup.html must not hardcode `max-width: 520px`"
    )
    # (c) opts into the canonical narrow shell.
    assert "container--narrow" in text, (
        "setup.html must opt into the .container--narrow design-system shell"
    )
    # (d) the login-card chrome wrapper divs are removed.
    for cls in ('class="login-page"', 'class="login-card-wrapper"',
                'class="login-card"'):
        assert cls not in text, (
            f"setup.html must not carry the login-chrome wrapper ({cls})"
        )


def test_standalone_allowlist_has_no_stale_entries() -> None:
    """Every _STANDALONE_ALLOWLIST entry must still exist AND still be a
    standalone (no `{% extends %}`). When a page is migrated onto a base its
    allowlist entry goes stale — this fails so the entry is removed, keeping
    the allowlist honest instead of silently masking a now-compliant page."""
    stale: list[str] = []
    for name in sorted(_STANDALONE_ALLOWLIST):
        path = TEMPLATES / name
        if not path.exists() or _EXTENDS_RE.search(path.read_text(encoding="utf-8")):
            stale.append(name)
    assert not stale, (
        "stale _STANDALONE_ALLOWLIST entr(ies) — page migrated or removed, "
        f"drop from the allowlist: {stale}"
    )
