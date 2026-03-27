# Corporate Memory — Knowledge sharing and governance

## What It Is
Corporate memory collects knowledge from all analysts' CLAUDE.local.md files
and makes it available to everyone through a curated catalog.

## How It Works
1. Analysts write insights in their CLAUDE.local.md
2. `da sync --upload-only` pushes content to server
3. Server processes with LLM (Haiku) to extract knowledge items
4. Items go through governance (pending → approved/mandatory)
5. Approved items are distributed as Claude rules

## Governance Flow
- **pending**: New item, awaiting review
- **approved**: Available to all users
- **mandatory**: Force-pushed to all users' rules
- **rejected**: Not distributed

## Admin Commands
```bash
# View pending items (via web UI or API)
da query "SELECT id, title, status FROM system.knowledge_items WHERE status='pending'" --remote

# Approve/reject via API
curl -X PUT http://server:8000/api/memory/<id>/status?new_status=approved -H "Authorization: Bearer $TOKEN"
```

## Voting
Users can upvote/downvote knowledge items to surface the most useful ones.
