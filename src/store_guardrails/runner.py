"""Orchestrator for the flea-market guardrail pipeline.

Two public entry points:

* :func:`run_inline_checks` — synchronous, runs manifest + static-security
  + quality+templating against a baked plugin tree. Returns an
  ``InlineResult`` the upload endpoint inspects to decide between
  ``blocked_inline`` and ``pending_llm``.

* :func:`run_llm_review` — async background-task entry point. Reads the
  pending submission row, calls the LLM reviewer, persists the verdict,
  and flips ``store_entities.visibility_status`` accordingly. Idempotent
  via the submission ID.

Both functions take a connection factory rather than a live connection so
the BackgroundTask runs against a fresh DuckDB handle (important —
DuckDB connections aren't safe to share across threads).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import duckdb

from src.repositories.audit import AuditRepository
from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.store_submissions import StoreSubmissionsRepository
from . import llm_review, manifest_check, quality_check, static_scan

logger = logging.getLogger(__name__)


@dataclass
class InlineResult:
    """Aggregate verdict from the inline (no-LLM) checks."""

    manifest: Dict[str, Any] = field(default_factory=dict)
    static_security: Dict[str, Any] = field(default_factory=dict)
    quality: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        # Quality is a soft check — it can ``warn`` but never ``fail``,
        # so we ignore its status here and only block on manifest +
        # static-security failures.
        return (
            self.manifest.get("status") == "pass"
            and self.static_security.get("status") == "pass"
        )

    def to_response_dict(self) -> Dict[str, Any]:
        """Shape for the structured 422 body returned to the uploader."""
        return {
            "manifest": self.manifest,
            "static_security": self.static_security,
            "quality": self.quality,
        }


@dataclass
class LlmResult:
    """Aggregate verdict from the LLM security review."""

    verdict: Dict[str, Any] = field(default_factory=dict)
    reviewed_by_model: Optional[str] = None
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return llm_review.is_safe(self.verdict)


def run_inline_checks(
    plugin_dir: Path,
    *,
    type_: str,
    description: Optional[str],
) -> InlineResult:
    """Run the three deterministic checks and aggregate the verdicts."""
    return InlineResult(
        manifest=manifest_check.check(plugin_dir, type_),
        static_security=static_scan.scan_dir(plugin_dir),
        quality=quality_check.check(plugin_dir, description=description),
    )


# ---------------------------------------------------------------------------
# Async LLM review (BackgroundTasks entry point)
# ---------------------------------------------------------------------------


def run_llm_review(
    submission_id: str,
    *,
    plugin_dir: Path,
    conn_factory: Callable[[], duckdb.DuckDBPyConnection],
    api_key_loader: Callable[[], str],
    model_loader: Callable[[], str],
) -> LlmResult:
    """Background-task entry point. Resolves the LLM verdict and persists it.

    Side effects:
        * ``store_submissions.update_status`` — flips status to
          ``approved`` / ``blocked_llm`` / ``review_error`` based on the
          verdict.
        * ``store_entities.set_visibility`` — flips to ``approved`` on
          pass; stays ``pending`` on fail (admin can override or the
          uploader can retry).
        * ``audit_log`` — one row per outcome with the verdict in
          ``params``.

    Errors during the LLM call are recorded as ``review_error`` and
    surface a retry button in the admin UI. Errors during persistence
    propagate — those mean a bug in our DB layer and we want a stack.
    """
    conn = conn_factory()
    try:
        subs_repo = StoreSubmissionsRepository(conn)
        ents_repo = StoreEntitiesRepository(conn)
        audit = AuditRepository(conn)

        sub = subs_repo.get(submission_id)
        if sub is None:
            logger.warning("run_llm_review: submission %s vanished", submission_id)
            return LlmResult(error="submission_missing")

        entity_id = sub.get("entity_id")
        type_ = sub["type"]
        name = sub["name"]
        version = sub.get("version") or ""
        submitter_id = sub.get("submitter_id")

        if not plugin_dir.exists():
            # Bundle was deleted between accept + review (e.g. submitter
            # deleted their entity). Mark the submission so admins can
            # see the trail without a phantom approval.
            subs_repo.update_status(submission_id, status="review_error")
            audit.log(
                user_id=submitter_id,
                action="store.submission.review_error",
                resource=f"store_submission:{submission_id}",
                params={"reason": "plugin_dir_missing"},
                result="error",
            )
            return LlmResult(error="plugin_dir_missing")

        try:
            api_key = api_key_loader()
            model = model_loader()
        except Exception as e:  # config error
            logger.exception("run_llm_review: failed to load LLM config")
            subs_repo.update_status(submission_id, status="review_error")
            audit.log(
                user_id=submitter_id,
                action="store.submission.review_error",
                resource=f"store_submission:{submission_id}",
                params={"reason": "llm_config_unavailable", "error": str(e)},
                result="error",
            )
            return LlmResult(error=f"config:{e}")

        verdict = llm_review.review_bundle(
            plugin_dir,
            type_=type_,
            name=name,
            version=version,
            description=None,
            api_key=api_key,
            model=model,
        )

        if verdict.get("error"):
            subs_repo.update_status(
                submission_id,
                status="review_error",
                llm_findings=verdict,
                reviewed_by_model=model,
            )
            audit.log(
                user_id=submitter_id,
                action="store.submission.review_error",
                resource=f"store_submission:{submission_id}",
                params={"verdict": verdict},
                result="error",
            )
            return LlmResult(verdict=verdict, reviewed_by_model=model,
                             error=verdict["error"])

        passed = llm_review.is_safe(verdict)
        if passed:
            subs_repo.update_status(
                submission_id,
                status="approved",
                llm_findings=verdict,
                reviewed_by_model=model,
            )
            applied = True
            if entity_id:
                # BG-task race guard: only flip when the entity is still
                # in the review window (pending/hidden). If an admin
                # archived the row while the LLM was thinking, leave it
                # archived — the admin's decision wins.
                applied = ents_repo.set_visibility_if_pending(
                    entity_id, "approved",
                )
                # v37 promote-on-approval: PUT edit + restore paths
                # leave the entity row at the prior version_no during
                # the LLM review window so existing installers keep
                # receiving the previously approved bundle. After
                # approval, promote the version this submission added
                # to history (match by hash) — bumps version_no +
                # entity.version + file_size + swaps live ``plugin/``
                # to the new bundle bytes. For initial uploads (v1)
                # this is a no-op since v1 is already current; the
                # match-by-hash succeeds and promote_version sees no
                # state change.
                sub_row = subs_repo.get(submission_id) or {}
                sub_hash = sub_row.get("version")
                ent_row = ents_repo.get(entity_id) or {}
                target_version_no = None
                for entry in (ent_row.get("version_history") or []):
                    if entry.get("hash") == sub_hash:
                        try:
                            target_version_no = int(entry.get("n"))
                        except (TypeError, ValueError):
                            target_version_no = None
                        break
                if (target_version_no is not None
                        and target_version_no != int(ent_row.get("version_no") or 0)):
                    if ents_repo.promote_version(entity_id, target_version_no):
                        try:
                            from app.api.store import _swap_live_to_version
                            _swap_live_to_version(entity_id, target_version_no)
                        except Exception:
                            logger.exception(
                                "promote_version live swap failed for entity %s v%d",
                                entity_id, target_version_no,
                            )
            if applied:
                audit.log(
                    user_id=submitter_id,
                    action="store.submission.approved",
                    resource=f"store_submission:{submission_id}",
                    params={"risk_level": verdict.get("risk_level"),
                            "model": model},
                    result="ok",
                )
            else:
                # Surface the skipped flip so an operator triaging the
                # admin queue understands why an "approved" verdict
                # didn't publish the entity.
                current = (ents_repo.get(entity_id) or {}).get(
                    "visibility_status",
                )
                audit.log(
                    user_id=submitter_id,
                    action="store.submission.bg_verdict_skipped",
                    resource=f"store_submission:{submission_id}",
                    params={
                        "attempted_verdict": "approved",
                        "current_visibility": current,
                        "reason": "entity left review window before "
                                  "LLM verdict landed (admin action)",
                        "model": model,
                    },
                    result="skipped",
                )
        else:
            subs_repo.update_status(
                submission_id,
                status="blocked_llm",
                llm_findings=verdict,
                reviewed_by_model=model,
            )
            # Entity stays at visibility_status='pending' — invisible in
            # the flea browse for non-admins. Admin can override.
            audit.log(
                user_id=submitter_id,
                action="store.submission.blocked_llm",
                resource=f"store_submission:{submission_id}",
                params={"risk_level": verdict.get("risk_level"),
                        "finding_count": len(verdict.get("findings") or []),
                        "model": model},
                result="blocked",
            )

        return LlmResult(verdict=verdict, reviewed_by_model=model)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# Convenience: default factories the API layer wires up. Kept here so
# tests can swap in mocks without monkeypatching the API module.

def default_api_key_loader() -> str:
    """Read ANTHROPIC_API_KEY from the environment.

    The same env var the corporate-memory service uses; keeping a single
    convention so operators don't juggle multiple keys.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        # Fall back to LLM_API_KEY for parity with the factory's
        # backward-compat resolution path.
        key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Guardrail LLM review requires ANTHROPIC_API_KEY (or LLM_API_KEY) "
            "in the environment. Set it on the FastAPI container, or disable "
            "guardrails via instance.yaml: guardrails.enabled: false"
        )
    return key


def default_model_loader() -> str:
    """Resolve the configured model tier to a concrete Anthropic model ID."""
    from app.instance_config import get_guardrails_review_model

    return get_guardrails_review_model()
