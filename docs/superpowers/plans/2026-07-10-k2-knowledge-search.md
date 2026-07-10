# K2: knowledge_search — one query across documents + knowledge base + table catalog

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One query fans out over Collections chunks (hybrid), knowledge_items (FTS), and table_registry catalog cards, returning a single ranked, typed, cited result list via REST + MCP + CLI.

**Architecture:** New thin module `src/search/unified.py` that calls the three existing engines and merges results (per-source min-max normalization, interleave by score, deterministic tie-break). RBAC stays the caller's job: the new REST endpoint resolves the caller's grant sets fail-closed (collections grants, memory-domain grants + groups, `can_access_table`) and passes them in. No engine is modified. Slice K2 of `docs/superpowers/specs/2026-07-10-unified-knowledge-design.md` (#797).

**Tech Stack:** Python/FastAPI, existing `src/ingest/retrieval.py`, `src/repositories/knowledge.py` (+`_pg`), `table_registry`, FastMCP stdio server, Typer CLI.

## Global Constraints

- No new repository methods → no parity work; engines used via factories only.
- New REST endpoint ⇒ CLI + MCP coverage in the same PR (API triple-surface ratchet: `tests/test_documentation_api_triple_surface.py` must pass without adding to `tests/api_triple_surface_grandfathered.txt`).
- Fail-closed per source: empty grant set for a source ⇒ that source contributes nothing.
- Vendor-agnostic; CHANGELOG bullet; no AI attribution; full suite before push.

---

### Task 1: `src/search/unified.py` — fan-out + merge

**Files:**
- Create: `src/search/__init__.py` (empty), `src/search/unified.py`
- Test: `tests/test_search_unified.py`

**Interfaces:**
- Produces:

```python
def unified_search(
    query: str,
    *,
    corpus_ids: List[str],
    user_groups: List[str],
    granted_domains: List[str],
    tables: List[Dict[str, Any]],   # pre-RBAC-filtered table_registry rows
    k: int = 10,
) -> List[Dict[str, Any]]
```

Each hit is one of:
- `{"type": "chunk", "score": float, "chunk_id", "corpus_id", "file_id", "filename", "ordinal", "section_path", "text", "confidence"}` (fields passed through from `retrieval.search`)
- `{"type": "knowledge", "score": float, "id", "title", "snippet", "domain"}`
- `{"type": "table", "score": float, "table_id", "name", "description", "pivot_hint"}` where `pivot_hint = f"structured data — query with SQL via `agnes query`, table id: {table_id}"`

- [ ] **Step 1: Failing tests** (`tests/test_search_unified.py`)

```python
"""unified_search — fan-out over chunks + knowledge + catalog cards (K2)."""

from unittest.mock import patch

TABLES = [
    {"id": "t_orders", "name": "orders", "description": "customer orders and revenue", "columns_json": None},
    {"id": "t_web", "name": "web_sessions", "description": "web analytics sessions", "columns_json": None},
]


def _fake_chunks(corpus_ids, query, k=10):
    if not corpus_ids:
        return []
    return [{"chunk_id": "ch1", "corpus_id": "c1", "file_id": "f1", "filename": "billing.md",
             "ordinal": 0, "section_path": None, "text": "invoices are monthly", "score": 0.9,
             "confidence": "high"}]


def _fake_knowledge(query, **kw):
    if not kw.get("granted_domains") and not kw.get("user_groups"):
        return []
    return [{"id": "ki1", "title": "Billing policy", "content": "We invoice monthly in EUR.",
             "domain": "finance"}]


def test_merges_all_three_sources():
    from src.search.unified import unified_search

    with patch("src.search.unified._chunk_search", _fake_chunks), \
         patch("src.search.unified._knowledge_search", _fake_knowledge):
        hits = unified_search("invoices orders", corpus_ids=["c1"], user_groups=["g1"],
                              granted_domains=["d1"], tables=TABLES, k=10)
    types = {h["type"] for h in hits}
    assert types == {"chunk", "knowledge", "table"}
    table_hit = next(h for h in hits if h["type"] == "table")
    assert table_hit["table_id"] == "t_orders"
    assert "agnes query" in table_hit["pivot_hint"]


def test_fail_closed_per_source():
    from src.search.unified import unified_search

    with patch("src.search.unified._chunk_search", _fake_chunks), \
         patch("src.search.unified._knowledge_search", _fake_knowledge):
        hits = unified_search("invoices", corpus_ids=[], user_groups=[],
                              granted_domains=[], tables=[], k=10)
    assert hits == []


def test_blank_query_returns_empty():
    from src.search.unified import unified_search

    assert unified_search("  ", corpus_ids=["c1"], user_groups=["g"],
                          granted_domains=["d"], tables=TABLES) == []


def test_k_caps_results_and_order_deterministic():
    from src.search.unified import unified_search

    with patch("src.search.unified._chunk_search", _fake_chunks), \
         patch("src.search.unified._knowledge_search", _fake_knowledge):
        a = unified_search("invoices orders", corpus_ids=["c1"], user_groups=["g"],
                           granted_domains=["d"], tables=TABLES, k=2)
        b = unified_search("invoices orders", corpus_ids=["c1"], user_groups=["g"],
                           granted_domains=["d"], tables=TABLES, k=2)
    assert len(a) == 2
    assert a == b


def test_table_scoring_prefers_term_overlap():
    from src.search.unified import _table_scores

    scored = _table_scores("customer orders revenue", TABLES)
    assert scored[0]["table_id"] == "t_orders"
    assert scored[0]["score"] > 0
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_search_unified.py -q` → FAIL (module missing)

- [ ] **Step 3: Implement** `src/search/unified.py`:

```python
"""Unified knowledge search (K2, #797) — one query across three engines.

Thin fan-out over the EXISTING search surfaces; none is modified here:

1. Collections chunks — ``src.ingest.retrieval.search`` (hybrid lexical+vector)
2. Knowledge base — ``knowledge_repo().search`` (FTS/BM25 or ILIKE fallback)
3. Table catalog cards — lexical term overlap over ``table_registry`` rows;
   a table hit returns a *pivot hint* (query it with SQL), never rows.

RBAC is the caller's responsibility: pass only granted ``corpus_ids`` /
``user_groups`` / ``granted_domains`` / pre-filtered ``tables``. An empty
grant set for a source contributes nothing (fail-closed), never "search all".

Merging: scores are min-max normalized WITHIN each source (the engines'
score scales are incomparable), then interleaved by normalized score with a
deterministic tie-break (type, then id) so equal-score runs are stable.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _TOKEN.findall((text or "").lower())


def _chunk_search(corpus_ids: List[str], query: str, k: int = 10) -> List[Dict[str, Any]]:
    from src.ingest.retrieval import search

    return search(corpus_ids, query, k=k)


def _knowledge_search(query: str, **kw: Any) -> List[Dict[str, Any]]:
    from src.repositories import knowledge_repo

    return knowledge_repo().search(query, **kw)


def _minmax(scores: List[float]) -> List[float]:
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi <= lo:
        return [1.0] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


def _table_scores(query: str, tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lexical term-overlap scoring over name + description + column names."""
    q_terms = set(_tokenize(query))
    if not q_terms:
        return []
    out: List[Dict[str, Any]] = []
    for t in tables:
        cols = ""
        cj = t.get("columns_json")
        if cj:
            try:
                cols = " ".join(str(c.get("name", "")) for c in json.loads(cj))
            except (ValueError, TypeError, AttributeError):
                cols = ""
        hay = set(_tokenize(f"{t.get('name', '')} {t.get('description', '')} {cols}"))
        overlap = len(q_terms & hay)
        if overlap == 0:
            continue
        table_id = t.get("id")
        out.append(
            {
                "type": "table",
                "table_id": table_id,
                "name": t.get("name"),
                "description": t.get("description"),
                "score": overlap / len(q_terms),
                "pivot_hint": (
                    f"structured data — query with SQL via `agnes query`, table id: {table_id}"
                ),
            }
        )
    out.sort(key=lambda h: (-h["score"], h["table_id"] or ""))
    return out


def unified_search(
    query: str,
    *,
    corpus_ids: List[str],
    user_groups: List[str],
    granted_domains: List[str],
    tables: List[Dict[str, Any]],
    k: int = 10,
) -> List[Dict[str, Any]]:
    """Fan the query out over all three sources and return one ranked list."""
    if not (query or "").strip():
        return []

    buckets: List[List[Dict[str, Any]]] = []

    chunk_hits = [dict(h, type="chunk") for h in _chunk_search(corpus_ids, query, k=k)] if corpus_ids else []
    buckets.append(chunk_hits)

    knowledge_hits: List[Dict[str, Any]] = []
    if user_groups or granted_domains:
        for rank, item in enumerate(
            _knowledge_search(
                query,
                exclude_personal=True,
                user_groups=user_groups,
                granted_domains=granted_domains,
                limit=k,
            )
        ):
            content = item.get("content") or ""
            knowledge_hits.append(
                {
                    "type": "knowledge",
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "snippet": content[:280],
                    "domain": item.get("domain"),
                    # BM25 rank order only (the repo returns no score in the
                    # ILIKE fallback) — decay by position, top hit = 1.0.
                    "score": 1.0 / (1 + rank),
                }
            )
    buckets.append(knowledge_hits)
    buckets.append(_table_scores(query, tables)[:k] if tables else [])

    merged: List[Dict[str, Any]] = []
    for bucket in buckets:
        norms = _minmax([h["score"] for h in bucket])
        for h, n in zip(bucket, norms):
            merged.append({**h, "score": round(n, 4)})

    merged.sort(
        key=lambda h: (
            -h["score"],
            h["type"],
            str(h.get("chunk_id") or h.get("id") or h.get("table_id") or ""),
        )
    )
    return merged[:k]
```

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_search_unified.py -q` → PASS
- [ ] **Step 5: Commit** `feat(search): unified fan-out over chunks, knowledge, catalog cards`

---

### Task 2: REST `GET /api/knowledge/search`

**Files:**
- Create: `app/api/knowledge_search.py`
- Modify: `app/main.py` (import + `include_router`)
- Test: `tests/test_api_knowledge_search.py`

**Interfaces:**
- Consumes: `unified_search` (Task 1); `_accessible_corpus_ids` (from `app.api.collections`), `_caller_granted_memory_domains` + `_effective_groups` (from `app.api.memory`), `can_access_table` (from `src.rbac`), `table_registry_repo`.
- Produces: `GET /api/knowledge/search?q=<query>&k=<int>` → `{"query": str, "results": [hit, ...]}`, auth required (`get_current_user`), RBAC fail-closed.

- [ ] **Step 1: Failing test** — mirror the fixture idiom of `tests/test_api_collections.py` (`seeded_app`, `_auth`, `_seed_collection_grant`): admin uploads a doc into a granted collection (reuse the upload flow), a knowledge item + a registered table exist from the fixture where available; assert (a) 401 unauthenticated, (b) 200 shape `{"query", "results"}`, (c) analyst without any grants gets `results` not containing chunks from ungranted collections. Keep assertions structural (types/keys) rather than ranking-sensitive.
- [ ] **Step 2: Run** → FAIL (404 route)
- [ ] **Step 3: Implement** `app/api/knowledge_search.py`:

```python
"""Unified knowledge search endpoint (K2, #797) — REST surface for
``src.search.unified.unified_search``. Resolves the caller's grant sets
fail-closed and fans out; see the module docstring there for semantics."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import get_current_user
from src.db import get_system_db
from src.rbac import can_access_table
from src.repositories import table_registry_repo
from src.search.unified import unified_search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("/search")
async def knowledge_search(
    q: str = Query(..., min_length=1, description="Search query"),
    k: int = Query(10, ge=1, le=50),
    user=Depends(get_current_user),
):
    """One query across documents, the knowledge base, and the table catalog.

    Results are typed (``chunk | knowledge | table``); table hits carry a
    pivot hint (query via SQL) instead of rows. Everything is filtered to the
    caller's grants, fail-closed per source.
    """
    from app.api.collections import _accessible_corpus_ids
    from app.api.memory import _caller_granted_memory_domains, _effective_groups

    corpus_ids = _accessible_corpus_ids(user)
    conn = get_system_db()
    try:
        groups = _effective_groups(user, conn)
        domains = _caller_granted_memory_domains(user, conn)
        tables = [t for t in table_registry_repo().list_all() if can_access_table(user, t["id"], conn)]
    finally:
        conn.close()

    results = unified_search(
        q, corpus_ids=corpus_ids, user_groups=groups, granted_domains=domains, tables=tables, k=k
    )
    return {"query": q, "results": results}
```

Adapt the exact import paths/signatures to what those helpers actually take (verify `_effective_groups(user, conn)` and `_caller_granted_memory_domains(user, conn)` signatures and whether `get_system_db()` connections are context-managed elsewhere in `app/api/memory.py` — copy that idiom).

Register in `app/main.py` next to the other routers: import `router as knowledge_search_router` and `app.include_router(knowledge_search_router)`.

- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `feat(api): GET /api/knowledge/search — unified knowledge search`

---

### Task 3: MCP tool + CLI command (triple-surface)

**Files:**
- Modify: `cli/mcp/server.py` (new `knowledge_search` tool after `collections_search`)
- Create: `cli/commands/search.py`; Modify: `cli/main.py` (register command)
- Test: extend the existing CLI/MCP test modules (`tests/test_cli_collections.py` sibling patterns; check `tests/test_documentation_api_triple_surface.py` passes)

- [ ] **Step 1: MCP tool** (mirror `collections_search`):

```python
@mcp.tool()
def knowledge_search(query: str, k: int = 10) -> dict:
    """One query across documents, the knowledge base, and the data catalog.

    Fans out server-side over Collections chunks (hybrid lexical+vector),
    corporate-memory knowledge items (fulltext), and table catalog cards —
    all RBAC-filtered. Results are typed ``chunk | knowledge | table``;
    a ``table`` hit means structured data: pivot to SQL via the ``query``
    tool with the hit's ``table_id`` instead of reading text chunks.
    """
    try:
        return api_get_json("/api/knowledge/search", q=query, k=k)
    except V2ClientError as exc:
        raise ValueError(_mcp_error("knowledge_search", exc)) from exc
```

- [ ] **Step 2: CLI command** `cli/commands/search.py` (mirror `search_collections` in `cli/commands/collections.py`, top-level command):

```python
"""`agnes search` — unified knowledge search (K2)."""

from __future__ import annotations

import json as json_lib
from typing import Optional  # noqa: F401 — keep parity with sibling modules

import typer

from cli.lib.api import V2ClientError, api_get_json

search_app = typer.Typer(help="Unified search across documents, knowledge, and the catalog.")


@search_app.callback(invoke_without_command=True)
def search(
    query: str = typer.Argument(..., help="Search query"),
    k: int = typer.Option(10, "--k", help="Max results"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """One query across documents, the knowledge base, and the data catalog."""
    try:
        body = api_get_json("/api/knowledge/search", q=query, k=k)
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return
    results = body.get("results", [])
    if not results:
        typer.echo("No matches.")
        return
    for r in results:
        t = r.get("type")
        if t == "chunk":
            typer.echo(f"[{r.get('score')}] doc  {r.get('filename')} #{r.get('ordinal')}: {(r.get('text') or '')[:110]}")
        elif t == "knowledge":
            typer.echo(f"[{r.get('score')}] know {r.get('title')}: {(r.get('snippet') or '')[:110]}")
        else:
            typer.echo(f"[{r.get('score')}] tbl  {r.get('name')} — {r.get('pivot_hint')}")
```

Wire in `cli/main.py`: `from cli.commands.search import search_app` + `app.add_typer(search_app, name="search")`. Verify the exact `api_get_json` import path used by `cli/commands/collections.py` and copy it.

- [ ] **Step 3: Tests** — add an API-driven CLI test mirroring how `tests/test_cli_collections.py` tests `agnes collections search` (respx/httpx mock or its existing idiom); run `.venv/bin/pytest tests/test_documentation_api_triple_surface.py -q` and confirm the new endpoint is recognized as covered (NOT added to the grandfather list).
- [ ] **Step 4: Commit** `feat(cli,mcp): agnes search + knowledge_search MCP tool`

---

### Task 4: CHANGELOG + full suite

- [ ] CHANGELOG `### Added`:

```markdown
- Unified knowledge search: one query across document Collections (hybrid
  lexical+vector), the corporate-memory knowledge base (fulltext), and the
  table catalog — `GET /api/knowledge/search`, `agnes search`, MCP tool
  `knowledge_search`. Table hits return a "pivot to SQL" hint instead of
  rows (#797).
```

- [ ] Full suite `.venv/bin/pytest tests/ --tb=short -n auto -q` → PASS
- [ ] Commit `docs: changelog for unified knowledge search`

---

## Execution notes

- Branch `zs/k2-knowledge-search` (off `origin/main`, independent of K1).
- After implementation: `/agnes-review` → fix → release-cut decision (if K1's PR #815 merges first, this PR again carries the only `[Unreleased]` content → release-cut 0.74.33 as final commit; re-check at PR time) → PR → auto-merge watch.
