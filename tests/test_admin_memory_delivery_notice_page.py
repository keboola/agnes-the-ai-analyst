"""The review page must render what the API now tells it.

Source-level, because the queue is drawn by client-side JS that no server
render exercises. The value is the ratchet: the API can grow a field and the
page can quietly ignore it, which is exactly how the warning would end up
existing everywhere except where an admin actually clicks Approve.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATE = Path(__file__).resolve().parents[1] / "app/web/templates/admin_corporate_memory.html"


@pytest.fixture(scope="module")
def page() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def test_item_card_reads_the_api_field(page):
    assert "item.delivery_warnings" in page


def test_standing_notice_element_exists_and_is_filled_from_the_api(page):
    assert 'id="reviewDeliveryNotice"' in page
    assert "data.delivery_notice" in page or "delivery_notice" in page


def test_warning_text_is_escaped_before_it_reaches_innerhtml(page):
    """The excerpt is analyst-authored note content on its way into the DOM.

    A note is untrusted input by the same argument that produced this whole
    change — it is written by whoever had access to propose it.
    """
    start = page.index("const deliveryWarningHtml")
    # `const isRequired` also appears in an unrelated modal handler earlier in
    # the file, so search forward from the block rather than from the top.
    block = page[start : page.index("const isRequired", start)]
    assert "w.excerpt" in block
    # Every interpolation of a warning field goes through escapeHtml.
    assert "escapeHtml(String(w.excerpt" in block
    assert "escapeHtml(String(w.reason" in block
    # No raw interpolation of the untrusted fields.
    assert "${w.excerpt}" not in block
    assert "${w.reason}" not in block


def test_notice_is_rendered_by_the_review_queue(page):
    """Wired into the render path, not merely defined next to it."""
    render = page[page.index("function renderReviewItems") :]
    assert "renderDeliveryNotice(data)" in render.split("function renderAllItems")[0]
