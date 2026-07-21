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

`ALLOWLIST` holds the surfaces not yet swept — admin pages and the two
async-hydrated marketplace detail templates (folded in with their own
redesign pass). The list may only shrink: do not add to it.
"""
import re
from pathlib import Path

TEMPLATES = Path("app/web/templates")

_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002705\U000026A0\U0000FE0F]")

# Not yet swept — tracked for follow-up. Shrink this, never grow it.
ALLOWLIST = {
    "marketplace_plugin_detail.html",  # async-hydrated; own redesign pass
    "marketplace_item_detail.html",    # async-hydrated; own redesign pass
}


def _in_scope(rel: str) -> bool:
    if rel.startswith("admin_"):  # admin surfaces: separate follow-up sweep
        return False
    return rel not in ALLOWLIST


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
