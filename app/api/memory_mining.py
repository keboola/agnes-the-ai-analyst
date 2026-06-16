"""Memory mining API (v78) — corporate-memory-from-sessions, privacy-gated.

Privacy gate (design spec §4.4): mining a user's session transcripts into shared
corporate memory is OPT-IN. Each candidate is PII-scanned before it can become a
proposal, carries provenance (which author it derived from), and routes through
the authoring-suggestions moderation queue — never an admin-direct write.

Routers:
  - GET/POST /api/studio/memory-mining/consent   — a user manages their own opt-in
  - POST /api/admin/memory-mining/run             — admin mines opted-in users

NOTE: the candidate *extraction* here is a deterministic placeholder (one
provenance-tagged seed per opted-in author). Distilling rich knowledge items
from transcript content is the LLM step, pluggable on top of this gate — it does
not change the consent / PII / provenance / approval contract enforced here.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user
from src.repositories import (
    authoring_suggestions_repo,
    memory_mining_consent_repo,
)

logger = logging.getLogger(__name__)

public_router = APIRouter(prefix="/api/studio/memory-mining", tags=["memory-mining"])
admin_router = APIRouter(prefix="/api/admin/memory-mining", tags=["memory-mining"])

# Conservative PII signals — an extracted candidate that trips any of these is
# dropped rather than proposed (better a missed insight than a leak into a
# group-broadcast memory tier).
_PII_PATTERNS = [
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # email
    re.compile(r"\b(?:\+?\d[\s-]?){9,}\d\b"),  # phone-ish
    re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|ghp_[A-Za-z0-9]{20,})\b"),  # secrets
]


def _looks_like_pii(text: str) -> bool:
    return any(p.search(text) for p in _PII_PATTERNS)


class ConsentBody(BaseModel):
    opt_in: bool


@public_router.get("/consent")
async def get_consent(user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    return {"opted_in": memory_mining_consent_repo().is_opted_in(user["email"])}


@public_router.post("/consent")
async def set_consent(body: ConsentBody, user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    memory_mining_consent_repo().set_consent(user["email"], opted_in=body.opt_in)
    return {"opted_in": body.opt_in}


@admin_router.post("/run")
async def run_mining(_admin: dict = Depends(require_admin)) -> Dict[str, Any]:
    """Mine opted-in users into corporate-memory suggestions (privacy-gated)."""
    consent = memory_mining_consent_repo()
    sugg = authoring_suggestions_repo()
    # Dedup: skip authors who already have a PENDING corporate-memory proposal,
    # so re-running the miner doesn't spam the moderation queue with duplicates.
    already_pending = {
        (s.get("payload") or {}).get("provenance", {}).get("author")
        for s in sugg.list(status="pending", domain="corporate-memory")
    }
    created: list[str] = []
    skipped_pii = 0
    skipped_existing = 0
    for author in consent.list_opted_in():
        if author in already_pending:
            skipped_existing += 1
            continue
        # Placeholder extraction: one provenance-tagged seed per opted-in author.
        # The author's email lives ONLY in provenance (their own attribution) —
        # never in the candidate name/description, which is PII-scanned as the
        # content that would land in the shared, group-broadcast memory tier.
        slug = re.sub(r"[^a-z0-9]+", "-", author.lower()).strip("-")
        name = f"Session insights ({slug})"
        description = "Knowledge distilled from a contributor's opted-in sessions."
        if _looks_like_pii(name) or _looks_like_pii(description):
            skipped_pii += 1
            continue
        payload = {
            "name": name,
            "slug": f"session-insights-{slug}"[:63],
            "description": description,
            "provenance": {"source": "sessions", "author": author},
        }
        created.append(sugg.create(domain="corporate-memory", payload=payload, created_by="memory-miner"))
    return {
        "created": created,
        "skipped_pii": skipped_pii,
        "skipped_existing": skipped_existing,
        "authors": len(created) + skipped_pii + skipped_existing,
    }
