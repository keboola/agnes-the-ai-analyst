"""Mock CRM MCP server — for POC and local dev.

A tiny in-memory MCP server simulating a CRM REST API surface as MCP tools.
Used by the Universal MCP POC (RFC #461) to test the inbound `connectors/mcp/`
connector without depending on a real CRM.

Tools:
- listAccounts(country=None, limit=50)  — bulk list, materialize candidate
- searchContacts(query, limit=20)       — parameterized lookup, passthrough candidate
- getAccount(account_id)                — point lookup, passthrough candidate

Run via stdio (the way Claude Desktop / our connector will launch it):
    .venv/bin/python scripts/dev/mock_crm_mcp_server.py

Or wire into the inbound connector's source config as:
    transport: stdio
    command: [".venv/bin/python", "scripts/dev/mock_crm_mcp_server.py"]
"""
from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "mock-crm",
    instructions=(
        "Mock CRM MCP server for Universal MCP POC. "
        "Tools: listAccounts (bulk), searchContacts (filtered lookup), getAccount (by id)."
    ),
)


# In-memory mock data ----------------------------------------------------------

_ACCOUNTS = [
    {"id": "ACC-001", "name": "Northwind Trading",  "country": "DE", "industry": "Wholesale",  "arr_usd": 124_000, "email": "ops@northwind.example",  "phone": "+49-30-1234567"},
    {"id": "ACC-002", "name": "Globex Logistics",   "country": "DE", "industry": "Logistics",  "arr_usd":  86_500, "email": "billing@globex.example", "phone": "+49-30-7654321"},
    {"id": "ACC-003", "name": "Initech Software",   "country": "US", "industry": "SaaS",       "arr_usd": 312_000, "email": "ar@initech.example",    "phone": "+1-415-5550001"},
    {"id": "ACC-004", "name": "Acme Corp",          "country": "US", "industry": "Manufacturing","arr_usd": 980_000, "email": "ar@acme.example",       "phone": "+1-212-5550100"},
    {"id": "ACC-005", "name": "Soylent Foods",      "country": "US", "industry": "FMCG",       "arr_usd": 145_000, "email": "finance@soylent.example","phone": "+1-415-5550199"},
    {"id": "ACC-006", "name": "Umbrella Pharma",    "country": "GB", "industry": "Pharma",     "arr_usd": 720_000, "email": "ar@umbrella.example",   "phone": "+44-20-71234567"},
    {"id": "ACC-007", "name": "Stark Industries",   "country": "US", "industry": "Defense",    "arr_usd":2_140_000,"email": "ap@stark.example",      "phone": "+1-212-5559999"},
    {"id": "ACC-008", "name": "Cyberdyne Systems",  "country": "US", "industry": "AI",         "arr_usd": 410_000, "email": "billing@cyberdyne.example","phone":"+1-415-5550042"},
    {"id": "ACC-009", "name": "Tyrell Corporation", "country": "US", "industry": "Biotech",    "arr_usd": 660_000, "email": "ar@tyrell.example",     "phone": "+1-213-5550200"},
    {"id": "ACC-010", "name": "Hooli Inc",          "country": "US", "industry": "Tech",       "arr_usd": 215_000, "email": "ar@hooli.example",      "phone": "+1-650-5550300"},
    {"id": "ACC-011", "name": "Pied Piper",         "country": "US", "industry": "Tech",       "arr_usd":  42_000, "email": "ar@piedpiper.example",  "phone": "+1-650-5550400"},
    {"id": "ACC-012", "name": "Krusty Krab",        "country": "US", "industry": "Restaurant", "arr_usd":   8_500, "email": "ops@krustykrab.example","phone": "+1-305-5550500"},
    {"id": "ACC-013", "name": "Wonka Industries",   "country": "GB", "industry": "FMCG",       "arr_usd": 530_000, "email": "ar@wonka.example",      "phone": "+44-161-1234567"},
    {"id": "ACC-014", "name": "Oscorp Labs",        "country": "US", "industry": "Biotech",    "arr_usd": 905_000, "email": "ap@oscorp.example",     "phone": "+1-212-5557777"},
    {"id": "ACC-015", "name": "Sterling Cooper",    "country": "US", "industry": "Advertising","arr_usd": 178_000, "email": "ar@sterlingcooper.example","phone":"+1-212-5558888"},
]

_CONTACTS = [
    {"id": "CNT-001", "account_id": "ACC-001", "name": "Anna Schmidt",   "title": "Head of Procurement", "email": "anna.schmidt@northwind.example"},
    {"id": "CNT-002", "account_id": "ACC-001", "name": "Bernd Klein",    "title": "CFO",                 "email": "bernd.klein@northwind.example"},
    {"id": "CNT-003", "account_id": "ACC-002", "name": "Clara Weber",    "title": "VP Operations",       "email": "clara.weber@globex.example"},
    {"id": "CNT-004", "account_id": "ACC-003", "name": "Peter Gibbons",  "title": "Sales Director",      "email": "peter.gibbons@initech.example"},
    {"id": "CNT-005", "account_id": "ACC-003", "name": "Joanna Price",   "title": "Account Manager",     "email": "joanna.price@initech.example"},
    {"id": "CNT-006", "account_id": "ACC-004", "name": "Wile E. Coyote", "title": "Head of R&D",         "email": "wile.coyote@acme.example"},
    {"id": "CNT-007", "account_id": "ACC-004", "name": "Road Runner",    "title": "Field Sales",         "email": "road.runner@acme.example"},
    {"id": "CNT-008", "account_id": "ACC-005", "name": "Lisa Wong",      "title": "Finance Lead",        "email": "lisa.wong@soylent.example"},
    {"id": "CNT-009", "account_id": "ACC-006", "name": "Albert Wesker",  "title": "VP Sales EMEA",       "email": "albert.wesker@umbrella.example"},
    {"id": "CNT-010", "account_id": "ACC-007", "name": "Pepper Potts",   "title": "COO",                 "email": "pepper.potts@stark.example"},
    {"id": "CNT-011", "account_id": "ACC-007", "name": "Tony Stark",     "title": "CTO",                 "email": "tony.stark@stark.example"},
    {"id": "CNT-012", "account_id": "ACC-008", "name": "Miles Dyson",    "title": "Head of AI",          "email": "miles.dyson@cyberdyne.example"},
    {"id": "CNT-013", "account_id": "ACC-009", "name": "Eldon Tyrell",   "title": "CEO",                 "email": "eldon.tyrell@tyrell.example"},
    {"id": "CNT-014", "account_id": "ACC-010", "name": "Gavin Belson",   "title": "CEO",                 "email": "gavin.belson@hooli.example"},
    {"id": "CNT-015", "account_id": "ACC-011", "name": "Richard Hendricks","title": "CEO",               "email": "richard@piedpiper.example"},
    {"id": "CNT-016", "account_id": "ACC-013", "name": "Willy Wonka",    "title": "CEO",                 "email": "willy.wonka@wonka.example"},
    {"id": "CNT-017", "account_id": "ACC-014", "name": "Norman Osborn",  "title": "CEO",                 "email": "norman.osborn@oscorp.example"},
    {"id": "CNT-018", "account_id": "ACC-015", "name": "Don Draper",     "title": "Creative Director",   "email": "don.draper@sterlingcooper.example"},
    {"id": "CNT-019", "account_id": "ACC-015", "name": "Peggy Olson",    "title": "Copy Chief",          "email": "peggy.olson@sterlingcooper.example"},
    {"id": "CNT-020", "account_id": "ACC-002", "name": "Dieter Hoffmann","title": "Procurement Lead",    "email": "dieter.hoffmann@globex.example"},
]


# Tools -----------------------------------------------------------------------

@mcp.tool()
def listAccounts(country: Optional[str] = None, limit: int = 50) -> dict:
    """List CRM accounts. Bulk-list tool — good candidate for materialize mode.

    Args:
        country: ISO 2-letter country filter (DE/US/GB). None = all.
        limit:   Max accounts to return (default 50).

    Returns:
        {"accounts": [...], "total": N}
    """
    rows = _ACCOUNTS
    if country:
        rows = [r for r in rows if r["country"].upper() == country.upper()]
    rows = rows[: max(0, limit)]
    return {"accounts": rows, "total": len(rows)}


@mcp.tool()
def searchContacts(query: str, limit: int = 20) -> dict:
    """Search CRM contacts by name (substring, case-insensitive). Passthrough candidate.

    Args:
        query: Substring to match against contact name.
        limit: Max contacts to return (default 20).

    Returns:
        {"contacts": [...], "total": N, "query": "..."}
    """
    q = (query or "").strip().lower()
    if not q:
        return {"contacts": [], "total": 0, "query": query}
    matches = [c for c in _CONTACTS if q in c["name"].lower()]
    matches = matches[: max(0, limit)]
    return {"contacts": matches, "total": len(matches), "query": query}


@mcp.tool()
def getAccount(account_id: str) -> dict:
    """Get a single account by id with its contacts. Passthrough candidate (point lookup).

    Args:
        account_id: Account id like ACC-001.

    Returns:
        {"account": {...}, "contacts": [...]} or {"error": "not found"}.
    """
    acc = next((a for a in _ACCOUNTS if a["id"] == account_id), None)
    if acc is None:
        return {"error": f"account not found: {account_id}"}
    contacts = [c for c in _CONTACTS if c["account_id"] == account_id]
    return {"account": acc, "contacts": contacts}


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
