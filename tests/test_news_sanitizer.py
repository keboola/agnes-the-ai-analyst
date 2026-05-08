"""Sanitizer tests for src.sanitize_news.sanitize.

Bypass-shape coverage matches the security review for the legacy
welcome-template regex sanitizer; closing all four classes is the
reason we adopted nh3 for the news pipeline.
"""

from __future__ import annotations

from src.sanitize_news import sanitize, stripped_text


def test_basic_prose_round_trip():
    assert sanitize("<p>Hello <strong>world</strong></p>") == "<p>Hello <strong>world</strong></p>"


def test_script_tag_stripped():
    out = sanitize("<p>x</p><script>alert(1)</script>")
    assert "<script>" not in out
    assert "alert" not in out
    assert "<p>x</p>" in out


def test_event_handler_attribute_stripped_on_img():
    out = sanitize('<img src="x" onerror="alert(1)">')
    assert "onerror" not in out
    assert "alert" not in out


def test_formaction_attribute_stripped_on_a():
    out = sanitize('<a formaction="javascript:alert(1)" href="https://example.com">x</a>')
    assert "formaction" not in out
    assert "javascript" not in out
    assert "https://example.com" in out


def test_target_blank_gets_rel_noopener_injected():
    out = sanitize('<a href="https://example.com" target="_blank">link</a>')
    assert 'rel="noopener noreferrer"' in out


def test_javascript_scheme_blocked_on_href():
    out = sanitize('<a href="javascript:alert(1)">x</a>')
    assert "javascript" not in out


def test_object_tag_stripped():
    out = sanitize('<object data="evil.swf"></object>')
    assert "<object" not in out
    assert "evil" not in out


def test_base_tag_stripped():
    out = sanitize('<base href="https://evil"/>')
    assert "<base" not in out
    assert "evil" not in out


def test_iframe_youtube_allowed():
    out = sanitize('<iframe src="https://www.youtube.com/embed/abc"></iframe>')
    assert "<iframe" in out
    assert "youtube.com/embed/abc" in out


def test_iframe_vimeo_allowed():
    out = sanitize('<iframe src="https://player.vimeo.com/video/123"></iframe>')
    assert "<iframe" in out
    assert "vimeo.com/video/123" in out


def test_iframe_loom_allowed():
    out = sanitize('<iframe src="https://www.loom.com/embed/xyz"></iframe>')
    assert "<iframe" in out


def test_iframe_evil_host_stripped_completely():
    out = sanitize('<iframe src="https://evil.com/x"></iframe><p>after</p>')
    assert "<iframe" not in out
    assert "evil.com" not in out
    # The non-iframe content survives.
    assert "<p>after</p>" in out


def test_iframe_no_src_stripped():
    out = sanitize("<iframe>no src</iframe>")
    assert "<iframe" not in out


def test_callout_class_kept_on_div():
    out = sanitize('<div class="callout callout-warn"><strong>Heads up</strong></div>')
    assert 'class="callout callout-warn"' in out


def test_class_kept_on_anchor():
    out = sanitize('<a href="https://example.com" class="news-cta">Click</a>')
    assert 'class="news-cta"' in out


def test_table_classes_kept():
    out = sanitize('<table class="news-grid-2"><tr><td class="cell">x</td></tr></table>')
    assert 'class="news-grid-2"' in out
    assert 'class="cell"' in out


def test_video_embed_wrapper_round_trip():
    src = '<div class="video-embed"><iframe src="https://www.youtube.com/embed/abc"></iframe></div>'
    out = sanitize(src)
    assert 'class="video-embed"' in out
    assert "youtube.com/embed/abc" in out


def test_empty_input_returns_empty_string():
    assert sanitize("") == ""
    assert sanitize(None) == ""


def test_stripped_text_strips_tags():
    assert stripped_text("<p><strong>Hello</strong> there</p>") == "Hello there"


def test_stripped_text_truncates():
    long = "<p>" + ("x" * 200) + "</p>"
    out = stripped_text(long, limit=30)
    assert len(out) <= 30
    assert out.endswith("…")
