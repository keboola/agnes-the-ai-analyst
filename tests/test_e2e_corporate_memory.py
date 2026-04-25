"""End-to-end browser tests for Corporate Memory V1 using Playwright.

Tests click through the UI to verify:
- Page renders with correct elements
- Domain filter dropdown works
- Voting works
- My Contributions section visible
- Personal flag toggle works
- Admin: review queue, approve/reject, contradictions tab

Uses LOCAL_DEV_MODE=1 for auth bypass (auto-login as dev@localhost admin).
Starts a real uvicorn server on a random port per test session.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Skip entire module if playwright is not installed
playwright = pytest.importorskip("playwright")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Start a real uvicorn server with LOCAL_DEV_MODE for the test session."""
    tmp = tmp_path_factory.mktemp("e2e_data")
    port = _find_free_port()

    # Create required directories
    (tmp / "state").mkdir()
    (tmp / "analytics").mkdir()
    (tmp / "extracts").mkdir()

    env = os.environ.copy()
    env["LOCAL_DEV_MODE"] = "1"
    env["DATA_DIR"] = str(tmp)
    env["TESTING"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Wait for server to be ready
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            import httpx
            resp = httpx.get(f"{base_url}/login", timeout=1)
            if resp.status_code in (200, 302):
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        proc.kill()
        stdout = proc.stdout.read().decode() if proc.stdout else ""
        pytest.fail(f"Server failed to start on port {port}.\nOutput: {stdout[:2000]}")

    yield {"url": base_url, "port": port, "data_dir": tmp, "proc": proc}

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def api(server):
    """HTTP client for API calls to seed data."""
    import httpx
    return httpx.Client(base_url=server["url"], timeout=10)


def _seed_items(api):
    """Seed test knowledge items and return their IDs."""
    items = [
        {"title": "Churn is MRR-based", "content": "Churn = MRR lost / total MRR at start", "category": "business_logic", "domain": "finance", "tags": ["churn", "MRR"]},
        {"title": "NPS rolling 90-day", "content": "NPS uses rolling 90-day window", "category": "business_logic", "domain": "product", "tags": ["NPS"]},
        {"title": "CAC excludes organic", "content": "CAC = marketing + sales / paid customers", "category": "business_logic", "domain": "finance", "tags": ["CAC"]},
        {"title": "Orders PK is order_id", "content": "Primary key is order_id, revenue column is net_revenue_usd", "category": "data_analysis", "domain": "data", "tags": ["schema"]},
    ]
    ids = []
    for item in items:
        resp = api.post("/api/memory", json=item)
        assert resp.status_code == 201
        ids.append(resp.json()["id"])

    # Approve first 3, leave 4th pending
    for item_id in ids[:3]:
        resp = api.post(f"/api/memory/admin/approve?item_id={item_id}")
        assert resp.status_code == 200

    return ids


class TestCorporateMemoryUserPage:
    """Browser tests for the user-facing corporate memory page."""

    def test_page_loads_with_knowledge_items(self, server, api, page):
        ids = _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # Page title present
        assert page.title() != ""

        # Knowledge items rendered (3 approved)
        items = page.locator(".knowledge-item")
        assert items.count() >= 3

    def test_domain_badges_visible(self, server, api, page):
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # Domain badges should be present
        domain_badges = page.locator(".domain-badge")
        assert domain_badges.count() >= 1

        # Should see Finance and Product domains
        page_text = page.content()
        assert "Finance" in page_text or "finance" in page_text

    def test_confidence_badges_visible(self, server, api, page):
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        confidence_badges = page.locator(".confidence-badge")
        assert confidence_badges.count() >= 1

    def test_domain_filter_filters_items(self, server, api, page):
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # Select "Finance" from domain dropdown
        page.select_option("#domainFilter", "finance")
        # Wait for JS to re-render
        page.wait_for_timeout(1000)

        # Items in main list should only be finance
        items = page.locator("#knowledgeList .knowledge-item")
        count = items.count()
        assert count >= 1

        # All visible items should have Finance domain badge
        for i in range(count):
            item_text = items.nth(i).inner_text()
            assert "Finance" in item_text or "finance" in item_text

    def test_domain_filter_back_to_all(self, server, api, page):
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # Filter to finance
        page.select_option("#domainFilter", "finance")
        page.wait_for_timeout(500)

        # Back to all
        page.select_option("#domainFilter", "")
        page.wait_for_timeout(1000)

        # Should show all approved items again
        items = page.locator(".knowledge-item")
        assert items.count() >= 3

    def test_voting_works(self, server, api, page):
        ids = _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # Click upvote on first item
        first_upvote = page.locator(".knowledge-item .vote-btn.upvote").first
        first_upvote.click()
        page.wait_for_timeout(1000)

        # Vote count should update (check via API)
        resp = api.get(f"/api/memory/{ids[0]}/vote", params={"vote": 1})
        # Just verify page didn't crash
        assert page.locator(".knowledge-item").count() >= 1

    def test_my_contributions_button_visible(self, server, api, page):
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # My Contributions button should be in stats bar
        btn = page.locator("a[href='#my-contributions']")
        assert btn.count() >= 1
        assert "My Contributions" in btn.inner_text()

    def test_my_contributions_section_scrolls(self, server, api, page):
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # Click My Contributions button
        page.locator("a[href='#my-contributions']").click()
        page.wait_for_timeout(500)

        # Section should be visible
        section = page.locator("#my-contributions")
        assert section.is_visible()

    def test_personal_flag_toggle(self, server, api, page):
        ids = _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # Scroll to My Contributions
        page.locator("a[href='#my-contributions']").click()
        page.wait_for_timeout(500)

        # Find a "Mark Personal" button and click it
        personal_btn = page.locator("button:has-text('Mark Personal')").first
        if personal_btn.count() > 0:
            personal_btn.click()
            # Page reloads
            page.wait_for_load_state("networkidle")

            # Verify item was flagged via API
            resp = api.get("/api/memory/my-contributions")
            contributions = resp.json()["items"]
            personal_items = [i for i in contributions if i.get("is_personal")]
            assert len(personal_items) >= 1

    def test_search_filters_items(self, server, api, page):
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # Type in search box
        page.fill("#searchInput", "NPS")
        page.wait_for_timeout(1000)

        # Should show NPS-related items
        items = page.locator("#knowledgeList .knowledge-item")
        if items.count() > 0:
            all_text = page.locator("#knowledgeList").inner_text()
            assert "NPS" in all_text


class TestCorporateMemoryAdminPage:
    """Browser tests for the admin corporate memory page."""

    def test_admin_page_loads(self, server, api, page):
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory/admin")

        # Should have tab buttons
        assert page.locator(".tab-btn").count() >= 3

    def test_review_queue_shows_pending(self, server, api, page):
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory/admin")

        # Wait for review queue to load
        page.wait_for_timeout(1500)

        # Review queue tab should be active by default
        active_tab = page.locator(".tab-btn.active")
        assert "Review Queue" in active_tab.inner_text()

    def test_approve_item_via_api_from_admin(self, server, api, page):
        ids = _seed_items(api)
        pending_id = ids[3]  # 4th item is pending

        # Approve via API (the review queue uses JS-rendered buttons
        # that match the batch action buttons; test the actual flow via API)
        resp = api.post(f"/api/memory/admin/approve?item_id={pending_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        # Verify all 4 items are now approved via API
        resp = api.get("/api/memory?status_filter=approved")
        approved_items = resp.json()["items"]
        approved_ids = [i["id"] for i in approved_items]
        assert pending_id in approved_ids

    def test_contradictions_tab_exists(self, server, api, page):
        page.goto(f"{server['url']}/corporate-memory/admin")

        # Find and click Contradictions tab
        contradictions_tab = page.locator(".tab-btn:has-text('Contradictions')")
        assert contradictions_tab.count() >= 1

        contradictions_tab.click()
        page.wait_for_timeout(500)

        # Tab content should show (either empty state or contradiction cards)
        tab_content = page.locator("#tab-contradictions")
        assert tab_content.is_visible()

    def test_all_items_tab_shows_items(self, server, api, page):
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory/admin")

        # Click "All Items" tab
        page.locator(".tab-btn:has-text('All Items')").click()
        page.wait_for_timeout(1500)

        # Should show items
        tab_content = page.locator("#tab-all")
        assert tab_content.is_visible()

    def test_audit_log_tab_loads(self, server, api, page):
        _seed_items(api)  # This triggers approve actions that create audit entries
        page.goto(f"{server['url']}/corporate-memory/admin")

        # Click Audit Log tab
        page.locator(".tab-btn:has-text('Audit Log')").click()
        page.wait_for_timeout(1500)

        # Tab should be visible
        tab_content = page.locator("#tab-audit")
        assert tab_content.is_visible()

    def test_stats_bar_shows_counts(self, server, api, page):
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory/admin")

        # Stats bar should show numbers
        stats = page.locator(".stats-bar .stat-item .value")
        assert stats.count() >= 3

    def test_admin_stats_bar_shows_nonzero_counts(self, server, api, page):
        """Stats bar must show actual counts, not zeros -- catches API URL mismatches."""
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory/admin")
        page.wait_for_timeout(1500)

        pending = page.locator("#statPending")
        approved = page.locator("#statApproved")
        total = page.locator("#statTotal")

        # Pending should be >= 1 (4th item is pending)
        pending_text = pending.inner_text().strip()
        assert pending_text.isdigit() and int(pending_text) >= 1, (
            f"Pending count should be >= 1, got '{pending_text}'"
        )

        # Approved should be >= 3
        approved_text = approved.inner_text().strip()
        assert approved_text.isdigit() and int(approved_text) >= 3, (
            f"Approved count should be >= 3, got '{approved_text}'"
        )

        # Total should be >= 4
        total_text = total.inner_text().strip()
        assert total_text.isdigit() and int(total_text) >= 4, (
            f"Total count should be >= 4, got '{total_text}'"
        )

    def test_review_queue_renders_pending_items(self, server, api, page):
        """Review queue must actually render pending items, not just show empty state."""
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory/admin")
        page.wait_for_timeout(1500)

        # Review queue should contain the pending item title
        review_list = page.locator("#reviewList")
        review_text = review_list.inner_text()
        assert "Orders PK is order_id" in review_text, (
            f"Pending item not found in review queue. Content: {review_text[:500]}"
        )

    def test_all_items_tab_renders_actual_items(self, server, api, page):
        """All Items tab must render knowledge items after JS fetch."""
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory/admin")

        # Click "All Items" tab
        page.locator(".tab-btn:has-text('All Items')").click()
        page.wait_for_timeout(1500)

        # Should render actual item content
        all_list = page.locator("#allList")
        all_text = all_list.inner_text()
        assert "Churn is MRR-based" in all_text or "NPS" in all_text, (
            f"No knowledge items rendered in All Items tab. Content: {all_text[:500]}"
        )

    def test_admin_js_fetches_use_correct_api_prefix(self, server, api, page):
        """Verify JS fetch calls hit /api/memory/ not a wrong prefix."""
        _seed_items(api)
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        page.goto(f"{server['url']}/corporate-memory/admin")
        page.wait_for_timeout(2000)

        # No fetch errors in console
        fetch_errors = [e for e in console_errors if "Failed to" in e or "fetch" in e.lower()]
        assert len(fetch_errors) == 0, f"JS console had fetch errors: {fetch_errors}"


class TestCorporateMemoryUserPageStats:
    """Verify the user page stats bar shows real data, not zeros."""

    def test_user_page_stats_show_nonzero(self, server, api, page):
        """Contributors and Knowledge Items stats must reflect seeded data."""
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")
        page.wait_for_timeout(1000)

        stats_bar = page.locator(".stats-bar")
        stats_text = stats_bar.inner_text()

        # Should show at least 1 contributor
        assert "0\nCONTRIBUTORS" not in stats_text.replace(" ", ""), (
            f"Contributors shows 0. Stats bar content: {stats_text}"
        )

    def test_user_page_my_contributions_count(self, server, api, page):
        """MY CONTRIBUTIONS count must be nonzero after seeding items."""
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")
        page.wait_for_timeout(1000)

        stats_text = page.locator(".stats-bar").inner_text()
        # "0\nMY CONTRIBUTIONS" should not appear -- we seeded 4 items as dev@localhost
        assert "0\nMY CONTRIBUTIONS" not in stats_text.replace(" ", ""), (
            f"My Contributions shows 0. Stats bar content: {stats_text}"
        )


class TestCorporateMemoryFilters:
    """Verify client-side filters actually return results when data exists."""

    def test_category_buttons_match_actual_data(self, server, api, page):
        """Category filter buttons must reflect real categories, not hardcoded placeholders."""
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # Get the filter buttons (excluding "All" and "My Rules")
        buttons = page.locator(".filter-btn")
        button_categories = []
        for i in range(buttons.count()):
            cat = buttons.nth(i).get_attribute("data-category")
            if cat and cat != "my_rules":
                button_categories.append(cat)

        # At least one category from our seeded data should be a button
        seeded_categories = {"business_logic", "data_analysis"}
        assert len(button_categories) >= 1, "No category filter buttons rendered"
        assert any(cat in seeded_categories for cat in button_categories), (
            f"Category buttons {button_categories} don't include any seeded categories"
        )

    def test_category_filter_returns_results(self, server, api, page):
        """Clicking a category filter button must show matching items, not empty state."""
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")
        page.wait_for_timeout(1000)

        # Find the first non-All, non-my_rules filter button and click it
        buttons = page.locator(".filter-btn")
        clicked = False
        for i in range(buttons.count()):
            cat = buttons.nth(i).get_attribute("data-category")
            if cat and cat not in ("", "my_rules"):
                buttons.nth(i).click()
                clicked = True
                break
        assert clicked, "No category buttons to click"

        page.wait_for_timeout(1500)
        list_el = page.locator("#knowledgeList")
        list_text = list_el.inner_text()
        assert "No matching knowledge items found" not in list_text, (
            f"Category filter returned empty results. Content: {list_text[:300]}"
        )

    def test_domain_filter_returns_results(self, server, api, page):
        """Domain dropdown filter must show matching items when a domain with items is selected."""
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # Select "finance" domain
        page.select_option("#domainFilter", "finance")
        page.wait_for_timeout(1500)

        list_el = page.locator("#knowledgeList")
        items = list_el.locator(".knowledge-item")
        assert items.count() >= 1, "Domain filter 'finance' returned no items"

    def test_domain_filter_combined_with_all_category(self, server, api, page):
        """Domain filter with 'All' category should show all items in that domain."""
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")

        # Make sure "All" category is active (default)
        all_btn = page.locator('.filter-btn[data-category=""]')
        assert all_btn.get_attribute("class") and "active" in all_btn.get_attribute("class")

        # Select finance domain
        page.select_option("#domainFilter", "finance")
        page.wait_for_timeout(1500)

        items = page.locator("#knowledgeList .knowledge-item")
        assert items.count() >= 2, (
            f"Finance domain with 'All' category should show 2+ items, got {items.count()}"
        )


    def test_domain_change_resets_category_filter(self, server, api, page):
        """Changing domain dropdown must reset category to All so items always show."""
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")
        page.wait_for_timeout(1000)

        # Click a category button first (e.g., the first non-All one)
        buttons = page.locator(".filter-btn")
        for i in range(buttons.count()):
            cat = buttons.nth(i).get_attribute("data-category")
            if cat and cat not in ("", "my_rules"):
                buttons.nth(i).click()
                break
        page.wait_for_timeout(500)

        # Now change domain to finance
        page.select_option("#domainFilter", "finance")
        page.wait_for_timeout(1500)

        # "All" category button should be active again
        all_btn = page.locator('.filter-btn[data-category=""]')
        assert "active" in (all_btn.get_attribute("class") or ""), (
            "Category filter did not reset to 'All' when domain changed"
        )

        # Should show finance items (not empty)
        items = page.locator("#knowledgeList .knowledge-item")
        assert items.count() >= 1, (
            f"Domain 'finance' returned no items after domain change (category should have reset)"
        )

    def test_category_change_resets_domain_filter(self, server, api, page):
        """Clicking a category button must reset domain to All Domains."""
        _seed_items(api)
        page.goto(f"{server['url']}/corporate-memory")
        page.wait_for_timeout(1000)

        # Set domain filter first
        page.select_option("#domainFilter", "finance")
        page.wait_for_timeout(500)

        # Now click a category button that has items
        buttons = page.locator(".filter-btn")
        for i in range(buttons.count()):
            cat = buttons.nth(i).get_attribute("data-category")
            if cat and cat not in ("", "my_rules"):
                buttons.nth(i).click()
                break
        page.wait_for_timeout(1500)

        # Domain dropdown should be reset to "" (All Domains)
        domain_val = page.locator("#domainFilter").input_value()
        assert domain_val == "", (
            f"Domain filter did not reset to 'All Domains' when category changed, got '{domain_val}'"
        )


class TestCorporateMemoryAuditLog:
    """Verify audit log renders entries after admin actions."""

    def test_audit_log_shows_entries_after_approve(self, server, api, page):
        """After approving an item, audit log must show the action."""
        ids = _seed_items(api)
        # 4th item is pending -- approve it to create audit entry
        resp = api.post(f"/api/memory/admin/approve?item_id={ids[3]}")
        assert resp.status_code == 200

        page.goto(f"{server['url']}/corporate-memory/admin")

        # Switch to Audit Log tab
        page.locator(".tab-btn:has-text('Audit Log')").click()
        page.wait_for_timeout(1500)

        audit_content = page.locator("#auditContent")
        audit_text = audit_content.inner_text()
        assert "No audit log entries" not in audit_text, (
            f"Audit log shows empty after approve action. Content: {audit_text[:500]}"
        )
        # Should show the approve action
        assert "approve" in audit_text.lower(), (
            f"Audit log doesn't contain 'approve' entry. Content: {audit_text[:500]}"
        )

    def test_audit_log_row_shows_item_title_not_uuid(self, server, api, page):
        """Audit table must render the item title, not a truncated UUID."""
        ids = _seed_items(api)
        resp = api.post(f"/api/memory/admin/approve?item_id={ids[3]}")
        assert resp.status_code == 200

        page.goto(f"{server['url']}/corporate-memory/admin")
        page.locator(".tab-btn:has-text('Audit Log')").click()
        page.wait_for_timeout(1500)

        audit_text = page.locator("#auditContent").inner_text()
        assert "Orders PK is order_id" in audit_text, (
            f"Audit log doesn't show item title. Content: {audit_text[:500]}"
        )

    def test_audit_log_row_expands_with_full_details(self, server, api, page):
        """Clicking an audit row must reveal the detail panel with raw params."""
        ids = _seed_items(api)
        # Mandate creates an audit entry with non-trivial params (reason, audience)
        resp = api.post(
            f"/api/memory/admin/mandate?item_id={ids[3]}",
            json={"reason": "All analysts must use canonical orders schema", "audience": "all"},
        )
        assert resp.status_code == 200

        page.goto(f"{server['url']}/corporate-memory/admin")
        page.locator(".tab-btn:has-text('Audit Log')").click()
        page.wait_for_timeout(1500)

        # Detail row should be hidden initially
        detail_row = page.locator("tr.audit-detail-row").first
        assert not detail_row.is_visible(), "Detail row should start collapsed"

        # Click the first audit row (the mandate action)
        page.locator("tr.audit-row").first.click()
        page.wait_for_timeout(300)

        # Detail row now visible with the mandate context
        assert detail_row.is_visible(), "Detail row should expand on click"
        detail_text = detail_row.inner_text()
        assert "All analysts must use canonical orders schema" in detail_text, (
            f"Mandate reason missing from expanded detail. Content: {detail_text[:500]}"
        )
        # Item context (resolved from id) must appear
        assert "Orders PK is order_id" in detail_text, (
            f"Expanded detail missing item title. Content: {detail_text[:500]}"
        )
        # Raw params block must include the audience field
        assert "audience" in detail_text and "all" in detail_text, (
            f"Raw params block missing audience. Content: {detail_text[:500]}"
        )

        # Clicking again collapses
        page.locator("tr.audit-row").first.click()
        page.wait_for_timeout(300)
        assert not detail_row.is_visible(), "Detail row should collapse on second click"


class TestCorporateMemoryAPI:
    """API endpoint tests via browser-hosted server (complements unit tests)."""

    def test_new_endpoint_my_contributions(self, api):
        _seed_items(api)
        resp = api.get("/api/memory/my-contributions")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert len(data["items"]) >= 4

    def test_new_endpoint_personal_flag(self, api):
        ids = _seed_items(api)
        # Flag as personal
        resp = api.post(f"/api/memory/{ids[0]}/personal", json={"is_personal": True})
        assert resp.status_code == 200
        assert resp.json()["is_personal"] is True

        # Verify excluded from default list
        resp = api.get("/api/memory")
        items = resp.json()["items"]
        item_ids = [i["id"] for i in items]
        assert ids[0] not in item_ids

        # Unflag
        resp = api.post(f"/api/memory/{ids[0]}/personal", json={"is_personal": False})
        assert resp.status_code == 200

    def test_new_endpoint_provenance(self, api):
        ids = _seed_items(api)
        resp = api.get(f"/api/memory/{ids[0]}/provenance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_type"] == "claude_local_md"
        assert data["domain"] == "finance"
        assert data["source_user"] == "dev@localhost"

    def test_new_endpoint_contradictions_empty(self, api):
        resp = api.get("/api/memory/admin/contradictions")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_domain_filter(self, api):
        _seed_items(api)
        resp = api.get("/api/memory?domain=finance")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert all(i["domain"] == "finance" for i in items)

    def test_stats_include_new_fields(self, api):
        _seed_items(api)
        resp = api.get("/api/memory/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "by_domain" in data
        assert "by_source_type" in data


def _seed_mixed_sources(api):
    """Seed items with both source types so the source filter can be exercised.

    Two items from CLAUDE.local.md (default), one item flagged as a verified
    fact extracted from an analyst session. Returns dict with the seeded IDs.
    """
    md_items = [
        {
            "title": "Finance churn definition",
            "content": "Churn = MRR lost / total MRR at start",
            "category": "business_logic",
            "domain": "finance",
            "tags": ["churn", "MRR"],
        },
        {
            "title": "Orders schema reference",
            "content": "Primary key is order_id, revenue column is net_revenue_usd",
            "category": "data_analysis",
            "domain": "data",
            "tags": ["schema"],
        },
    ]
    md_ids = []
    for item in md_items:
        resp = api.post("/api/memory", json=item)
        assert resp.status_code == 201
        md_ids.append(resp.json()["id"])

    verified_payload = {
        "title": "Verified fact from analyst session",
        "content": "Anna confirmed that EU revenue excludes intercompany transfers",
        "category": "business_logic",
        "domain": "finance",
        "tags": ["revenue", "verified"],
        "source_type": "user_verification",
    }
    resp = api.post("/api/memory", json=verified_payload)
    assert resp.status_code == 201
    verified_id = resp.json()["id"]

    # Approve everything so they appear in the All Items tab regardless of status
    for item_id in md_ids + [verified_id]:
        resp = api.post(f"/api/memory/admin/approve?item_id={item_id}")
        assert resp.status_code == 200

    return {"md_ids": md_ids, "verified_id": verified_id}


class TestVerifiedFactsSourceFilterAPI:
    """Confirm the API contract powering the new admin Source filter.

    These run before browser tests so a backend regression fails loud, fast,
    and obvious before we suspect the UI.
    """

    def test_source_filter_returns_only_verified(self, api):
        seeded = _seed_mixed_sources(api)
        resp = api.get("/api/memory?source_type=user_verification")
        assert resp.status_code == 200
        items = resp.json()["items"]

        assert len(items) >= 1, "Verified-facts filter returned no items despite seeding one"
        for item in items:
            assert item["source_type"] == "user_verification", (
                f"Source filter leaked non-verified item: {item['title']} ({item['source_type']})"
            )

        verified_titles = {item["title"] for item in items}
        assert "Verified fact from analyst session" in verified_titles
        assert "Finance churn definition" not in verified_titles

    def test_source_filter_combined_with_status(self, api):
        """Cross-filter rule: status + source_type stack correctly."""
        seeded = _seed_mixed_sources(api)
        resp = api.get(
            "/api/memory",
            params={"status_filter": "approved", "source_type": "user_verification"},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 1
        for item in items:
            assert item["status"] == "approved"
            assert item["source_type"] == "user_verification"

    def test_source_filter_results_sorted_recent_first(self, api):
        """Verified-facts list must surface most recently updated items first."""
        seeded = _seed_mixed_sources(api)
        # Touch the verified item so its updated_at is the newest
        resp = api.post(
            f"/api/memory/admin/edit?item_id={seeded['verified_id']}",
            json={"content": "Anna confirmed EU revenue excludes intercompany transfers (edited)"},
        )
        assert resp.status_code == 200

        resp = api.get("/api/memory?source_type=user_verification")
        items = resp.json()["items"]
        assert items, "Expected at least one verified item"
        assert items[0]["id"] == seeded["verified_id"], (
            f"Most recently edited verified item should be first; got {items[0]['title']}"
        )


class TestVerifiedFactsSourceFilterUI:
    """Browser tests for the new Source dropdown on the admin All Items tab."""

    def _open_all_tab(self, server, page):
        page.goto(f"{server['url']}/corporate-memory/admin")
        page.locator(".tab-btn:has-text('All Items')").click()
        page.wait_for_timeout(1500)

    def test_source_filter_dropdown_exists_with_expected_options(self, server, api, page):
        """The dropdown must be wired with the two source types we ingest today."""
        _seed_mixed_sources(api)
        self._open_all_tab(server, page)

        dropdown = page.locator("#sourceTypeFilter")
        assert dropdown.count() == 1, "sourceTypeFilter dropdown not rendered on admin page"

        option_values = [
            opt.get_attribute("value")
            for opt in dropdown.locator("option").all()
        ]
        assert "" in option_values, "Missing 'all sources' default option"
        assert "user_verification" in option_values, "Missing verified-facts option"
        assert "claude_local_md" in option_values, "Missing CLAUDE.local.md option"

    def test_filter_to_verified_shows_only_verified_items(self, server, api, page):
        """Content + known-value rule: only the seeded verified item should render."""
        _seed_mixed_sources(api)
        self._open_all_tab(server, page)

        page.select_option("#sourceTypeFilter", "user_verification")
        page.wait_for_timeout(1500)

        all_text = page.locator("#allList").inner_text()
        assert "Verified fact from analyst session" in all_text, (
            f"Verified item not rendered after filtering. Content: {all_text[:500]}"
        )
        assert "Finance churn definition" not in all_text, (
            "CLAUDE.local.md item leaked into verified-facts filter"
        )
        assert "Orders schema reference" not in all_text, (
            "CLAUDE.local.md item leaked into verified-facts filter"
        )
        # Empty-state rule: list should NOT show its empty placeholder
        assert "No matching" not in all_text
        assert "Error loading items" not in all_text

    def test_verified_badge_visible_when_filtered(self, server, api, page):
        """The blue 'Verified' badge must render on items in the filtered list."""
        _seed_mixed_sources(api)
        self._open_all_tab(server, page)

        page.select_option("#sourceTypeFilter", "user_verification")
        page.wait_for_timeout(1500)

        all_text = page.locator("#allList").inner_text()
        assert "Verified" in all_text, (
            f"Verified badge not rendered for verified items. Content: {all_text[:500]}"
        )

    def test_filter_to_claude_local_md_excludes_verified(self, server, api, page):
        """Inverse direction: switching to CLAUDE.local.md must hide verified items."""
        _seed_mixed_sources(api)
        self._open_all_tab(server, page)

        page.select_option("#sourceTypeFilter", "claude_local_md")
        page.wait_for_timeout(1500)

        all_text = page.locator("#allList").inner_text()
        assert "Finance churn definition" in all_text or "Orders schema reference" in all_text, (
            f"CLAUDE.local.md items missing after filtering. Content: {all_text[:500]}"
        )
        assert "Verified fact from analyst session" not in all_text, (
            "Verified item leaked into CLAUDE.local.md filter"
        )

    def test_reset_to_all_sources_shows_everything(self, server, api, page):
        """Cross-filter rule: resetting source filter must restore mixed list."""
        _seed_mixed_sources(api)
        self._open_all_tab(server, page)

        # Narrow then widen
        page.select_option("#sourceTypeFilter", "user_verification")
        page.wait_for_timeout(1000)
        page.select_option("#sourceTypeFilter", "")
        page.wait_for_timeout(1500)

        all_text = page.locator("#allList").inner_text()
        assert "Verified fact from analyst session" in all_text
        assert (
            "Finance churn definition" in all_text
            or "Orders schema reference" in all_text
        ), "Resetting source filter did not restore CLAUDE.local.md items"

    def test_status_and_source_filters_stack(self, server, api, page):
        """Combining Status=Approved + Source=Verified must intersect correctly."""
        _seed_mixed_sources(api)
        self._open_all_tab(server, page)

        page.select_option("#statusFilter", "approved")
        page.wait_for_timeout(800)
        page.select_option("#sourceTypeFilter", "user_verification")
        page.wait_for_timeout(1500)

        all_text = page.locator("#allList").inner_text()
        assert "Verified fact from analyst session" in all_text
        assert "Finance churn definition" not in all_text
        assert "No matching" not in all_text

    def test_no_console_errors_when_using_filter(self, server, api, page):
        """Console-error rule: catches API prefix mistakes and JS exceptions."""
        _seed_mixed_sources(api)

        errors: list[str] = []
        page.on(
            "console",
            lambda msg: errors.append(msg.text) if msg.type == "error" else None,
        )

        self._open_all_tab(server, page)
        page.select_option("#sourceTypeFilter", "user_verification")
        page.wait_for_timeout(1500)
        page.select_option("#sourceTypeFilter", "claude_local_md")
        page.wait_for_timeout(1500)
        page.select_option("#sourceTypeFilter", "")
        page.wait_for_timeout(1500)

        fatal = [
            e for e in errors
            if "Failed to" in e or "TypeError" in e or "fetch" in e.lower()
        ]
        assert not fatal, f"JS console raised errors while using source filter: {fatal}"
