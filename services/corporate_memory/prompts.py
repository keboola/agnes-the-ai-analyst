"""
Prompts for Claude HAIKU knowledge extraction.

These prompts guide the AI in curating a shared knowledge catalog
from team members' CLAUDE.local.md files, preserving existing item IDs
for vote stability and merging similar knowledge across users.
"""

CATALOG_REFRESH_PROMPT = """You are a knowledge curator managing a shared knowledge base for a team.

Your job is to produce an updated knowledge catalog by reviewing ALL team members' notes and mapping them to existing catalog items or creating new ones.

## Existing Knowledge Catalog
These items already exist. PRESERVE their IDs when the knowledge still applies.
{existing_catalog}

## Team Members' Notes
{user_files}

## Your Task
1. Review ALL team members' notes and extract valuable, reusable knowledge
2. Map extracted knowledge to existing catalog items where possible (preserve IDs!)
3. Add genuinely new items not covered by existing catalog
4. For each item, list ALL source users who mention this knowledge
5. If an existing item is no longer found in any user's notes, still keep it (someone may have removed notes but the knowledge is still valid) - preserve its existing source_users
6. Merge similar knowledge from different users into single items rather than creating duplicates

FILTERING RULES:
- EXCLUDE: API keys, tokens, passwords, credentials
- EXCLUDE: personal preferences, project-specific paths
- EXCLUDE: basic knowledge any developer would know
- EXCLUDE: incomplete or unclear notes
- EXCLUDE: anything referencing specific people negatively

For each item provide:
- existing_id: The ID from existing catalog if this maps to an existing item, or null for new items
- title: Short descriptive title (max 60 chars)
- content: Clear explanation with examples if relevant (max 1000 chars)
- category: One of [data_analysis, api_integration, debugging, performance, workflow, infrastructure, business_logic]
- tags: 2-4 relevant keywords
- source_users: Array of usernames who contributed this knowledge (for existing items with no matching user notes, preserve the original source_users)

If no valuable knowledge is found across all notes, return empty items array."""

SENSITIVITY_CHECK_PROMPT = """Review this extracted knowledge item for any sensitive information that should NOT be shared across a team.

Check for:
- API keys, tokens, passwords, secrets
- Personal information (emails, phone numbers, addresses)
- Internal URLs that should not be shared
- Credentials or authentication details
- Proprietary business information marked as confidential
- Anything that could be a security risk if shared

Knowledge item:
---
Title: {title}
Content: {content}
Tags: {tags}
---

Set safe=true if the item is safe to share, or safe=false with a reason if it contains sensitive data."""
