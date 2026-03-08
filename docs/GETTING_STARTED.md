# Getting Started with Internal AI Data Analyst

Quick start guide for analysts who want to explore company data using AI.

## What is This?

**Internal AI Data Analyst** gives you local access to your organization's data (sales, HR, finance, telemetry) so you can analyze it using Claude Code with natural language questions.

Instead of writing SQL queries manually, you can ask Claude questions like:
- "Which companies have the highest revenue?"
- "Show me employee headcount trends over the last year"
- "Compare actual PPU usage vs contract limits for this month"

## Prerequisites

- An account on your organization's Data Analyst instance
- Claude Code installed locally ([claude.ai/code](https://claude.ai/code))
- That's it! Claude handles the rest.

## First Time Setup (5 minutes)

1. **Visit the setup page**: `https://your-instance-url`
2. **Sign in** with your organization account
3. **Click "Copy Setup Instructions"** - your username is pre-filled
4. **Open Claude Code** in a new folder (e.g., `~/data-analysis`)
5. **Paste the instructions** into Claude Code
6. **Let Claude do the setup** - it will:
   - Generate SSH keys
   - Create your server account
   - Download ~690 MB of data
   - Set up DuckDB database
   - Install Python dependencies

That's it! Claude handles everything automatically.

## How to Use It

### Starting a New Session

Every time you open Claude Code in your project folder:

1. Claude will automatically detect the project (via `CLAUDE.md`)
2. **Always check data freshness first** - ask Claude: "Is my data fresh?"
3. If stale, ask: "Sync the latest data"
4. Start asking questions!

### Example Questions to Ask Claude

**Sales & Revenue Analysis:**
- "What are our top 10 customers by total contract value?"
- "Show me new opportunities created this month"
- "Which products generate the most revenue?"

**HR & Headcount:**
- "How many employees do we have by department?"
- "Show me headcount growth over the last 6 months"
- "Who are the top salespeople by closed deals?"

**Platform Usage & Telemetry:**
- "Which projects are using the most PPU credits?"
- "Compare actual usage vs limits for our biggest customers"
- "Show me PAYG payment trends"

**Finance:**
- "What's our MRR trend over the last year?"
- "Show me budget vs actuals for Q4"
- "Compare revenue by product line"

**Cross-Domain Analysis:**
- "Which account owners have the highest win rates?"
- "Link organizations to their CRM accounts"
- "Show employee owners and their total pipeline value"

### What Claude Can Do

Claude Code can:
- ✅ Write and run SQL queries on your DuckDB database
- ✅ Create visualizations and charts
- ✅ Analyze trends and patterns
- ✅ Join data across domains (sales, HR, finance, telemetry)
- ✅ Export results to CSV or other formats
- ✅ Keep your data fresh by syncing from the server

### What Data is Available?

Your local database contains:

| Domain | Tables | Examples |
|--------|--------|----------|
| **Sales & CRM** | 14 tables | Companies, contact, opportunities, contracts, products, activities, usage limits, MRR |
| **HR** | 2 tables | Employees, historical snapshots |
| **Finance** | 5 tables | P&L KPIs, budgets, actuals, exchange rates, infrastructure cost |
| **Telemetry** | 4 tables | Organizations, projects, usage metrics, payments |

**Total: 25 tables with full relationships documented**

For detailed schemas, ask Claude: "Show me the table relationships" or check `docs/data_description.md`.

## Tips for Better Analysis

1. **Always check data freshness** - stale data = wrong conclusions
   - Ask: "Is my data fresh?" or "When was data last synced?"

2. **Be specific with questions**
   - ❌ "Show me sales data"
   - ✅ "Show me top 10 companies by contract value in 2024"

3. **Ask Claude to explain queries**
   - "Explain this query in plain English"
   - "Why did you join these tables this way?"

4. **Iterate on results**
   - "Now group by month" or "Add a filter for Europe only"

5. **Export when ready**
   - "Export this to CSV"
   - "Create a chart of this trend"

## Keeping Data Fresh

Your local data syncs from the server. Always work with fresh data:

**Sync latest data:**
- Ask Claude: "Sync latest data"
- Or run: `bash scripts/sync_data.sh`

**How often?** Data is refreshed on the server every few hours. Sync daily or before important analysis.

## Reporting Issues

If you encounter problems:

### Option 1: GitHub Issue (Preferred)

If you have access to the project's GitHub repository:
1. Go to the repository's Issues page
2. Click "New Issue"
3. Describe the problem with:
   - What you were trying to do
   - Error message or unexpected behavior
   - Steps to reproduce

### Option 2: Internal Issue Tracker

If your organization uses an internal issue tracker (Linear, Jira, etc.):
1. Create a new issue in the appropriate project
2. Describe the problem
3. The platform team will triage and handle it.

### What to Include

When reporting issues:
- ✅ Error messages (copy the full text)
- ✅ What you were trying to do
- ✅ Output of `bash server/scripts/sync_data.sh`
- ✅ Claude Code version (if relevant)

## Technical Details (For the Curious)

If you want to understand what's under the hood:

**Architecture:**
- Data syncs from the configured data source to the server
- Your local setup downloads Parquet files via rsync
- DuckDB creates views over Parquet files (no data duplication)
- Claude Code queries DuckDB using SQL

**File Structure:**
```
~/data-analysis/
├── CLAUDE.md              # Claude Code project context (auto-updated on sync)
├── CLAUDE.local.md        # Your personal customizations (never overwritten)
├── .claude/
│   └── settings.json      # Project permissions (synced from server)
├── server/                # Read-only data from server
│   ├── parquet/           # Data files (~690 MB)
│   ├── docs/              # Documentation
│   ├── scripts/           # Helper scripts
│   ├── examples/          # Example scripts
│   └── metadata/          # Sync state, table metadata
├── user/                  # Your workspace (writable)
│   ├── duckdb/            # DuckDB database
│   ├── notifications/     # Your notification scripts
│   ├── artifacts/         # Analysis outputs
│   └── scripts/           # Your custom scripts
└── .venv/                 # Python environment
```

**Python Environment:**
- Virtual environment with: pandas, duckdb, pyarrow
- Scripts auto-activate the venv
- Claude Code manages this automatically

For complete technical documentation, see:
- `docs/data_description.md` - Table schemas and relationships
- `CLAUDE.md` - Project context for Claude Code
- `../dev_docs/server.md` - Server architecture (for developers)

## FAQ

**Q: Do I need to know SQL?**
A: No! Ask Claude in natural language. It writes the SQL for you.

**Q: Can I break anything?**
A: No. You're only reading local data. The server and data source are read-only.

**Q: How much disk space do I need?**
A: ~2 GB (data + database + Python dependencies)

**Q: What if my data gets out of sync?**
A: Just ask Claude to sync: "Sync latest data from server"

**Q: What if DuckDB gets corrupted?**
A: The sync script automatically detects corrupted DuckDB files and recreates them from parquet files. This can happen if sync is interrupted or the file is only partially transferred. All data is safe in parquet files - DuckDB only contains VIEW definitions that point to parquets.

**Q: Can I use this without Claude Code?**
A: Yes, but you'd need to write SQL manually. Claude Code makes it much easier.

**Q: Is this data secure?**
A: Yes. Data is synced via SSH (requires authentication). Only approved users with accounts can access it.

## Next Steps

1. **Complete the setup** (follow instructions at your instance URL)
2. **Ask Claude a simple question** to test: "How many companies are in the database?"
3. **Explore the data** - ask Claude: "What tables are available?"
4. **Start analyzing!** - ask real business questions

Need help? Contact your platform team or create an issue as described above.

Happy analyzing!
