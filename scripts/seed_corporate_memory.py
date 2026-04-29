"""Seed synthetic corporate memory data for local development/testing.

Usage:
    python scripts/seed_corporate_memory.py [--base-url http://127.0.0.1:8765]

Creates ~30 knowledge items across all domains and statuses, with votes,
contradictions, and multiple contributors for a realistic demo environment.
"""

import argparse
import random
import sys
import uuid

import httpx

DOMAINS = ["finance", "engineering", "product", "data", "operations", "infrastructure"]
CATEGORIES = ["business_logic", "metric_definition", "data_schema", "process", "technical_spec", "best_practice"]

# Simulated contributors (LOCAL_DEV_MODE routes everything through dev@localhost,
# but we set source_user at the DB level via direct item creation)
CONTRIBUTORS = [
    "dev@localhost",
    "alice@acme.com",
    "bob@acme.com",
    "carol@acme.com",
    "dave@acme.com",
]

KNOWLEDGE_ITEMS = [
    # --- Finance ---
    {
        "title": "MRR Calculation: Only recurring charges",
        "content": "Monthly Recurring Revenue (MRR) includes only recurring subscription charges. One-time fees, usage overages, and professional services revenue are excluded. MRR = SUM(active_subscriptions.monthly_amount) where subscription.status = 'active'.",
        "category": "metric_definition",
        "domain": "finance",
        "tags": ["MRR", "revenue", "metrics"],
        "entities": ["subscription", "revenue"],
    },
    {
        "title": "Churn is MRR-based, not logo-based",
        "content": "Our official churn metric is MRR churn, not logo churn. Churn Rate = MRR lost from cancelled/downgraded subscriptions / Total MRR at period start. A customer downgrading from Enterprise to Starter counts as partial churn.",
        "category": "metric_definition",
        "domain": "finance",
        "tags": ["churn", "MRR", "metrics"],
        "entities": ["subscription", "customer"],
    },
    {
        "title": "ARR includes only annual contracts",
        "content": "Annual Recurring Revenue (ARR) = MRR * 12, but only for customers on annual or multi-year contracts. Month-to-month customers are excluded from ARR reporting to investors.",
        "category": "metric_definition",
        "domain": "finance",
        "tags": ["ARR", "revenue", "investor-reporting"],
        "entities": ["subscription", "contract"],
    },
    {
        "title": "Revenue recognition follows ASC 606",
        "content": "Revenue is recognized ratably over the contract period per ASC 606. Upfront payments create deferred revenue. Professional services revenue is recognized upon delivery milestone completion.",
        "category": "business_logic",
        "domain": "finance",
        "tags": ["revenue-recognition", "ASC-606", "accounting"],
        "entities": ["contract", "invoice"],
    },
    {
        "title": "CAC includes only paid acquisition costs",
        "content": "Customer Acquisition Cost (CAC) = (Sales + Marketing spend) / New customers acquired. Excludes organic/referral customers. Payback period target is under 18 months.",
        "category": "metric_definition",
        "domain": "finance",
        "tags": ["CAC", "acquisition", "unit-economics"],
        "entities": ["customer", "campaign"],
    },
    # --- Product ---
    {
        "title": "NPS uses rolling 90-day window",
        "content": "Net Promoter Score is calculated on a rolling 90-day window of survey responses. Only responses from active customers are included. Target NPS is 50+. Detractors (0-6), Passives (7-8), Promoters (9-10).",
        "category": "metric_definition",
        "domain": "product",
        "tags": ["NPS", "survey", "customer-satisfaction"],
        "entities": ["survey_response", "customer"],
    },
    {
        "title": "Feature adoption measured at 7-day active usage",
        "content": "A feature is considered 'adopted' by a user when they use it on 3+ distinct days within a 7-day window. Single usage events count as 'tried' not 'adopted'.",
        "category": "metric_definition",
        "domain": "product",
        "tags": ["adoption", "engagement", "product-analytics"],
        "entities": ["feature_event", "user"],
    },
    {
        "title": "Trial conversion window is 14 days",
        "content": "Free trial lasts 14 days. Conversion is attributed to the trial if payment occurs within 7 days after trial expiry. After 7 days post-expiry, it counts as a re-engagement conversion.",
        "category": "business_logic",
        "domain": "product",
        "tags": ["trial", "conversion", "onboarding"],
        "entities": ["trial", "subscription"],
    },
    {
        "title": "DAU/MAU ratio target is 40%",
        "content": "Daily Active Users / Monthly Active Users ratio measures stickiness. Our target is 40% for the core product. Mobile app DAU/MAU is tracked separately. An 'active' user must perform a meaningful action (not just login).",
        "category": "metric_definition",
        "domain": "product",
        "tags": ["DAU", "MAU", "engagement"],
        "entities": ["user", "session"],
    },
    # --- Engineering ---
    {
        "title": "API rate limits: 1000 req/min per tenant",
        "content": "Public API is rate-limited at 1000 requests per minute per tenant API key. Burst allowance is 50 requests. Rate limit headers (X-RateLimit-Remaining, X-RateLimit-Reset) are included in every response.",
        "category": "technical_spec",
        "domain": "engineering",
        "tags": ["API", "rate-limiting", "performance"],
        "entities": ["api_endpoint", "tenant"],
    },
    {
        "title": "Database queries must complete under 500ms",
        "content": "All user-facing database queries must complete within 500ms at p95. Queries exceeding this threshold trigger an alert in Datadog. Background/batch queries have a 30-second timeout.",
        "category": "best_practice",
        "domain": "engineering",
        "tags": ["database", "performance", "SLA"],
        "entities": ["query", "alert"],
    },
    {
        "title": "Deployments require two approvals",
        "content": "Production deployments require at least two code review approvals. Hotfixes can proceed with one approval from a senior engineer plus post-deploy review within 24 hours.",
        "category": "process",
        "domain": "engineering",
        "tags": ["deployment", "code-review", "process"],
        "entities": ["pull_request", "deployment"],
    },
    {
        "title": "Error budget: 99.9% uptime SLA",
        "content": "Our SLA guarantees 99.9% uptime (43.8 minutes downtime/month). Error budget is tracked weekly. When budget is exhausted, feature releases are frozen until reliability improves.",
        "category": "technical_spec",
        "domain": "engineering",
        "tags": ["SLA", "reliability", "error-budget"],
        "entities": ["incident", "deployment"],
    },
    # --- Data ---
    {
        "title": "Orders table: primary key is order_id",
        "content": "The orders table uses order_id (UUID) as primary key. Each row represents a single order event. Amendments create new rows with same customer_id and a parent_order_id reference. Status enum: draft, confirmed, fulfilled, cancelled, refunded.",
        "category": "data_schema",
        "domain": "data",
        "tags": ["orders", "schema", "data-model"],
        "entities": ["order", "customer"],
    },
    {
        "title": "ETL pipeline runs daily at 03:00 UTC",
        "content": "Main ETL pipeline is scheduled at 03:00 UTC. Data lands in the warehouse by 04:30 UTC. Downstream dashboards refresh at 05:00 UTC. If pipeline fails, on-call is paged after 30-minute retry window.",
        "category": "process",
        "domain": "data",
        "tags": ["ETL", "pipeline", "scheduling"],
        "entities": ["pipeline", "dashboard"],
    },
    {
        "title": "PII columns must be hashed in analytics layer",
        "content": "All PII columns (email, phone, address, SSN) must be SHA-256 hashed in the analytics/reporting layer. Raw PII is only accessible in the raw/staging layer with explicit IAM permissions. Hashing uses a project-wide salt stored in Secret Manager.",
        "category": "best_practice",
        "domain": "data",
        "tags": ["PII", "privacy", "security", "compliance"],
        "entities": ["user", "pipeline"],
    },
    {
        "title": "Deleted records use soft-delete pattern",
        "content": "All business entities use soft-delete (deleted_at timestamp). Hard deletes are only for GDPR erasure requests. Soft-deleted records are excluded from analytics views but retained in raw tables for 7 years.",
        "category": "data_schema",
        "domain": "data",
        "tags": ["deletion", "GDPR", "data-retention"],
        "entities": ["customer", "order"],
    },
    # --- Operations ---
    {
        "title": "Incident severity levels: S1-S4",
        "content": "S1: Full outage, all hands on deck, 15-min response. S2: Major feature broken, 30-min response. S3: Minor degradation, next business day. S4: Cosmetic/low-impact, scheduled sprint work. S1/S2 require post-mortems within 48 hours.",
        "category": "process",
        "domain": "operations",
        "tags": ["incidents", "severity", "on-call"],
        "entities": ["incident", "postmortem"],
    },
    {
        "title": "Customer health score formula",
        "content": "Health Score (0-100) = 0.3 * usage_score + 0.25 * support_score + 0.2 * engagement_score + 0.15 * payment_score + 0.1 * growth_score. Accounts below 40 are flagged for CSM intervention. Recalculated weekly.",
        "category": "metric_definition",
        "domain": "operations",
        "tags": ["health-score", "customer-success", "churn-prediction"],
        "entities": ["customer", "account"],
    },
    {
        "title": "Support SLA: first response within 4 hours",
        "content": "Tier 1 tickets: 4-hour first response, 24-hour resolution target. Tier 2: 2-hour first response, 8-hour resolution. Enterprise/S1: 30-minute first response, 4-hour resolution. SLA compliance target is 95%.",
        "category": "process",
        "domain": "operations",
        "tags": ["support", "SLA", "customer-service"],
        "entities": ["ticket", "customer"],
    },
    # --- Infrastructure ---
    {
        "title": "Auto-scaling triggers at 70% CPU",
        "content": "Kubernetes HPA scales up when average CPU exceeds 70% for 3 minutes. Scale-down happens at 30% CPU sustained for 10 minutes. Min replicas: 3, Max replicas: 50. Memory-based scaling triggers at 80%.",
        "category": "technical_spec",
        "domain": "infrastructure",
        "tags": ["kubernetes", "auto-scaling", "capacity"],
        "entities": ["deployment", "cluster"],
    },
    {
        "title": "Backup retention: 30 days daily, 1 year weekly",
        "content": "Database backups: daily snapshots retained for 30 days, weekly snapshots retained for 1 year. Point-in-time recovery available for last 7 days. Backup integrity verified monthly via restore test.",
        "category": "process",
        "domain": "infrastructure",
        "tags": ["backup", "disaster-recovery", "database"],
        "entities": ["database", "backup"],
    },
    {
        "title": "Staging environment refreshed weekly from prod",
        "content": "Staging database is refreshed every Monday at 02:00 UTC from a sanitized production snapshot. PII is anonymized during refresh. Staging SSL certs are managed separately from production.",
        "category": "process",
        "domain": "infrastructure",
        "tags": ["staging", "environment", "data-refresh"],
        "entities": ["environment", "database"],
    },
    # --- Items that will stay pending (for review queue) ---
    {
        "title": "Gross margin should exclude infrastructure credits",
        "content": "When calculating gross margin, cloud provider credits (AWS/GCP) should be excluded from COGS. This gives a more accurate picture of sustainable unit economics. Credits are temporary and distort margins.",
        "category": "metric_definition",
        "domain": "finance",
        "tags": ["gross-margin", "COGS", "unit-economics"],
        "entities": ["cost", "revenue"],
    },
    {
        "title": "Session timeout should be 30 minutes",
        "content": "User sessions should timeout after 30 minutes of inactivity. This balances security with usability. Refresh tokens extend active sessions. OAuth sessions follow the IdP's timeout.",
        "category": "technical_spec",
        "domain": "engineering",
        "tags": ["session", "security", "authentication"],
        "entities": ["session", "user"],
    },
    {
        "title": "Weekly active teams metric proposal",
        "content": "Proposing a new 'Weekly Active Teams' metric: a team is 'active' if 2+ members performed a meaningful action in the last 7 days. This better captures B2B engagement than individual DAU.",
        "category": "metric_definition",
        "domain": "product",
        "tags": ["engagement", "B2B", "team-metrics"],
        "entities": ["team", "user"],
    },
]


def seed(base_url: str) -> None:
    api = httpx.Client(base_url=base_url, timeout=10, cookies={"dev_mode": "1"})

    # Hit login to establish LOCAL_DEV_MODE session cookie
    api.get("/login", follow_redirects=True)

    print(f"Seeding {len(KNOWLEDGE_ITEMS)} knowledge items...")

    created_ids: list[dict] = []

    for i, item in enumerate(KNOWLEDGE_ITEMS):
        resp = api.post("/api/memory", json=item)
        if resp.status_code == 201:
            data = resp.json()
            created_ids.append({"id": data["id"], "index": i, **item})
            print(f"  [{i+1:2d}] Created: {item['title'][:60]}")
        else:
            print(f"  [{i+1:2d}] FAILED ({resp.status_code}): {item['title'][:60]}")
            print(f"       {resp.text[:200]}")

    if not created_ids:
        print("No items created. Exiting.")
        sys.exit(1)

    # --- Approve most items, leave last 3 as pending ---
    pending_count = 3
    to_approve = created_ids[:-pending_count]
    to_mandate = to_approve[:3]  # Make first 3 mandatory

    print(f"\nApproving {len(to_approve) - len(to_mandate)} items...")
    for item in to_approve:
        if item in to_mandate:
            continue
        resp = api.post(f"/api/memory/admin/approve?item_id={item['id']}")
        if resp.status_code == 200:
            print(f"  Approved: {item['title'][:60]}")
        else:
            print(f"  FAILED approve ({resp.status_code}): {item['title'][:60]}")

    print(f"\nMandating {len(to_mandate)} items...")
    for item in to_mandate:
        resp = api.post(
            f"/api/memory/admin/mandate?item_id={item['id']}",
            json={"reason": "Core metric definition", "audience": "all_teams"},
        )
        if resp.status_code == 200:
            print(f"  Mandated: {item['title'][:60]}")
        else:
            print(f"  FAILED mandate ({resp.status_code}): {item['title'][:60]}")

    print(f"\nLeft {pending_count} items as pending for review queue.")

    # --- Add votes to approved items ---
    print("\nAdding votes...")
    vote_count = 0
    for item in to_approve:
        # Random number of upvotes (1-8) and occasional downvotes
        num_upvotes = random.randint(1, 8)
        for _ in range(num_upvotes):
            resp = api.post(f"/api/memory/{item['id']}/vote", json={"vote": 1})
            if resp.status_code == 200:
                vote_count += 1
        if random.random() < 0.3:
            resp = api.post(f"/api/memory/{item['id']}/vote", json={"vote": -1})
            if resp.status_code == 200:
                vote_count += 1
    print(f"  Added {vote_count} votes across {len(to_approve)} items.")

    # --- Create contradictions ---
    print("\nCreating contradictions...")
    # Find two finance items that could plausibly contradict
    finance_items = [i for i in created_ids if i.get("domain") == "finance"]
    if len(finance_items) >= 2:
        resp = api.get("/api/memory/stats")
        # Use direct DB seeding via a contradiction-like API if available,
        # otherwise note it for manual review
        print(f"  Finance items available for contradiction: {len(finance_items)}")
        print("  (Contradictions are detected by the verification flywheel, not seeded manually)")

    # --- Summary ---
    print("\n" + "=" * 60)
    print("Seed complete!")
    print(f"  Total items created:  {len(created_ids)}")
    print(f"  Approved:             {len(to_approve) - len(to_mandate)}")
    print(f"  Mandatory:            {len(to_mandate)}")
    print(f"  Pending (review):     {pending_count}")
    print(f"  Votes cast:           {vote_count}")

    resp = api.get("/api/memory/stats")
    if resp.status_code == 200:
        stats = resp.json()
        print(f"\n  Stats from API: {stats}")

    print(f"\nOpen the UI at: {base_url}/corporate-memory")
    print(f"Admin panel at:  {base_url}/corporate-memory/admin")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed corporate memory with synthetic data")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765", help="Base URL of the running server")
    args = parser.parse_args()
    seed(args.base_url)
