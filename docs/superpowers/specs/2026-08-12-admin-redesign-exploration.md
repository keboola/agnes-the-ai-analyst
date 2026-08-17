# Admin experience redesign — UX exploration

**Status:** exploration + mockup only. No implementation.
**Mockup:** [`2026-08-12-admin-redesign-mockup.html`](2026-08-12-admin-redesign-mockup.html)
**Date:** 2026-08-12

---

## 1. What the admin is actually trying to do

Three jobs, in this order, forever:

1. **Add and manage people.**
2. **Connect data.**
3. **Get the right data to the right people.**

Everything else in `/admin` — moderation, prompts, backends, telemetry — is
maintenance of an instance that already does those three things.

## 2. What the product makes them do instead

### 2.1 The object graph they must learn first

To move one table to one analyst, an admin must currently understand and
operate six objects and five dependencies between them:

```
Source connection ──storage token──► registered table ──┐
        │                                               │
        └──master (owner) token──► semantic layer       │
                                                        ▼
                                                  Data package
                                                        │
                                            resource_grant (group, package)
                                                        │
                                              requirement: available | required
                                                        ▼
                                        group ──members──► user ──► stack
                                                        │
                                                  `agnes pull`
```

Five of these are invisible until you hit them:

- **A second token.** The semantic layer needs a Keboola *master (owner)*
  token, distinct from the storage token that connected the project. The only
  places that is stated are `/admin/semantic-layer`'s empty state and a compact
  row inside a connection card on `/admin/data-sources`
  ([admin_data_sources.html:437](../../../app/web/templates/admin_data_sources.html)).
  An admin who never opens the semantic-layer page never learns the dependency
  exists.
- **Tables outside a package reach nobody.** Per-table `resource_grants` no
  longer surface tables in analyst manifests — a table must be in a Data
  Package. Nothing on `/admin/tables` states this as a consequence; the
  unpackaged bucket is presented as a neutral grouping, not as "nobody can see
  these".
- **A granted package is still not delivered.** `available` shows it in the
  analyst's Browse; only `required` (or an explicit subscribe) puts it in
  their stack. The two tiers are named after the database column, not after
  what happens.
- **Nothing lands until `agnes pull` runs.** No admin surface says "3 people
  have not pulled since Tuesday".
- **Grants are one-directional in the UI.** The grant matrix exists only on a
  group's detail page. `/admin/data-packages` is explicitly a *read-only*
  inventory — its Add/Remove footer buttons are hidden by CSS
  ([admin_data_packages.html:29](../../../app/web/templates/admin_data_packages.html)).
  So "who can see this package?" has no answer anywhere in the product.

### 2.2 The IA is named after tables, not verbs

`app/web/admin_nav.py` — **30 rows in 7 collapsible sections**. The three jobs
are scattered:

| Admin's job | Pages they must visit, in order |
|---|---|
| Add data | Data sources → (Instance secrets) → Tables → Sync → Semantic layer |
| Share data | Tables (create package) → Groups → *a* group → Access tab → find the package → tick → pick tier |
| Manage people | Users → *a* user → Groups → *a* group → Members → Tokens |

Section names ("Tables", "Groups", "Data packages") are the schema. The admin
has to translate intent → object → page before every action.

### 2.3 Two navigation columns, ~430px of chrome

On `/admin/*` the app rail renders in icon mode (56px) *plus* the 240px admin
column — a second, full navigation tier. `_app_rail.html` acknowledges the
cost in a comment: the rail defaults to collapsed on `/admin` because "430px of
chrome measurably squeezes the content on an 1100px viewport there". Two tiers
of nav for one area is the symptom; 30 rows is the cause.

### 2.4 No state, anywhere, for the thing being managed

Five stages — connected, synced, packaged, shared, delivered — live on five
pages with five unrelated status vocabularies:

| Surface | Vocabulary |
|---|---|
| Data sources | `env` / `vault` / `unset`, `default` |
| Sync | `ok` / error / row + byte counts |
| Semantic layer | `✓ n updated, n pruned` / `skipped (duplicate project)` / `✗ error` |
| Data packages | `prod` / `poc` / `coming-soon` / `draft` |
| Group → Access | checkbox + `available` / `required` |

`/admin` itself answers *"what needs me?"* well (`admin_signals.py` — two
zones, decisions and breakage, zero renders nothing). But it answers nothing
about *where the admin is*: a brand-new instance with no data connected shows
"Nothing needs your attention." That is the worst possible first-run signal.

### 2.5 One page carries most of the load

`admin_tables.html` is **6,164 lines**: six modals, a connector-picker dropdown
for "+ Register new table", `Group tables by bucket`, `Bulk assign tables`,
`+ New Data Package`, and cache-freshness warmup folded into a `<details>` in
the toolbar. The good ideas in there (bucket→package suggestion, bulk assign,
inline RBAC in the create modal) are buried as peer toolbar buttons with no
sense of when they are the right thing to press.

### 2.6 Vocabulary

Words the UI uses that describe the implementation: *register* a table,
*bucket*, `query_mode` (`local` / `remote` / `materialized`), *master token*,
*semantic layer*, *resource grant*, *requirement tier*, *stack*, *manifest*,
*orphaned rows*.

---

## 3. The redesign

### 3.1 One mental model: People → Data → Access

Three destinations in the order the work happens, one home in front of them,
and three maintenance areas behind:

```
Overview          where am I, what needs me, what's next
People            accounts · groups · invitations · tokens
Data              sources · tables · packages    (one surface, three lenses)
Access            who can use what               (editable from both sides)
────────────────
Library           marketplaces · submissions · memory · digests · studio · news
Instance          config · backend · prompts · workspace · secrets · connections
Activity          audit · telemetry · sessions · adoption
```

**Seven rail rows, one navigation tier, no collapsible sections, no second
column.** The admin rail replaces the app rail on `/admin/*` instead of
stacking on top of it, recovering ~190px of content width.

### 3.2 Four structural moves

**(a) The source owns its whole vertical.**
`Data sources` + `Tables` + `Sync` + `Semantic layer` are four pages over one
object: a connected project. They become one expandable source panel with a
pipeline strip — *tables · last sync · semantic layer · packages fed* — and
four sub-panels behind it. `/admin/semantic-layer` already admits this shape:
"One row per Keboola project with a master token." The master-token dependency
stops being a page you have to find and becomes a warn chip on the source that
needs it, at the moment its absence matters.

**(b) The relationship is the object.**
Sharing gets one editor, rendered from both sides:
- from a group — *"what can Finance use?"*
- from a package — *"who can use Revenue?"*

Same component, same tier control, so there is one thing to learn. This adds
the missing direction rather than replacing the existing one.

**(c) Tier and delivery in plain language, with the truth attached.**

| Today | Redesign |
|---|---|
| `available` | **Optional** — shows in their Library, they add it themselves |
| `required` | **Automatic** — added to their workspace on next sync |
| *(nothing)* | **14 people will get this · 11 have pulled it · 3 not since Aug 4** |

That last row is the end-to-end status the product currently cannot show at
all, and it is the answer to "did my change actually reach anyone?".

**(d) The gap is the metric.**
Overview does not show vanity counts. Each number is a gap with an action:

- 148 tables available · 46 added · **6 in no package →**
- 23 people · 5 groups · **2 with no data access →**
- 7 packages · 5 shared · **2 shared with nobody →**

Plus a self-retiring setup path for a fresh instance (4 steps, live state,
disappears once data reaches a person), in front of the existing
`admin_signals.py` zones — which stay exactly as they are.

### 3.3 Progressive disclosure, not removal

Every advanced control keeps a home, one level down, in the context that owns
it:

| Advanced capability | New home |
|---|---|
| `query_mode` local/remote/materialized, custom SQL, partitioning, clustering | Table drawer → **Advanced**, with the raw value shown as a mono chip next to the plain-language label |
| Sync schedule, run history, manual trigger, failures | Source → **Sync** panel |
| Semantic-layer refresh, per-project counts, orphaned rows | Source → **Semantic layer** panel |
| Storage token, semantic-layer token, rotate, test, set default, delete | Source → **Settings** panel |
| Cache warmup | Source → Sync panel (it is a per-source freshness job, not a toolbar button) |
| `Group tables by bucket` | A **suggestion** in the Add-data flow, at the step where it is the right move: "These 12 tables come from 3 buckets — create one package per bucket?" |
| `Bulk assign tables` | The action on the unpackaged tray, where the problem is visible |
| Package status prod/poc/coming-soon/draft, category, icon, colour, cover | Package → **Settings** |
| Every non-data `ResourceType` (skills, agents, memory, collections, chat, Slack, recipes, data apps, files) | Access → collapsed **"Also grantable"** groups below data packages |
| Per-user effective access | Access → **Simulate**, promoted from a section on the user-detail page to a first-class tool |
| Tokens / PATs | People → **Tokens** lens (fleet-wide) + the per-user slice on user detail |

### 3.4 Terminology

Lead with the plain-language line; keep the system noun so CLI, API and docs
still match.

| System word | Shown as | Kept where |
|---|---|---|
| Register a table | **Add tables** | API `POST /api/admin/register-table` |
| `query_mode: local` | **Synced copy** `local` | mono chip beside the label |
| `query_mode: remote` | **Live query** `remote` | ” |
| `query_mode: materialized` | **Saved query** `materialized` | ” |
| Master token | **Semantic-layer token** (project owner) | field hint explains why |
| Semantic layer | **Semantic layer** — *business metrics & glossary* | name kept, always subtitled |
| Data package | **Data package** — *a bundle of tables people add to their workspace* | name kept everywhere |
| Resource grant | *never shown* — "shared with" / "who can use this" | API unchanged |
| `available` / `required` | **Optional** / **Automatic** | API unchanged |
| Stack | **Workspace** | `agnes stack` CLI unchanged |
| Bucket | **Bucket** | it is Keboola's word; analysts know it |

### 3.5 Nothing is removed — full mapping of the current 30 rows

| Current nav row | New home |
|---|---|
| Dashboard | **Overview** |
| Users | **People** → People lens |
| Groups | **People** → Groups lens (+ every group reachable from Access) |
| Tokens | **People** → Tokens lens |
| Data sources | **Data** → Sources lens |
| Tables | **Data** → Tables lens *and* inside each source |
| Sync | **Data** → source → Sync panel (fleet-wide run list kept in Activity) |
| Data packages | **Data** → Packages lens (now editable, not read-only) |
| Semantic layer | **Data** → source → Semantic layer panel |
| MCP sources | **Instance** → Connections |
| Linked apps | **Instance** → Connections |
| Instance secrets | **Instance** → Secrets |
| Store moderation | **Library** → Moderation |
| Flea submissions | **Library** → Moderation |
| Store lint | **Library** → Moderation |
| Studio suggestions | **Library** → Moderation |
| Marketplaces | **Library** → Sources |
| Knowledge digests | **Library** → Knowledge |
| Corporate memory | **Library** → Knowledge |
| News | **Library** → Content |
| Contribute a skill | **Library** → Content |
| Studio | **Library** → Content (same `can_studio` gate) |
| Server config | **Instance** → Configuration |
| Database backend | **Instance** → Configuration |
| Initial workspace | **Instance** → Configuration |
| Prompts | **Instance** → Configuration |
| Audit log | **Activity** → Audit |
| Telemetry | **Activity** → Telemetry |
| Analyst sessions | **Activity** → Sessions |
| Chat sessions | **Activity** → Sessions |
| Adoption | **Activity** → Adoption |
| *(API guide / Interactive API / API reference)* | Rail footer, unchanged |

Route-level note for a future implementation: every existing URL keeps
working. New surfaces are compositions over the same APIs
(`/api/admin/source-connections`, `/api/admin/registry`,
`/api/admin/data-packages`, `/api/admin/grants`, `/api/admin/access-overview`),
and retired rows become `308`s onto their new home — the pattern
`/admin/access` → group detail already uses.

### 3.6 Time-to-first-success

Today, first data to first analyst: 5 pages, 2 tokens, 4 objects, no guidance
about order, and three of the dependencies are only discoverable by failing.

Redesigned, it is one flow with the dependencies pulled forward:

```
Add data ─┬─ 1 Connect      paste URL + storage token → validate
          ├─ 2 Choose       browse buckets, tick tables
          ├─ 3 Bundle       name a package  ·  suggestion: 3 buckets → 3 packages?
          └─ 4 Share        pick groups + Optional/Automatic
                            ↓
                     "Revenue is live for 14 people."
```

The semantic-layer token is offered where it belongs — step 1's "also enable
business metrics & glossary?" — instead of being a separate page an admin finds
weeks later.

### 3.7 One flow, many connectors

Only **step 1 varies by source**. It opens with a connector picker; each
connector brings its own connect form. Steps 2–4 (choose → bundle → share) are
identical for every connector, because once tables exist, bundling and sharing
don't care where the data came from.

| Connector | Connect step | Pipeline-strip cells |
|---|---|---|
| **Keboola** | project URL + storage token; pre-checked "also sync metrics & glossary" option carrying the owner-token field | Tables · Sync · Semantic layer · Feeds |
| **BigQuery** | GCP project; credentials are the instance-level service account (managed in Instance → Secrets, linked from the form); cost guardrails stated up front (5 GiB scan / 10 GiB materialize, editable in source Settings) | Tables · Cost guard · Feeds |
| **CSV / files** | drop-zone upload; each file becomes a table; re-upload by name replaces data while keeping package membership + sharing | Tables · Freshness (last replaced) · Feeds |
| **Jira** | site URL; webhook registration instructions (URL + secret) after connect | Tables · Webhook liveness · Feeds |

The pipeline strip is therefore **declared per connector**, not fixed at four
cells — the same extension point the `extract.duckdb` contract already gives
the backend: a new connector ships its connect form and its strip cells, and
inherits choose/bundle/share for free.

**Google Workspace is deliberately not a data source.** It is the identity
provider — OAuth sign-in plus nightly group sync — so it surfaces as an
identity strip on the **People** page ("Google Workspace is the identity
provider · groups synced 4 h ago · Sync now / Settings"), where an admin
wondering "why isn't Maria in Finance yet?" is already looking. Its
credentials stay in Instance → Secrets; Google-managed groups keep their
origin marking in the Groups lens (the `source` column that already segregates
Google-synced members from admin-added ones).

### 3.8 Step 3 is a composer, step 4 absorbs the cost

Buckets are a storage grouping, not a business grouping, so a bucket-split
suggestion cannot be the only path from "selected tables" to "packages".

**Step 3 — bundling board.** The selection from step 2 lands in one pre-made
package (the least presumptuous default; an admin who doesn't care clicks
straight through and gets the old single-package behavior). From there:

- **Seeds, not modes.** "One package" / "Split by bucket" are one-click
  starting points that populate the board; the admin then edits freely.
  (Bucket-split is today's *Group tables by bucket* toolbar button, demoted to
  a seed.)
- **Free movement.** Tick tables anywhere → a move bar offers every package,
  "Leave out", and "＋ New package". Checkbox-select rather than drag — drag
  does not survive 50 tables. Package names edit inline.
- **Leave out is visible, not silent.** Parked tables go to a
  warn-styled tray and land in the standing unpackaged tray on Data →
  Packages — opting out is a recorded decision, not a leak.
- **One package per table in the wizard.** `data_package_tables` is
  many-to-many, but multi-membership stays an advanced action on the table
  drawer / package page. The wizard keeps the simple invariant.
- **Decisions aren't fatal.** Merge/split/rename is cheap afterward and the
  copy says so, so nobody stalls chasing the perfect taxonomy inside a wizard.

**Step 4 — a stack of share cards.** One card per created package, each the
*same component* as the package detail's Sharing panel (learn once). N
packages don't need a grid because two pressure valves absorb the complexity:

1. **A bulk action** ("Share all with Everyone (optional)") for the common
   one-group-gets-everything case.
2. **Skipping is safe.** An unshared package warns on its own card and is
   counted by Overview's "packages shared with nobody" gap card — so "Skip
   sharing for now" is a first-class exit that moves the work to a visible
   queue instead of hiding it. In the old world a skipped step meant silent
   breakage discovered weeks later; here it means a chip on Overview.

The success summary is honest per package: *"Sales — live for 13 people:
Finance (automatic), Analysts (automatic)"* next to *"Product — created,
shared with nobody yet. It waits on Overview."*

Scale caveat for implementation: past ~50 selected tables the board needs
bucket-level bulk moves and a filter box; the seeds carry most of the load and
hand-moves stay the exception.

---

## 4. Key flows in the mockup

| Flow | Screens |
|---|---|
| First-run setup | Overview (fresh) → Add data drawer → Overview (steady) |
| Connect a source and check its health | Data · Sources → source expanded → Sync / Semantic layer panels |
| Fix invisible data | Overview gap "6 in no package" → Data · Packages → unpackaged tray → bulk assign |
| Share a package | Data · Packages → package → Sharing → group + tier → delivery read-out |
| Grant a team access | Access → group → resources + tier, "Also grantable" disclosed |
| Answer "why can't Maria see it?" | Access · Simulate → reason chain → last pull |
| Onboard a person | People → Invite → group → their access is already correct |

---

## 5. Open questions for the next pass

1. **Does the Tables lens survive?** Everything on it is reachable from a
   source or a package. It is kept in the mockup as a flat cross-source table
   for search ("where is `orders_daily`?"), but it may be better as a search
   result than a lens.
2. **`Everyone` + auto-membership.** A grant to `Everyone` is the fastest path
   to "share with the whole company" and also the easiest accidental
   over-share. The mockup marks it with a distinct affordance; the copy needs a
   real decision.
3. **Delivery data.** "3 have not pulled since Aug 4" assumes a last-pull
   timestamp per user is queryable at admin-page latency. Needs checking
   against `sync_state` / the manifest endpoint before it is designed in.
4. *(resolved — see §3.7)* ~~Non-Keboola sources.~~ The connector picker +
   per-connector pipeline cells cover Keboola / BigQuery / CSV / Jira; Google
   Workspace is placed on People as the identity strip.
5. **Who owns fleet-wide sync?** Per-source Sync panels answer "is *this*
   project healthy". The cross-source failure list still wants one home —
   Activity, or a filter on Overview's "Needs fixing".
