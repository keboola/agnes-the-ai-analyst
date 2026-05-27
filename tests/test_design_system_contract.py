"""Design-system invariants. Fails if a future PR undoes the design-pass."""

from pathlib import Path
import re


TEMPLATES = Path("app/web/templates")
STATIC = Path("app/web/static")


def _all_html() -> list[Path]:
    return sorted(p for p in TEMPLATES.rglob("*.html"))


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
# selector appears in ANY of these. Adding a new sheet means listing it
# here (or the new selectors silently fail this contract).
_CANONICAL_CSS = (
    STATIC / "style-custom.css",
    STATIC / "css" / "components.css",
    STATIC / "css" / "design-tokens.css",
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

    expected = static_classes | button_classes
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
