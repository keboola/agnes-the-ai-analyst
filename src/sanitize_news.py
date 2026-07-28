"""HTML sanitizer for the admin-edited news entity.

The /home news perex + /news full body are admin-authored HTML rendered
to every authenticated user, so the sanitizer is the security boundary.
nh3 (Rust-backed ammonia) is used in allowlist mode: anything not on
the explicit per-tag attribute list is dropped.

Iframe support is gated to a small list of video providers (YouTube,
Vimeo, Loom). The pre-pass strips any iframe whose `src` is missing or
not in the allowlist BEFORE handing to nh3 — nh3's own `attribute_filter`
can drop attributes but not whole elements, so a pre-pass is the
simplest way to enforce "iframe only when src is YouTube/Vimeo/Loom."

The sanitizer is invoked once on save (in the repository's `save_draft`)
before the row is written. Templates render with `{{ x | safe }}` and
trust the stored content — no second-pass sanitization on read.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import nh3


# Tag allowlist for nh3.
_ALLOWED_TAGS: set[str] = {
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "strong", "em", "b", "i", "u", "s",
    "code", "pre", "blockquote",
    "a", "img",
    "span", "div", "section",
    "table", "thead", "tbody", "tr", "th", "td",
    "details", "summary",
    "figure", "figcaption",
    "iframe",
}


# Per-tag attribute allowlist. Anything not listed here is stripped by nh3.
_ATTR_CLASS_TARGETS = {"span", "div", "section", "p",
                       "h1", "h2", "h3", "h4", "h5", "h6",
                       "table", "td", "th", "blockquote", "a"}

_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    # `rel` is managed by nh3's `link_rel="noopener noreferrer"` and must
    # NOT appear in this list (nh3 raises ValueError otherwise).
    "a":      {"href", "title", "target", "class"},
    "img":    {"src", "alt", "width", "height"},
    "iframe": {"src", "title", "width", "height", "allow",
               "allowfullscreen", "frameborder"},
}
for _tag in _ATTR_CLASS_TARGETS:
    _ALLOWED_ATTRIBUTES.setdefault(_tag, set()).add("class")


# URL scheme allowlist applied to <a href> / <img src>.
_ALLOWED_URL_SCHEMES: set[str] = {"http", "https", "mailto"}


# Iframe host allowlist — `src` must start with one of these prefixes
# (scheme + host + the leading path segment). Pre-pass drops the whole
# iframe element if `src` is missing or fails this check.
_IFRAME_SRC_PREFIXES: tuple[str, ...] = (
    "https://www.youtube.com/embed/",
    "https://youtube.com/embed/",
    "https://www.youtube-nocookie.com/embed/",
    "https://youtube-nocookie.com/embed/",
    "https://player.vimeo.com/video/",
    "https://www.loom.com/embed/",
    "https://www.loom.com/share/",
)


# Pre-pass regex matching opening <iframe ...>, with `re.DOTALL` so multi-
# line attributes are handled. We strip the WHOLE element (open tag,
# inner content, close tag) when the src doesn't pass the host check.
_IFRAME_OPEN_RE = re.compile(r"<iframe\b[^>]*>", re.IGNORECASE | re.DOTALL)
_SRC_ATTR_RE = re.compile(
    r'\bsrc\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))',
    re.IGNORECASE,
)


def _iframe_src_allowed(open_tag: str) -> bool:
    """Return True if the `src=` value on an iframe open-tag matches the
    video-host allowlist; False on missing src, malformed src, or
    out-of-allowlist src."""
    m = _SRC_ATTR_RE.search(open_tag)
    if not m:
        return False
    src = (m.group(1) or m.group(2) or m.group(3) or "").strip()
    if not src:
        return False
    return any(src.startswith(prefix) for prefix in _IFRAME_SRC_PREFIXES)


def _strip_disallowed_iframes(html: str) -> str:
    """Remove `<iframe>...</iframe>` blocks whose src is not in the
    video-host allowlist. nh3 then sees only the surviving iframes plus
    the rest of the document untouched.

    The walk is destructive (rewrites the string position by position)
    rather than re.sub-based so we can match the close tag cleanly even
    when iframes contain inner whitespace / nested children (rare but
    legal in HTML5)."""
    out_parts: list[str] = []
    i = 0
    while True:
        m = _IFRAME_OPEN_RE.search(html, i)
        if not m:
            out_parts.append(html[i:])
            break
        # Emit text before the iframe.
        out_parts.append(html[i:m.start()])
        open_end = m.end()
        # Find the matching </iframe> (case-insensitive). HTML5 disallows
        # nesting iframes, so the next close tag is the matching one.
        close_re = re.compile(r"</iframe\s*>", re.IGNORECASE)
        close_m = close_re.search(html, open_end)
        if close_m:
            inner_close_end = close_m.end()
        else:
            # Unclosed iframe — drop the rest of the document defensively.
            inner_close_end = len(html)

        if _iframe_src_allowed(m.group(0)):
            out_parts.append(html[m.start():inner_close_end])
        # else: drop the whole iframe element (open tag + body + close tag).
        i = inner_close_end
    return "".join(out_parts)


def sanitize(html: str | None) -> str:
    """Sanitize `html` against the news allowlist. Returns "" for None / "".

    The two-stage pipeline is: (1) strip non-allowlisted iframes via
    regex pre-pass, (2) hand the survivors to nh3 with the tag /
    attribute / url-scheme allowlists. nh3 enforces every other rule —
    event handlers stripped, javascript:/data: schemes blocked, unknown
    tags removed, comments stripped.
    """
    if not html:
        return ""
    pre = _strip_disallowed_iframes(html)
    return nh3.clean(
        pre,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


def stripped_text(html: str | None, limit: int = 120) -> str:
    """Return a plain-text preview of `html` clamped to `limit` chars.

    Used by the admin UI's versions table where each row shows a short
    preview of the intro + body. Strips ALL tags, then collapses
    whitespace and truncates with an ellipsis.
    """
    if not html:
        return ""
    plain = nh3.clean(html, tags=set(), attributes={}, strip_comments=True)
    plain = " ".join(plain.split()).strip()
    if len(plain) > limit:
        return plain[: limit - 1].rstrip() + "…"
    return plain


__all__ = ["sanitize", "stripped_text"]
