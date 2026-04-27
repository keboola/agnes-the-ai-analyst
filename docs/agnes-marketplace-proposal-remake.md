# **Agnes Marketplace**

**AI Skills & Knowledge Distribution Platform**

## **Executive Summary**

We propose extending the Agnes data platform into a company-wide distribution system for AI skills, agents, and knowledge catalogs. Today, our Claude Code marketplace lives in a git repo that engineers must manually clone and update when maintaining, and install through the Claude Code marketplace feature when using. Agnes already has everything needed to replace that: user management, RBAC, an API layer, and automated data sync (rsync).

**What is missing** is a departments/teams structure, user positioning within those departments, and a governance mechanism with clear ownership across departments. The current GitHub-based marketplace has no way to control who sees what — everyone sees everything. Agnes solves this by attaching organizational placement (Department → Tribe → Team) to each user profile so the system automatically filters and surfaces only the skills relevant to that user's role. This eliminates user confusion and enforces data security at the distribution layer. Knowledgebase management, already available in a simplistic version in Agnes, would similarly be improved to support department-scoped permissions.

To ensure quality and prevent organizational silos, we introduce a **Code Owners** governance model and an **AI-driven review pipeline** that automates security checks, deduplication against corporate memory, proactively surfaces related in-flight work across teams, and routes approvals to the right owners — removing the bottleneck of manual human review while keeping humans in the loop for final sign-off. Authors can use their skills locally immediately while the review process runs in the background. Agnes's existing Activity Dashboard extends into a **central AI proxy** — giving leadership real-time visibility into how every department uses AI skills, where gaps exist, and what to build next.

| Goal: Any department can publish skills and knowledge for their Claude Code users. Users subscribe once and get automatic updates. No git access required. |
| :---- |

This is not a rewrite. It is three incremental additions to Agnes, each independently valuable, deliverable in 2-week sprints with Keboola engineering support.

---

## **What Exists Today**

### **Agnes (this codebase)**

A data distribution platform that already provides:

- FastAPI server with 69+ endpoints, JWT auth, Google OAuth  
- DuckDB-powered analytics with pluggable connectors (Keboola, BigQuery, Jira)  
- RBAC with role hierarchy (viewer / analyst / admin) and table-level permissions  
- Docker Compose deployment with scheduler, healthchecks, reverse proxy  
- 1,159 tests (81% test-to-production LOC ratio)

### **Claude Marketplace (separate git repo)**

An engineering-only knowledge system distributed as a Claude Code plugin:

- 10 architect agents, 23 skills for architecture guidance  
- 7 Node.js query scripts reading JSON catalogs (C4 model, Confluence, Jira, Swagger)  
- Daily CI/CD syncs: architecture, Jira, incidents, alerts; weekly: Confluence  
- 386 services documented, 5,415 Confluence pages indexed, 5,000 API endpoints cataloged

### **The Gap**

| Capability | Agnes | Marketplace |
| :---- | :---- | :---- |
| User management & RBAC | Yes | No |
| API layer | Yes | No (static files) |
| Multi-department support | Yes | No (engineering only) |
| Org hierarchy (dept / tribe / team) | No | No |
| Department/team-based filtering | Partially (needs user profile extension) | No (everyone sees everything) |
| Skills & agents | No | Yes |
| Knowledge catalogs | No | Yes (7 data sources) |
| Auto-updates to users | No | No (manual git pull) |
| Governance & ownership | No | No |
| Usage analytics & adoption visibility | Partially (Activity Dashboard draft) | No |

---

## **Organizational Hierarchy & Team Governance**

### **The Problem with the Current GitHub Marketplace**

The existing GitHub-based marketplace cannot properly manage visibility — everyone sees everything. There is no mechanism to restrict skills or knowledge by department, tribe, or team. This leads to user confusion (an HR employee seeing deeply technical SRE runbooks) and creates data security gaps where sensitive department-specific content is globally visible.

### **Hierarchy: Department → Tribe → Team (Squad)**

Organizational structure follows a three-level hierarchy. The Agnes governance model mirrors this directly:

**Department** is the top-level organizational unit (e.g., Engineering, HR, Finance, Commerce). Each department maps to one plugin — the distributable bundle that users receive. A department is the unit of RBAC filtering: a user in the HR department sees the HR plugin; a user in Engineering sees the Engineering plugin.

**Tribe** is a strategic grouping within a department (e.g., within Engineering: Platform, Product, Data). Tribes provide an intermediate grouping for ownership and review routing. A tribe lead can be a code owner for all teams within their tribe.

**Team (Squad)** is the working unit (e.g., within Platform tribe: CICDO, SRE, Cloud, GDS, AI Ops, IMOC). Squads own services, but service-level context is not modeled in the plugin structure (see "Why No Service-Level Granularity" below). Squads are the primary authors of skills and agents — a CICDO engineer writes a pipeline triage skill, an SRE engineer writes a runbook skill.

### **Why No Service-Level Granularity in Skills**

Teams own multiple services, and it might seem natural to organize skills at the service level. We intentionally do not do this. Service-level design context — architecture, dependencies, API contracts, runbooks — is the domain of the **architecture-as-code agent**. That agent pulls context dynamically from live documentation sources (Jira, GitHub repos, Structurizr, Confluence) via Agnes knowledge catalog queries. Teams do not need to embed service-specific context into their skills because it is automatically populated at runtime through queries to the architecture-as-code subagent.

Skills are therefore authored at the team level and describe *how to do something* (e.g., "how to triage a pipeline failure," "how to run a post-mortem"), not *what a specific service looks like*. The architecture agent handles the "what"; skills handle the "how."

### **Organizational Hierarchy Data Model**

```
org_units
├── org_unit_id (PK)
├── name (e.g., "Engineering", "Platform", "CICDO")
├── level (department / tribe / team)
├── parent_org_unit_id (FK → org_units, nullable)
├── owner_user_id (FK → users — the code owner for this unit)
└── created_at

user_org_memberships
├── user_id (FK → users)
├── org_unit_id (FK → org_units)
├── role (member / maintainer / admin)
└── assigned_at
```

This single recursive table supports the full hierarchy. A user can belong to multiple org units (e.g., a Platform Engineering lead is a member of both the Platform tribe and the Engineering department). RBAC inheritance flows downward: a user with access to the Engineering department inherits access to all tribes and teams within it.

### **User Profile and Automatic Filtering**

When a user logs into Agnes via Google OAuth, their organizational placement is resolved (from Google Workspace directory sync or manual admin assignment). The system then automatically:

- Filters the skill/plugin catalog to show only what is relevant to the user's department and its children  
- Surfaces cross-department skills only when explicitly published as "company-wide"  
- Restricts knowledge catalog access based on org-unit-level permissions  
- Allows department and tribe admins to manage their own plugins without touching other departments

This means an HR skill is only offered to the HR department, a Sales skill only to Sales, and shared infrastructure skills to everyone. Users are not overwhelmed with irrelevant content, and sensitive departmental knowledge stays within its intended audience.

---

## **Code Owners Governance Model**

### **Ownership at Every Level of the Hierarchy**

We introduce a **Code Owners** system that maps directly to the organizational hierarchy. Ownership is defined per org unit — each department, tribe, and team has a designated owner who is responsible for reviewing and approving skills and agents contributed to their scope.

The ownership is stored in the `org_units` table (the `owner_user_id` field), but for the underlying git repository that Agnes operates on, it is also expressed as a `CODEOWNERS` file that maps the plugin directory structure to owners:

```
# Department-level plugins
/plugins/engineering/          @vp-platform-engineering
/plugins/commerce/             @commerce-lead
/plugins/hr/                   @hr-lead
/plugins/finance/              @finance-lead

# Tribe-level ownership (within department plugins)
/plugins/engineering/agents/platform-*    @platform-tribe-lead
/plugins/engineering/agents/product-*     @product-tribe-lead

# Team-level ownership (within department plugins)
/plugins/engineering/skills/cicdo-*       @cicdo-lead
/plugins/engineering/skills/sre-*         @sre-lead
/plugins/engineering/skills/aiops-*       @gabriela
/plugins/engineering/skills/imoc-*        @imoc-lead
```

The plugin directory structure follows the pattern:

```
/plugins/{department}/
├── plugin.json
├── agents/
│   ├── {agent-name}.md
│   └── ...
├── skills/
│   ├── {skill-name}/
│   │   └── SKILL.md
│   └── ...
├── tools/
│   ├── {tool-name}/
│   │   └── ...
│   └── ...
└── hooks/
    ├── {hook-name}.sh
    └── ...
```

Each department has one plugin. Inside the plugin, agents, skills, tools, and hooks are organized in flat directories. The naming convention (e.g., `cicdo-pipeline-triage`, `sre-runbook-executor`) ties artifacts to their owning team without imposing deep folder nesting. The CODEOWNERS file uses glob patterns against these names to route reviews to the correct team lead.

### **Cross-Team and Cross-Department Skills**

When an employee creates a skill that spans multiple teams or departments (e.g., an incident response skill that touches both IMOC and SRE, or a cost optimization skill that spans Engineering and Finance), the system automatically identifies overlapping ownership from the CODEOWNERS mapping and the `org_units` hierarchy. It requires review from **all affected code owners** before the skill is published organization-wide. This prevents one team from unintentionally overwriting or conflicting with another team's established practices.

---

## **AI-Driven Skill Review Pipeline**

Manual human review of every submitted skill does not scale. Instead, when an employee submits a new skill through Agnes, the system creates a pull request and runs it through a multi-stage automated review pipeline. Critically, this pipeline does not only evaluate the new submission in isolation — it also scans all existing open PRs and the current registry to surface related work happening across the organization, ensuring teams are aware of what others are building.

### **Stage 1: Security Guardrails (Automated)**

An AI agent scans the submitted SKILL.md for:

- Hardcoded secrets, credentials, API keys, or PII  
- References to internal-only URLs or endpoints that should not be in a skill file  
- Prompt injection patterns or instructions that could bypass safety controls  
- Compliance with the standard SKILL.md format (YAML frontmatter \+ markdown body)

If the agent detects a security issue, the PR is flagged and returned to the author with specific remediation guidance. No human reviewer needs to spend time on obviously non-compliant submissions.

### **Stage 2: Corporate Memory & In-Flight Awareness (Automated)**

The AI agent queries Agnes's existing skill registry, knowledge catalogs, **and all currently open PRs** to determine:

- Whether an identical or substantially similar skill already exists in the registry — even in a different department  
- Whether an existing skill could be extended rather than duplicated  
- Whether the submission conflicts with or contradicts established company-wide skills  
- **Whether any other team currently has an open PR for a related or overlapping skill**

This last point is the key de-siloing mechanism. The system does not only check what is already published — it checks what is currently being built. If the Finance team submits a "BigQuery query helper" skill and the Data Engineering team has an open PR for a similar skill submitted two days ago, both authors and both code owners are immediately notified. The notification includes a summary of the overlap, links to both PRs, and a recommendation: collaborate on a single shared skill rather than maintaining two.

**Proactive notifications for all open PRs:** Whenever a new PR is created, Agnes broadcasts a lightweight digest to relevant code owners and tribe leads summarizing what is incoming across the organization. This is not a blocking notification — it is an awareness feed. A weekly digest email or Slack post aggregates all open and recently merged PRs, grouped by department, so leadership has visibility into what the marketplace is evolving into. This prevents the scenario where two teams independently build similar capabilities without knowing about each other until both are already published.

### **Stage 3: Code Owner Approval (Human, One Click)**

Only after the AI agent clears both security and corporate memory checks does the PR get routed to the relevant Code Owner(s) for final approval. At this point, the code owner is not reviewing boilerplate compliance or checking for secrets — that work is done. They are making a judgment call on whether the skill is appropriate for their scope and meets quality standards.

The code owner receives a notification (email or Slack) with:

- A summary of the AI review findings (security: passed, duplicates: none found, related open PRs: list)  
- The full SKILL.md content for review  
- A one-click approve button

For cross-team or cross-department skills, all affected code owners must approve. The system tracks approval state and sends reminders if a PR is pending review for more than 48 hours.

### **Local-First Usage: Immediate Use Before Approval**

A strict "wait for approval before use" model creates friction that discourages contribution. An engineer who writes a skill to solve a problem they have right now should not have to wait days to use it. We propose a **local-first model** that lets authors use their skills immediately while the review pipeline runs in the background.

**How it works:**

1. **Author creates a skill locally.** They write a SKILL.md in their local Claude Code plugin directory (e.g., `~/.claude/plugins/engineering/skills/my-new-skill/SKILL.md`). Claude Code discovers it immediately on the next session. The author can use it right away — no submission, no approval needed for personal use.

2. **Author submits for organization-wide distribution.** When the author is satisfied the skill works, they submit it via Agnes (`da skill submit ./SKILL.md`). This triggers the AI review pipeline. The skill continues to work locally for the author throughout the review process.

3. **During review: skill is local-only.** The skill exists on the author's machine and is fully functional for them. It is not yet distributed to other users via the Agnes sync. Other team members cannot see or receive it until it is approved.

4. **After approval: skill goes live.** Once the code owner approves, Agnes publishes the skill to the registry. On the next sync cycle, all entitled users in the relevant department/tribe/team receive it automatically. The author's local copy is replaced by the canonical version from Agnes.

5. **If rejected: skill remains local-only.** The author keeps their local copy and can continue using it personally. They receive feedback from the reviewer and can revise and resubmit. There is no disruption to their workflow.

**Sharing before approval (team-level draft):** For cases where an author wants their immediate team to try a skill before formal submission, Agnes supports a "draft share" mode. The author submits the skill with a `--draft` flag, which makes it available to members of their team (squad) only, without triggering the full review pipeline. Team members see it tagged as "\[Draft\]" in their sync. When the team is ready, any member can promote it to a formal submission, which enters the standard review pipeline. This allows teams to iterate together without waiting for org-wide approval, while keeping the blast radius small.

**Why this matters:** The local-first model removes the tension between governance and velocity. Engineers are never blocked from using their own work. The review pipeline is a gate for *distribution*, not for *creation*. This encourages experimentation — an engineer can prototype five skills locally, test them, discard three, and submit the two that actually work. The review process only receives high-quality, battle-tested submissions.

### **Why This Pipeline Matters**

This pipeline ensures quality and organizational coherence without creating a bottleneck. It catches security issues before humans see them, prevents the organization from building the same thing twice in different silos, proactively connects teams working on related problems, and reduces the code owner's review burden to a focused, high-signal decision. The local-first model ensures that governance never stands between an engineer and their tools.

---

## **Proposed Architecture**

Merge the marketplace into Agnes. Agnes becomes both the data platform and the skill/knowledge distribution server.

### **Three New Concepts in Agnes**

| Concept | What It Is | DuckDB Table |
| :---- | :---- | :---- |
| Knowledge Catalog | Any searchable data source (C4, Confluence, Jira, CSV, Google Sheets) | `catalogs` \+ `catalog_items` |
| Skill | A SKILL.md file that teaches Claude Code how to do something | `skill_registry` |
| Plugin | A bundle of skills \+ agents \+ tools \+ hooks for a department | `plugin_registry` |

A plugin is the distribution unit. One plugin per department. Inside a plugin: agents (autonomous AI roles), skills (instructional SKILL.md files), tools (reusable scripts and integrations), and hooks (lifecycle hooks like SessionStart). Users receive the whole plugin for their department; the organizational hierarchy determines entitlement.

### **Generic Knowledge Catalogs**

Instead of one script per data source, a single generic table stores any structured knowledge. Each catalog declares its own schema (what fields exist, which are searchable, how to display summaries). One set of API endpoints handles all catalogs.

| Endpoint | Purpose |
| :---- | :---- |
| `GET /api/knowledge/{catalog}/search?q=...` | Search any catalog |
| `GET /api/knowledge/{catalog}/item/{id}` | Get item \+ children \+ relationships |
| `GET /api/knowledge/{catalog}/doc/{id}/{type}` | Read a document (Confluence page, runbook) |
| `GET /api/knowledge/{catalog}/overview` | Catalog stats and freshness |
| `POST /api/knowledge/catalogs` | Register a new catalog (admin) |

Adding a new data source means writing one Python connector class (\~50-100 lines) that implements `extract()`. No new tables, no new endpoints, no new query scripts.

Service-level context (architecture, dependencies, API specs) flows through these catalogs and is queried at runtime by the architecture-as-code agent. Teams do not embed this context into skills — it is always live and always current.

### **Connector Examples**

| Department | Source Type | Connector | Data |
| :---- | :---- | :---- | :---- |
| Engineering | Structurizr | Built-in | C4 architecture model |
| Engineering | Confluence | Built-in | Wiki pages by space |
| Engineering | Jira | Built-in | Projects, epics, sprints |
| Engineering | Swagger/OpenAPI | Built-in | API endpoint specs |
| Finance | Google Sheets | New (\~60 lines) | Financial reports, KPIs |
| HR | Confluence | Reuse existing | HR policies (different spaces) |
| Legal | CSV upload | New (\~30 lines) | Contract templates |
| Marketing | Notion | New (\~80 lines) | Campaign playbooks |

---

## **Skill & Plugin Distribution: Two Options**

There are two approaches to how users receive skills and plugins from Agnes. Both are viable; they differ in whether the user's machine pulls artifacts on demand or receives them via server-side sync.

### **Option A: UI-Driven Install (Catalog \+ Remote Command)**

Agnes lists all plugins, agents, skills, tools, and hooks in its web UI — similar to how the data catalog works today. Based on the user's organizational placement and permissions, they see only what is relevant to them.

When a user wants a plugin, they click **Install** in the Agnes UI. Agnes either:

1. Executes `claude plugin install <plugin-name>` remotely on the user's machine (if an Agnes CLI agent is running), or  
2. Displays a ready-to-paste terminal command that the user copies and runs

**Pros:** Familiar app-store interaction model. Users have explicit control over what gets installed. No background sync process to debug.

**Cons:** Requires user action for every update. Needs either a running local agent or manual copy-paste for each install. Git/GitHub access may still be needed depending on how `claude plugin install` resolves artifacts.

### **Option B: RBAC-Driven Rsync (No Git Required) — Recommended**

Agnes uses its existing rsync infrastructure (the same mechanism used for Parquet data files) to push skill and plugin files directly to the user's machine. The sync runs automatically on every Claude Code session start via a SessionStart hook.

**How it works:**

1. User's org unit memberships determine which plugins they are entitled to  
2. The SessionStart hook calls Agnes API: "give me my plugins" (authenticated, RBAC-filtered)  
3. Agnes responds with the current versions of all entitled plugin artifacts (SKILL.md files, agent definitions, tools, hooks, plugin.json)  
4. The hook writes them to the local Claude Code plugin directory — merging with any local-only skills the user has created  
5. Claude Code discovers them on session start as it normally would

**Skill submission under Option B:**

When an author submits a skill (via Agnes web editor or CLI), they specify the target team — or the system infers it from their profile context. Agnes creates the PR server-side — the author never touches git or GitHub. The AI review pipeline runs, code owners approve, and the skill is published to Agnes's registry. On the next sync cycle, all entitled users receive it automatically.

**Local skills are preserved:** The sync process never deletes files in a `local/` subdirectory of the user's plugin folder. This is where local-only and draft skills live. The sync only writes to the managed directories, ensuring the author's in-progress work is never overwritten.

**Pros:** Zero git/GitHub access required for any user. Fully automatic updates. Leverages Agnes's existing rsync and RBAC infrastructure. Authors submit skills without needing repository access. Local-first workflow is fully compatible.

**Cons:** Users get updates without explicit opt-in (mitigated by department filtering and the ability to unsubscribe). Requires the SessionStart hook to be installed once.

**Recommendation:** Option B is the stronger choice. It eliminates the git dependency entirely, reuses existing Agnes infrastructure, and delivers the zero-friction experience that drives adoption. Option A can be offered as a fallback for power users who want manual control.

---

## **Zero-Friction User Experience**

### **One-Time Setup (30 seconds)**

The user opens Agnes web UI, logs in with Google, clicks "Connect Claude Code." Agnes shows a single command to copy-paste into their terminal:

```
curl -s https://agnes.internal/setup | bash
```

This installer writes three things to the user's machine:

- A sync script (`~/.claude/agnes-sync.sh`) that checks Agnes for plugin updates based on the user's RBAC profile  
- A SessionStart hook in Claude Code settings that runs the sync script on every session  
- An MCP connection to Agnes for live data queries (no local data files needed)

### **After Setup: Fully Automatic**

| Event | What Happens | User Action |
| :---- | :---- | :---- |
| User opens Claude Code | SessionStart hook syncs entitled plugins silently | None |
| Admin publishes new skill | Next session picks it up for entitled users | None |
| Data refreshed by CI/CD | Plugin version bumps, hook downloads update | None |
| User changes team/department | Next sync adjusts available plugins automatically | None |
| User creates a local skill | Works immediately, no submission needed | None |
| User submits skill for review | Continues using it locally while review runs | None |
| User wants to browse available skills | Opens Agnes web UI, sees org-filtered catalog | Optional |

### **No Git Access Required**

Users never touch the marketplace git repo. They never run `/plugin` commands. Agnes is the only distribution point. The sync hook handles everything. Skill authors submit through Agnes, and the server-side pipeline handles PR creation, AI review, and publication.

### **How Claude Code Sees It**

Claude Code cannot tell the difference. The sync hook writes the same directory structure that a git-based plugin would: `plugin.json`, `skills/*/SKILL.md`, `agents/*.md`, `tools/*`, `hooks/*`. Claude Code discovers skills and agents by scanning these directories on session start.

The only difference: data queries go to Agnes via MCP instead of running local Node.js scripts against local JSON files. This eliminates \~30MB of local data and ensures every query returns fresh, RBAC-filtered results. Service-level context (which service am I working on, what does its architecture look like) is resolved dynamically by the architecture-as-code agent querying Agnes catalogs — never hardcoded into skills.

---

## **Usage Analytics & Central Proxy Dashboard**

Agnes already has a draft Activity Dashboard with the right conceptual framework: active users, business processes, decisions supported, success rate, adoption trend, maturity roadmap, and team-level views. The marketplace extension turns this from a data analytics dashboard into a **central AI proxy** — the single point of visibility into how the entire organization is using AI skills, where gaps exist, and what to build next.

All marketplace activity flows through Agnes: every MCP query from Claude Code, every plugin sync, every skill submission, every review. This means Agnes has complete telemetry without requiring any instrumentation inside Claude Code itself. The SessionStart hook reports which plugins were synced; MCP queries are logged server-side; the skill registry tracks submissions, approvals, and rejections.

### **Dashboard Views**

**Overview Tab** — maps directly to the existing Agnes dashboard header. The top-level KPIs become:

| Metric | What It Measures | Source |
| :---- | :---- | :---- |
| Active Today | Users who synced plugins or ran MCP queries today | Sync logs \+ MCP request logs |
| Business Processes | Number of distinct skills actively invoked this week | MCP query logs grouped by skill |
| Decisions Supported | MCP knowledge queries answered (catalog lookups, architecture queries) | MCP request logs |
| Success Rate | % of MCP queries that returned actionable results (non-empty, non-error) | MCP response logs |
| Adoption Trend | Week-over-week change in active users and query volume | Time-series aggregation |

**Teams Tab** — shows per-department and per-squad breakdown. For each team: how many active users, which skills they use most, which catalogs they query, how many skills they have contributed. This is the CEO's "central proxy" view — at a glance, leadership sees which departments are AI-active and which are not.

**Activity Tab** — real-time feed of marketplace events: skill submissions, approvals, new plugin versions published, notable query patterns. This is where the weekly PR digest also surfaces. Sortable and filterable by department, tribe, team.

**Processes Tab** — maps skills to business processes. Each skill in the registry can be tagged with the business process it supports (e.g., "incident response," "cost optimization," "deal onboarding"). This view aggregates usage by process, not by team — answering "how much AI support does incident response have across the whole company?" regardless of which team's skill is being used.

**Opportunities Tab** — the most strategically valuable view. Agnes analyzes query patterns and usage data to surface:

- **Missing skills:** Users repeatedly querying knowledge catalogs for topics where no skill exists. For example, if 15 users this week searched for "how to roll back a deployment" but no rollback skill is published, that surfaces as an opportunity.  
- **Underused skills:** Skills that were published but rarely invoked — candidates for improvement, better documentation, or retirement.  
- **Data gaps:** MCP queries that returned empty or low-quality results, indicating a knowledge catalog that needs enrichment or a new connector.  
- **Cross-team collaboration opportunities:** Teams independently querying similar topics or building similar draft skills — the system recommends they connect.

### **Maturity Roadmap**

The existing Agnes maturity model (Developing → Mature → Optimized) applies naturally to marketplace adoption per department:

**Developing** — Department has a plugin with initial skills imported. Users are onboarded and syncing. Basic MCP queries are flowing. Fewer than 50% of team members are active weekly.

**Mature** — Department has team-authored skills beyond the initial import. Skill submission pipeline is active with regular contributions. Knowledge catalogs are connected and queried daily. More than 50% of team members are active weekly. At least one cross-department skill contribution.

**Optimized** — Department has high skill coverage for its core business processes. Usage data drives skill iteration (skills are updated based on opportunity signals). Department contributes skills used by other departments. Query success rate above 80%. Active participation in cross-department review.

Each department's maturity score is computed automatically from the telemetry data Agnes already collects. The dashboard shows the aggregate maturity ring (same visual as the existing Agnes draft) and the per-department breakdown.

### **Data Model for Analytics**

```
marketplace_events
├── event_id (PK)
├── event_type (sync / mcp_query / skill_submit / skill_approve / skill_reject / draft_share)
├── user_id (FK → users)
├── org_unit_id (FK → org_units)
├── skill_id (FK → skill_registry, nullable)
├── catalog_id (FK → catalogs, nullable)
├── query_text (for MCP queries — what was asked)
├── response_quality (success / empty / error)
├── session_id (Claude Code session identifier)
├── created_at
└── metadata (JSONB — flexible field for event-specific data)
```

This single event table powers all dashboard views. DuckDB handles the analytical queries efficiently. No separate analytics pipeline is needed — Agnes queries its own event table directly.

---

## **Skill Submission by Users**

Any employee can contribute skills to their department's plugin. The process is designed to be frictionless for the author while maintaining quality and preventing organizational silos.

### **Submission Flow**

1. Author writes a SKILL.md locally and uses it immediately (local-first)  
2. When ready, submits via Agnes web UI or CLI: `da skill submit ./SKILL.md`  
3. Author specifies the target team — or the system remembers it from their profile context  
4. Agnes creates a PR server-side (author needs no git/GitHub access)  
5. **AI Review Pipeline runs automatically:**  
   - Stage 1: Security guardrails check (secrets, PII, format compliance)  
   - Stage 2: Corporate memory \+ in-flight PR scan (deduplication across all departments, overlap detection with open PRs, notification to related teams)  
   - Stage 3: If AI clears both checks → routes to Code Owner(s) for one-click approval  
6. Author continues using the skill locally throughout the review process  
7. Approved skill is published to the plugin registry  
8. All entitled subscribers receive it on next Claude Code session

### **Cross-Team and Cross-Department Submissions**

If a skill spans multiple teams or departments, the system detects this from the CODEOWNERS mapping and the org hierarchy. It requires approval from all affected code owners. The AI review stage specifically highlights the cross-boundary nature, any overlapping existing skills, and any related open PRs to each reviewer, ensuring informed decisions and encouraging collaboration rather than parallel effort.

### **Draft Sharing Within a Team**

Before formal submission, an author can share a skill with their immediate team using `da skill submit ./SKILL.md --draft`. Draft skills are synced only to members of the author's squad, tagged as "\[Draft\]" in the UI and in Claude Code's skill listing. Drafts do not go through the full review pipeline — they are for rapid team-level iteration. Any team member can promote a draft to a formal submission when the team is ready for org-wide distribution.

---

## **Delivery Plan**

Each phase is independently valuable. Ship Phase 1 and you already have a better system than today. Each subsequent phase adds capability without requiring the others.

**Keboola engineers are available to collaborate on delivery**, bringing Agnes platform expertise to accelerate all three phases. The proposed timeline assumes a joint AI Foundry \+ Keboola team working in parallel.

### **Phase 1: Knowledge Catalogs \+ Organizational Hierarchy (2 weeks)**

**AI Foundry owns:** Org hierarchy data model (department / tribe / team), user profile extension, RBAC scoping by org unit, CODEOWNERS mapping.

**Keboola engineers own:** Generic catalog tables in DuckDB, connector framework, API endpoints, MCP endpoint.

Deliverables:

- `org_units` and `user_org_memberships` tables in DuckDB  
- Three-level hierarchy support (department / tribe / team) with ownership per unit  
- User profile org placement (resolvable from Google Workspace or manual assignment)  
- `catalogs` \+ `catalog_items` tables in DuckDB  
- Generic search/item/doc/overview endpoints  
- Marketplace connector that reads existing JSON catalogs  
- MCP endpoint so Claude Code can query catalogs server-side  
- `marketplace_events` table and event logging for MCP queries and sync events

**Impact:**

- All marketplace knowledge queryable via SQL and MCP  
- RBAC controls who sees what, scoped by org unit hierarchy  
- Eliminates 30MB of local data files from plugin  
- Event telemetry starts collecting from day one — enables analytics in later phases  
- Foundation for org-filtered experience in all subsequent phases

### **Phase 2: Plugin & Skill Registry \+ Distribution (2 weeks)**

**AI Foundry owns:** Plugin directory structure (per-department plugin with agents/skills/tools/hooks), rsync-based distribution (Option B), SessionStart hook installer, local-first skill workflow.

**Keboola engineers own:** Skill/agent/plugin registry tables, plugin builder API, web UI for browsing and subscribing, activity dashboard extension.

Deliverables:

- `skill_registry`, `agent_registry`, `plugin_registry` tables  
- Import existing grpn skills and agents into registry  
- `GET /api/marketplace/plugins/{name}/archive` endpoint  
- Web UI: browse plugins (org-hierarchy-filtered), subscribe/unsubscribe  
- SessionStart hook installer (`curl | bash`)  
- Rsync-based plugin distribution with RBAC filtering and local skill preservation  
- CODEOWNERS file structure and ownership resolution logic across department/tribe/team  
- Local-first workflow: local `skills/` directory excluded from sync overwrite  
- Activity Dashboard: Overview and Teams tabs (active users, business processes, adoption trend, per-department breakdown)

**Impact:**

- Users subscribe via web UI, get auto-updates via rsync — no git access needed  
- Org-hierarchy-filtered catalog ensures users see only relevant skills  
- Code ownership formally established at department, tribe, and team levels  
- Authors can create and use skills locally without waiting for approval  
- Leadership has visibility into per-team AI adoption from day one  
- Foundation for AI-driven review pipeline

### **Phase 3: AI Review Pipeline \+ Multi-Department Onboarding (2 weeks)**

**AI Foundry owns:** AI review agent (security guardrails, corporate memory \+ in-flight PR awareness), cross-team/department review routing, proactive notification system, first non-engineering department onboarding.

**Keboola engineers own:** Skill submission API and web editor, server-side PR creation, draft sharing mechanism, review dashboard, weekly digest system, advanced analytics (Opportunities, Processes, maturity scoring).

Deliverables:

- Skill submission API and web UI / CLI (`da skill submit` and `da skill submit --draft`)  
- Server-side PR creation (no git access needed for authors)  
- AI review agent: security scan \+ corporate memory deduplication \+ open PR overlap detection  
- Proactive notification to related teams when overlapping work is detected  
- Weekly digest of open and recently merged PRs across all departments  
- Code Owner notification and one-click approval flow  
- Cross-team and cross-department review routing via org hierarchy  
- Draft sharing within squads (team-scoped sync without full review)  
- First non-engineering plugin onboarded (e.g., Finance with Google Sheets connector)  
- Activity Dashboard: Activity tab (real-time event feed), Processes tab (skill-to-business-process mapping), Opportunities tab (missing skills, data gaps, underused skills, collaboration signals)  
- Automated maturity scoring per department (Developing → Mature → Optimized)

**Impact:**

- Company-wide platform, not just engineering  
- Any department can publish and distribute AI skills  
- AI-driven review prevents duplication, enforces security, and connects teams building related capabilities  
- In-flight awareness eliminates the scenario where two teams independently build the same thing  
- Local-first \+ draft sharing means governance never blocks individual or team velocity  
- Opportunities dashboard gives leadership a data-driven view of where to invest in AI skills next  
- Maturity scoring creates a natural adoption incentive across departments  
- Self-service: teams manage their own plugins with automated governance

---

## **Risks and Mitigations**

| Risk | Likelihood | Mitigation |
| :---- | :---- | :---- |
| DuckDB single-writer bottleneck under load | Low | Current scale is fine. Monitor write contention; shard by plugin if needed |
| SessionStart hook reliability across OS | Medium | Test on Windows, macOS, Linux. Fail silently (no hook \= plugin still works, just stale) |
| MCP latency for data queries | Low | Server-side DuckDB is fast. Cache frequent queries. Fallback: local data option |
| User adoption friction | Low | One-time setup is one command. After that, fully automatic |
| Org hierarchy mapping accuracy | Medium | Seed from Google Workspace directory. Allow admin overrides. Surface unmapped users in dashboard |
| AI review false positives (blocking valid skills) | Low | AI flags for human review rather than auto-rejecting. Corporate memory check suggests consolidation, does not block |
| Local skill divergence from approved versions | Low | Sync replaces local copies with canonical versions once approved. Authors are notified. Draft skills are explicitly tagged |

---

## **What We Are Not Building**

This proposal intentionally avoids complexity that does not serve the core goal:

- **No custom query language.** DuckDB SQL is the query language.  
- **No agent execution server.** Skills and agents run locally in Claude Code.  
- **No real-time collaboration.** Plugins are versioned artifacts, not live documents.  
- **No plugin dependency system.** Plugins are self-contained bundles.  
- **No marketplace billing or monetization.** Internal tool, free for all employees.  
- **No per-query cost tracking or billing.** Usage analytics track activity patterns and adoption, not individual query costs.  
- **No manual-only review process.** The AI pipeline handles the heavy lifting; humans do final sign-off.  
- **No service-level skill granularity.** Service context is dynamically resolved by the architecture-as-code agent at runtime, not embedded in skills.

