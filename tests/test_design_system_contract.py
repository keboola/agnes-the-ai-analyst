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
    "modal-btn": "btn + .btn-primary / .btn-secondary",
    "users-table": "data-table",
    "gp-table": "data-table",
    "marketplaces-table": "data-table",
    "audit-table": "data-table",
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
