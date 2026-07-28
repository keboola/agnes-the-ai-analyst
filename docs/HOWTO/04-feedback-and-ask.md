# Feedback and ad-hoc telemetry questions

Two audiences, two workflows:

- **Analysts**: how to report a problem or give feedback.
- **Admins**: how to ask ad-hoc telemetry questions from the terminal.

---

## For analysts: reporting a problem

There is no `agnes feedback` command in the OSS build today. To report something:

1. **Check the GitHub issues first** — your problem may already be tracked: `https://github.com/keboola/agnes-the-ai-analyst/issues`
2. **Open a new issue** if it's not there. Include:
   - Agnes CLI version (`agnes --version`)
   - What you ran, what you expected, what actually happened
   - Relevant error output (redact any secrets or PII)
3. **Message your admin** for instance-specific issues (wrong data, missing tables, access errors). Your admin can drill into your session history via `/admin/users/<id>` → Sessions.

For urgent issues blocking your work, reach out to your admin directly — they have access to server logs and can diagnose faster than a GitHub issue.

---

## For admins: `agnes admin ask`

`agnes admin ask` translates a plain-English question into SELECT SQL, runs it read-only against the `usage_events` schema, and prints the generated SQL + results.

### Requirements

- `ANTHROPIC_API_KEY` in your environment (or server `.env`).
- Admin account on the Agnes instance.

### Usage

```bash
agnes admin ask "top 10 most-used skills last 7 days"
```

Output:
```
Generated SQL:
  SELECT skill_name, COUNT(*) AS invocations
  FROM usage_events
  WHERE event_time >= CURRENT_TIMESTAMP - INTERVAL '7 days'
    AND skill_name IS NOT NULL
  GROUP BY skill_name
  ORDER BY invocations DESC
  LIMIT 10

Results (10 rows):
skill_name                invocations
----------------------    -----------
sql-analyst               1,243
data-explorer              892
bq-cost-estimator          441
...
```

### Example questions

```bash
# Usage patterns
agnes admin ask "which users haven't run anything in 14 days"
agnes admin ask "how many sessions ran yesterday"
agnes admin ask "top tools by error rate this month"
agnes admin ask "show daily active users over the last 30 days"

# Skill / plugin analytics
agnes admin ask "how many times was the sql-analyst skill used last week"
agnes admin ask "which skills had the highest week-over-week growth"
agnes admin ask "compare skill usage between this week and last week"

# Health
agnes admin ask "what percentage of tool calls result in errors per day"
agnes admin ask "which sessions had more than 100 tool calls in the last 7 days"
```

### What's enforced server-side

- **SELECT-only**: the server parses the generated SQL and rejects any statement containing `INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, or `TRUNCATE`. This is enforced even if the LLM produces a mutating query.
- **Audit log**: every `admin ask` request — the original question, the generated SQL, and the row count — is written to `audit_log`. Visible at `/admin/activity`.
- **Model**: Claude Haiku (fast, cheap). The generated SQL is shown before results so you can verify it before acting on the numbers.

### Limitations

- The LLM maps questions to the `usage_events` schema. Highly specific or schema-crossing questions may produce incorrect SQL — always review the generated query.
- Aggregations on very large date ranges may be slow (no query timeout on ask yet).
- Does not have access to `sync_state`, `audit_log`, or other system tables — only the `usage_*` tables.
