"""Guard: no pictographic emoji in user-facing HTML templates.

Policy (issue: "ban emojis in the UI, use meaningful icons"): analyst-facing
pages must use the shared line-icon set (`macros/_icon.html` / `ico(...)` in
`macros/_detail.html`) instead of pictographic emoji, which read as childish
in a B2B product. Admin-set entity glyphs render as 2-letter initials, never
the stored emoji.

Scope of the ban (deliberately narrow):
  - BANNED: true pictographic emoji — U+1F000–U+1FAFF (📦 🎯 🧠 📄 …), plus
    ✅ (U+2705) and ⚠ (U+26A0), which render as colour emoji on most
    platforms, plus the emoji variation selector U+FE0F.
  - ALLOWED: typographic symbols that are normal UI text — arrows
    (→ ← ↗ ↓ …) and the monochrome ✓ / ✗ / ✕ marks. Converting those to
    SVG across every button/link would be a large, low-value churn.

`ALLOWLIST` is now drained — every user-facing template, including the admin
surfaces and the async-hydrated marketplace detail pages, has been swept. The
list may only shrink: do not add to it. A new template that needs a glyph must
use the shared line-icon set, not a pictographic emoji.
"""

import re
from pathlib import Path

TEMPLATES = Path("app/web/templates")

_EMOJI = re.compile("[\U0001f000-\U0001faff\U00002705\U000026a0\U0000fe0f]")

# Drained — every user-facing template is swept. Shrink only, never grow.
ALLOWLIST: set[str] = set()


# Frozen byte-for-byte copies of the pre-redesign pages, kept so a default
# topnav instance renders exactly what it rendered before the redesign
# (tests/test_ui_layout_theme.py::TestDefaultContentParity). They may not
# drift — including cosmetic emoji sweeps — and they retire together with the
# topnav chrome. A CLOSED set on purpose: a new template cannot dodge the ban
# by taking a `_legacy` name (test_legacy_exemption_is_a_closed_set).
LEGACY_FROZEN: set[str] = {
    "library_legacy.html",
    "marketplace_legacy.html",
    "catalog_table_detail_legacy.html",
    "catalog_package_detail_legacy.html",
    "catalog_recipe_detail_legacy.html",
    "marketplace_plugin_detail_legacy.html",
    "marketplace_item_detail_legacy.html",
    "library_detail_legacy.html",
    "memory_domain_detail_legacy.html",
    "catalog_legacy.html",
    "corporate_memory_legacy.html",
    "profile_legacy.html",
    "me_activity_legacy.html",
    "agents_legacy.html",
    "me_cowork_legacy.html",
    "_tour_legacy.html",
    "_chat_welcome_cards_legacy.html",
    "_profile_tokens_legacy.html",
    "_profile_troubleshooting_legacy.html",
}


def _in_scope(rel: str) -> bool:
    if rel in LEGACY_FROZEN:
        return False
    return rel not in ALLOWLIST


def test_legacy_exemption_is_a_closed_set() -> None:
    """Every `*_legacy.html` on disk must be one of the known frozen copies —
    the exemption is for pinned pre-redesign pages, not a naming loophole."""
    on_disk = {p.name for p in TEMPLATES.rglob("*_legacy.html")}
    assert on_disk <= LEGACY_FROZEN, (
        "unexpected *_legacy.html templates (either a naming dodge of the emoji "
        f"ban, or extend LEGACY_FROZEN deliberately): {sorted(on_disk - LEGACY_FROZEN)}"
    )


def test_no_pictographic_emoji_in_user_facing_templates() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        rel = str(path.relative_to(TEMPLATES))
        if not _in_scope(rel):
            continue
        hits = []
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _EMOJI.search(line):
                hits.append(f"    L{lineno}: {line.strip()[:80]}")
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "Pictographic emoji found in user-facing templates — use a meaningful "
        "icon from macros/_icon.html (`ico.icon('name')`) instead:\n"
        + "\n".join(f"  {f}\n" + "\n".join(lines) for f, lines in offenders.items())
    )


def test_allowlist_entries_still_exist() -> None:
    """Keep ALLOWLIST honest — a renamed/deleted entry should fail loudly so
    the list can't silently rot."""
    missing = [n for n in ALLOWLIST if not (TEMPLATES / n).exists()]
    assert not missing, f"ALLOWLIST names no longer present (remove them): {missing}"
