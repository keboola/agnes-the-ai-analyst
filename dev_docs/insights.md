# Activity Center

Enterprise data intelligence demo page for the webapp dashboard.

## Purpose

Convince C-level executives (CEO/COO of a bank) that the platform gives them
complete visibility into how data powers business processes across the entire
organization.

This is NOT a usage log. It is a **strategic command center** that:
- Maps data queries to business processes they support
- Shows organizational maturity in data-driven decision making
- Identifies which processes benefit from data and which don't yet
- Demonstrates adoption velocity and team progression
- Reveals unmet data needs as strategic opportunities

## Status

- **Current state**: Demo mockup with fictional data (DEMO badge in header)
- **URL**: https://your-instance.example.com/activity-center (requires login)
- **PR**: https://github.com/keboola/internal_ai_data_analyst/pull/122
- **Branch**: `feature/activity-center`
- **Dashboard link**: Not added yet (UX placement decided, implementation pending)

## Files

| File | Purpose |
|------|---------|
| `webapp/activity_data.py` | Mock data module - `get_activity_data()` returns complete dict |
| `webapp/templates/activity_center.html` | Standalone HTML template (pattern from `corporate_memory.html`) |
| `webapp/app.py` | Route `/activity-center` (after corporate-memory routes) |

## Data Architecture

### Executive Summary
Top-level KPIs displayed in the stats bar:

| Metric | Value | Purpose |
|--------|-------|---------|
| Total analysts | 97 | Org-wide adoption scale |
| Active this week | 68 | Weekly engagement |
| Active today | 34 | Real-time pulse |
| Teams active | 19 / 23 | Coverage breadth |
| Business processes | 47 | Value mapping |
| Decisions supported/wk | 142 | Business impact |
| Success rate | 87% | Quality of insights |
| Adoption trend | +12% | Month-over-month growth |

### Business Processes (15 total, 6 categories)

The key differentiator: instead of logging queries, we map them to recurring
organizational needs that data helps solve.

| Category | Process | Status | Queries/wk |
|----------|---------|--------|------------|
| Finance & Revenue | Revenue & ARR Monitoring | optimized | 34 |
| Finance & Revenue | Budget vs Actuals Analysis | optimized | 22 |
| Customer Success | Churn Risk Detection | mature | 28 |
| Customer Success | Contract Renewal Forecasting | mature | 19 |
| Customer Success | Customer Onboarding Analytics | developing | 8 |
| Operations & Infrastructure | Infrastructure Cost Optimization | mature | 25 |
| Operations & Infrastructure | Support Quality & SLA Tracking | mature | 31 |
| Operations & Infrastructure | Platform Capacity Planning | developing | 12 |
| Growth & Market | Pipeline & Deal Velocity | developing | 15 |
| Growth & Market | Cross-sell/Upsell Identification | developing | 9 |
| Growth & Market | Competitive Win/Loss Analysis | early | 4 |
| Growth & Market | Marketing Attribution | early | 3 |
| Product & Engineering | Product Usage Telemetry | mature | 22 |
| Product & Engineering | Engineering Velocity & Quality | early | 5 |
| People & Culture | Headcount & Workforce Planning | developing | 10 |

Each process has: description, sample_queries (2), data_sources, teams_involved, impact.

### Team Maturity Model (23 teams)

| Maturity | Count | Teams | Score Range |
|----------|-------|-------|-------------|
| Optimized | 3 | Finance (92), Leadership (88), Internal AI & Data Squad (85) | 85-95 |
| Mature | 6 | Customer Success (78), CSM (75), Engineering SRE (71), Engineering (70), Professional Services (69), Customer Enthusiasts (68) | 68-80 |
| Developing | 8 | Agile AI (60), Agile AJDA (57), Agile DMD (55), Agile PAT (52), Sales (50), Marketing (48), People (46), Engineering Support (45) | 45-60 |
| Early | 6 | Engineering UI (38), UX (35), R&D (30), Sales Engineering (28), Product Marketing (25), General & Admin (20) | 20-38 |

Each team has 2-8 members with Czech names, roles, activity status, and recent queries.

### Data Opportunities (10 items, enriched)

Unmet data needs framed as strategic growth opportunities. Each card expands to show:

- **Data Integration Map** - Mermaid flowchart diagram showing join paths (existing=blue, new=orange)
- **Integration Path** - Technical description of how to connect the data (source systems, APIs)
- **Join Keys** - Table showing exact column-level connections (new_table.column -> existing_table)
- **Team Impact** - Specific beneficiaries with concrete use case descriptions
- **Enabled Queries** - Example queries that would become possible with the new data

| Priority | Title | Key Join | Primary Beneficiaries |
|----------|-------|----------|----------------------|
| HIGH | NPS & Customer Satisfaction Data | company_id -> company | CS, CSM, Leadership |
| HIGH | Customer Health Score | company_id (derived from existing data) | CSM, CS, Sales |
| HIGH | Marketing Campaign Data | company_id, opportunity_id | Marketing, Sales, Product Marketing |
| MEDIUM | Slack/Teams Engagement Analytics | company_id (channel mapping) | CS, Customer Enthusiasts |
| MEDIUM | Product Roadmap Data | product_id, opportunity_id | Sales, Sales Engineering |
| MEDIUM | Competitor Intelligence Feed | opportunity_id | Sales, Product Marketing |
| MEDIUM | Git/Dev Productivity Metrics | employee_id, jira_issue_key | Engineering, Agile AI |
| LOW | Partner & Channel Revenue | company_id (partner_id FK exists!) | Sales, Finance |
| LOW | Employee Satisfaction Surveys | employee_id | People, Leadership |
| LOW | Security & Compliance Logs | kbc_project_id, kbc_organization_id | Engineering SRE |

Join keys are designed against the real data model (25 tables, 5 domains from `data_description.md`).

### Activity Feed

20 recent items with person_name, team, query_text, timestamp, status, process_name.
Timestamps span 10:15-11:25 covering 12 different teams.

## Page Layout

Tabbed layout with always-visible stats bar. URL hash support for bookmarking (`#processes`, `#teams`, etc).

```
ALWAYS VISIBLE:
  Header (back link, title, DEMO badge, user avatar)
  Executive Pulse stats bar (5 KPIs)
  Summary sentence

TAB BAR:
  Processes (15) | Teams (23) | Activity (20) | Opportunities (10)

TAB: PROCESSES
  Business Process Intelligence map, grouped by category with expand/collapse

TAB: TEAMS
  Team Maturity leaderboard (2-column grid, full width)
  Team Details accordion (click leaderboard row -> scroll + expand)

TAB: ACTIVITY
  Live Activity feed (2-column grid, full width, with time filters)

TAB: OPPORTUNITIES
  Unmet Data Needs cards (expandable with integration details)
```

### Visual Design

- **Maturity badges**: green (optimized), blue (mature), yellow (developing), gray (early)
- **Process dots**: filled green/blue for mature+, filled yellow for developing, empty gray for early
- **Team bars**: horizontal 0-100, colored by maturity level
- **Feed avatars**: colored circles with initials, 8 rotating colors
- **DEMO badge**: orange pill in header (`rgba(245, 159, 10, 0.15)` background)
- **Tab bar**: blue underline for active tab, count badges per tab
- **Mermaid diagrams**: existing tables blue (`#dbeafe`), new data sources orange (`#fed7aa`)

### JavaScript Interactions

- `switchTab(tabId, btn)` - switch active tab, updates URL hash via `history.replaceState()`
- `toggleProcessDetail(processId)` - expand process to show sample queries, data sources, impact
- `toggleTeam(teamId)` - expand/collapse team accordion
- `scrollToTeam(teamId)` - leaderboard click scrolls to team detail + expands + highlight effect
- `filterActivity(period, btn)` - filter feed: "All"/"Today" show all, "This Hour" shows first 5
- `toggleOpportunity(oppId)` - expand opportunity card to show ERD diagram, join keys, team impact
- Tab restored from URL hash on page load (supports direct linking to any tab)

## UX: Dashboard Link Placement

### Recommendation: Widget in right column, below Corporate Memory

A dedicated card matching the Corporate Memory widget pattern:
- Green left border (matching Activity Center icon color)
- Icon + "Activity Center" title + DEMO badge inline
- Brief description line
- "View Activity Center >" button in green

```
RIGHT COLUMN (current):
  Corporate Memory widget    <- existing
  Activity Center widget     <- NEW (below Corporate Memory, above Account)
  Account card               <- existing
```

### Rationale

1. **Right column = intelligence column.** Corporate Memory (shared knowledge) and
   Activity Center (team analytics) are conceptual siblings.
2. **High visibility for executives** visiting for a demo - they see it in the natural
   scan pattern (left column first for data, then right for supplementary intelligence).
3. **Follows established pattern.** Corporate Memory set the precedent with colored border,
   icon, stats, and CTA button. Same treatment = consistent design language.
4. **DEMO badge solves dual-audience.** Analysts see the badge and deprioritize it.
   Executives see it as a capability preview.

### Rejected Alternatives

- **Text link in YOUR DATA footer**: too low visibility for C-level demo audience
- **Header/stats bar**: breaks clean header pattern, wrong semantic context for navigation

## Design Principles

1. **30-second scan**: Executive Pulse answers "is our data investment working?" at a glance
2. **Process-first, not query-first**: Business Process Map shows WHAT the org does with data
3. **Maturity narrative**: distribution bar tells a momentum story
4. **Drill-down flow**: Overview -> Process -> Team -> Person -> Query
5. **Unmet needs = opportunity**: missing data framed as growth potential, not gaps
6. **Traffic light pattern**: green/blue/yellow/gray badges for instant status comprehension

## Technical Notes

- Template is standalone (all CSS inline in `<style>`, all JS inline in `<script>`)
- Reuses CSS variables from `style-keboola.css`
- Route requires `@login_required` (redirects to Google SSO)
- Data is static mock - `get_activity_data()` returns a hardcoded dict
- Process grouping uses Jinja2 `namespace()` pattern for dict building in templates
- Nested loop IDs use `{% set cat_idx = loop.index %}` (Jinja2 has no `loop.parent`)
- Mermaid.js loaded from CDN (`cdn.jsdelivr.net/npm/mermaid@11`) as ES module
- Mermaid diagrams lazy-rendered on first card expand (not on page load) for performance
- `history.replaceState()` for tab hash updates (avoids polluting browser back history)
- 2-column grids (leaderboard, feed) collapse to 1-column below 1024px via `@media` query
- Opportunity detail uses `event.stopPropagation()` to prevent card toggle when clicking inside detail
