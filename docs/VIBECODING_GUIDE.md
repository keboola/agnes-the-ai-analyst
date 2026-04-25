# Vibecoding Guide: Building Systems Together with AI

Hard-won lessons from building the Agnes corporate memory feature.
This guide is for humans and AI working together to build production systems.

---

## The Problem We Keep Hitting

AI writes tests that pass against broken UIs. Tests check "does the HTML element exist?"
instead of "does it show real data?" The result: a green test suite and a completely
broken product.

This guide exists to prevent that pattern.

---

## Phase 1: Before Writing Any Code

### Seed data first, always

Before touching any UI or writing tests, create a seed script that populates realistic data.
You cannot test what you cannot see.

```bash
# Good: seed script with realistic data across all domains
python scripts/seed_corporate_memory.py --base-url http://localhost:8765

# Bad: writing tests with inline 2-line items then checking "count >= 1"
```

**Rule:** If you can't visually verify the feature in a browser with realistic data,
you're not ready to write tests.

### Open the page before writing tests

Start the dev server, seed data, open the page in a browser. Click every button.
Try every filter combination. Note what breaks. THEN write tests for what you observed.

**Rule:** Never write E2E tests without first manually using the feature.

---

## Phase 2: Writing Tests That Actually Catch Bugs

### The Content Rule

Every test that checks rendered data must assert on **content**, not **structure**.

```python
# BAD - passes even when stats show 0/0/0
def test_stats_bar(page):
    stats = page.locator(".stats-bar .value")
    assert stats.count() >= 3  # Just checks DOM elements exist

# GOOD - fails when stats show zeros
def test_stats_bar_shows_real_data(page):
    pending = page.locator("#statPending").inner_text().strip()
    assert pending.isdigit() and int(pending) >= 1, f"Pending is {pending}"
```

### The Known-Value Rule

Seed a specific item, then assert its title appears in the rendered list.

```python
# BAD - just checks "some items rendered"
items = page.locator(".knowledge-item")
assert items.count() >= 1

# GOOD - checks a specific seeded item is visible
list_text = page.locator("#reviewList").inner_text()
assert "Churn is MRR-based" in list_text, f"Seeded item missing: {list_text[:300]}"
```

### The Empty-State Rule

Always check that empty-state messages do NOT appear when data exists.

```python
# GOOD
assert "No matching knowledge items found" not in list_text
assert "No audit log entries" not in audit_text
assert "Error loading" not in review_text
```

### The Console-Error Rule

Add a test that catches JS runtime errors. This single test would have caught
5 of our 10 bugs (GROUPS.map crash, tags.map crash, wrong API prefix 404s).

```python
def test_no_js_errors(page):
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(url)
    page.wait_for_timeout(2000)
    fetch_errors = [e for e in errors if "Failed to" in e or "TypeError" in e]
    assert len(fetch_errors) == 0, f"JS errors: {fetch_errors}"
```

### The Two-Path Rule

Server-side rendering (Jinja) and client-side rendering (JS fetch) are **two separate
code paths** that can diverge. Test both.

- Initial page load tests Jinja rendering
- Clicking a filter/tab tests JS fetch + render
- Both must show the same data

### The Cross-Filter Rule

Never test filters in isolation only. Test combinations and resets.

```python
# Test that changing domain resets category (prevents empty cross-filter results)
page.select_option("#domainFilter", "finance")
assert "active" in page.locator('.filter-btn[data-category=""]').get_attribute("class")
assert page.locator("#knowledgeList .knowledge-item").count() >= 1
```

---

## Phase 3: The Bug Pattern Catalog

These are the specific bug patterns we hit. Check for each one during code review.

### 1. API Prefix Mismatch
**Symptom:** JS fetch returns 404, empty-state messages appear, no console errors (caught silently)
**Cause:** Template JS hardcodes one URL prefix, API router uses another
**Test:** Console-error-catcher test + content assertion on JS-rendered sections
**Prevention:** Define the API prefix as a JS constant from the server: `const API = '{{ api_prefix }}';`

### 2. Field Name Divergence
**Symptom:** Data exists in API response but UI shows nothing/zeros
**Cause:** JS reads `entry.admin`, API returns `user_id`. JS reads `item.tags.map()`, API returns a JSON string.
**Test:** Assert specific known values appear in rendered HTML
**Prevention:** Type the API response shape in the JS, or parse defensively:
```javascript
// Always parse JSON fields that might be strings
function parseJsonField(val) {
    if (Array.isArray(val)) return val;
    if (typeof val === 'string') { try { return JSON.parse(val); } catch { return []; } }
    return [];
}
```

### 3. Pagination Off-By-One
**Symptom:** 500 errors on page load, works after clicking "next page"
**Cause:** JS initializes `page = 0`, API expects `page >= 1`, offset becomes negative
**Test:** Check that initial page load doesn't produce errors
**Prevention:** API should always clamp: `page = max(page, 1)`

### 4. Hardcoded UI vs Dynamic Data
**Symptom:** Filter buttons exist but clicking any of them returns empty
**Cause:** Button labels hardcoded ("Performance", "API") but actual data has different categories
**Test:** Assert that clicking a filter button returns results (not empty state)
**Prevention:** Generate UI elements from actual data: `{% for cat in categories %}`

### 5. Jinja Type Mismatch
**Symptom:** 500 Internal Server Error on page load
**Cause:** Template does `c.detected_at[:10]` on a datetime object (not a string)
**Test:** Simply loading the page with seeded data catches this
**Prevention:** Always use `|string` filter before slicing: `{{ (c.detected_at|string)[:10] }}`

### 6. Silent Cross-Filter Empty Results
**Symptom:** User selects a domain, sees nothing, thinks it's broken
**Cause:** A category filter was still active from a previous click
**Test:** Test filter combinations and resets
**Prevention:** Reset other filters when one changes. Show active filter state clearly.

---

## Phase 4: The Workflow

### When adding a new UI feature:

1. **Seed** realistic data covering all states (pending/approved/rejected, all domains, multiple users)
2. **Open** the page in a browser and click everything
3. **Write tests** that assert on content, not structure
4. **Include** a console-error-catcher test
5. **Test** all JS-driven paths (filters, tabs, pagination, actions)
6. **Test** filter resets and combinations
7. **Verify** the seed script can be re-run (idempotent or fresh DB)

### When debugging a "works in tests, broken in browser" situation:

1. Check the browser console for JS errors
2. Check the Network tab for 404/500 responses
3. Compare the API response shape with what the JS expects
4. Check if Jinja template uses different field names than JS fetch path
5. Check if any filter state is stale/sticky

### The 5-second smoke test before declaring victory:

1. Page loads without 500? (catches Jinja type errors)
2. Stats show nonzero numbers? (catches field name mismatches)
3. Lists show actual items? (catches API prefix/fetch errors)
4. Clicking a filter shows results? (catches hardcoded categories)
5. Browser console is clean? (catches JS runtime errors)

---

## Summary: The Three Laws of Vibecoding Tests

1. **Test content, not structure.** `int(el.text) >= 1` not `el.count() >= 1`
2. **Test the JS path, not just the Jinja path.** Click the tab, check what renders.
3. **Test combinations, not just singles.** Filter A + Filter B, then reset.

If every test follows these three rules, the "green suite, broken UI" problem disappears.
