"""FastAPI router for the flea-market community skill marketplace."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.auth.dependencies import get_current_user

from src.flea_market import (
    FleaMarketConfig,
    SKILL_SLUG_RE,
    clear_pending_marker,
    list_pending_skills,
    list_skills,
    refresh_serving,
    review_skill,
    skill_exists,
    slugify,
    write_pending_marker,
    write_skill_and_bump_version,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/flea-market", tags=["flea-market"])

_RETRY_INTERVAL = 300  # seconds between retry sweeps


class SubmitRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=64)
    description: str = Field(..., min_length=10, max_length=200)
    body: str = Field(..., min_length=20, max_length=20_000)

    @field_validator("name")
    @classmethod
    def name_must_be_valid_slug(cls, v: str) -> str:
        s = slugify(v)
        if not SKILL_SLUG_RE.match(s):
            raise ValueError("Name must produce a valid slug (letters, digits, hyphens)")
        return s


class SubmitResponse(BaseModel):
    status: str
    skill_name: str
    warning: Optional[str] = None
    duplicate_reason: Optional[str] = None


def _build_config() -> Optional[FleaMarketConfig]:
    """Build FleaMarketConfig from instance config, or None when not configured."""
    try:
        from app.instance_config import get_value
        cfg = get_value("flea_market", default=None)
        if not cfg:
            return None
        return FleaMarketConfig(
            marketplace_slug=cfg.get("marketplace_slug", "flea-market"),
            plugin_name=cfg.get("plugin_name", "flea-market"),
            github_repo=cfg.get("github_repo", ""),
            github_pat=cfg.get("github_pat", ""),
            github_app_id=cfg.get("github_app_id", ""),
            github_app_private_key=cfg.get("github_app_private_key", ""),
            github_app_installation_id=cfg.get("github_app_installation_id", ""),
            github_api_url=cfg.get("github_api_url", "https://api.github.com"),
        )
    except Exception:
        return None


def _get_config() -> FleaMarketConfig:
    """Return config or raise HTTP 503 when not configured."""
    cfg = _build_config()
    if cfg is None:
        raise HTTPException(status_code=503, detail="Flea market is not configured on this instance.")
    return cfg


def _get_extractor() -> Any:
    """Return an LLM extractor, or a no-op stub when no LLM is configured."""
    try:
        from connectors.llm import get_extractor
        return get_extractor()
    except Exception:
        return _NoOpExtractor()


class _NoOpExtractor:
    """Stub used when no LLM connector is available — skips duplicate detection."""
    def extract_json(self, **_kwargs):
        return {
            "is_duplicate": False, "duplicate_of": None, "duplicate_reason": None,
            "requires_setup": False, "setup_description": None,
        }


def _do_github_push(config: FleaMarketConfig, skill_name: str) -> None:
    """Push SKILL.md to GitHub. Clears the pending marker on success; leaves it on failure so the retry loop retries."""
    skill_md_path = config.skills_dir() / skill_name / "SKILL.md"
    try:
        skill_md = skill_md_path.read_text(encoding="utf-8")
    except OSError:
        logger.error("flea_market: SKILL.md missing for pending skill %s — clearing marker", skill_name)
        clear_pending_marker(config, skill_name)
        return
    try:
        if config.github_pat:
            from src.github_app import push_skill_with_pat
            push_skill_with_pat(
                config.github_pat, config.github_repo, config.plugin_name, skill_name, skill_md,
                api_url=config.github_api_url,
            )
        else:
            from src.github_app import GitHubAppConfig, push_skill
            push_skill(
                GitHubAppConfig(
                    app_id=config.github_app_id,
                    private_key_pem=config.github_app_private_key,
                    installation_id=config.github_app_installation_id,
                    repo=config.github_repo,
                    api_url=config.github_api_url,
                ),
                config.plugin_name, skill_name, skill_md,
            )
        clear_pending_marker(config, skill_name)
        logger.info("flea_market: pushed skill %s to GitHub", skill_name)
    except Exception:
        logger.exception("flea_market: GitHub push failed for %s (will retry)", skill_name)


def _retry_pending_skills(config: FleaMarketConfig) -> None:
    """Push any skills whose .pending marker was left by a failed background task."""
    pending = list_pending_skills(config)
    if not pending:
        return
    logger.info("flea_market: retrying %d pending skill push(es): %s", len(pending), pending)
    for skill_name in pending:
        _do_github_push(config, skill_name)


def _retry_loop() -> None:
    """Daemon: scan for pending skills and push them. Runs immediately on start, then every _RETRY_INTERVAL seconds."""
    while True:
        try:
            cfg = _build_config()
            if cfg is not None:
                _retry_pending_skills(cfg)
        except Exception:
            logger.exception("flea_market: retry loop error")
        time.sleep(_RETRY_INTERVAL)


def start_retry_loop() -> None:
    """Start the background GitHub-push retry loop. Call once from app startup."""
    t = threading.Thread(target=_retry_loop, daemon=True, name="flea-market-retry")
    t.start()
    logger.info("flea_market: retry loop started (interval=%ds)", _RETRY_INTERVAL)


@router.get("/skills")
def get_skills(user: dict = Depends(get_current_user)):
    config = _get_config()
    return {"skills": list_skills(config)}


@router.post("/submit", response_model=SubmitResponse)
def submit_skill(req: SubmitRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    config = _get_config()
    skill_name = req.name  # already slugified by validator

    if skill_exists(config, skill_name):
        raise HTTPException(
            status_code=409,
            detail=f"A skill named '{skill_name}' already exists. Choose a different name.",
        )

    extractor = _get_extractor()
    existing = list_skills(config)
    review = review_skill(extractor, skill_name, req.description, req.body, existing)

    write_skill_and_bump_version(config, skill_name, req.description, req.body)
    write_pending_marker(config, skill_name)

    try:
        refresh_serving(config.marketplace_slug)
    except Exception:
        logger.exception("flea_market: refresh_serving raised unexpectedly for %s", skill_name)

    background_tasks.add_task(_do_github_push, config, skill_name)

    warnings: list[str] = []
    if review.is_duplicate:
        warnings.append(
            f"This skill may overlap with '{review.duplicate_of}': {review.duplicate_reason}. "
            "Consider merging with the existing skill."
        )
    if review.requires_setup:
        warnings.append(
            f"This skill requires additional setup: {review.setup_description}. "
            "Make sure users know what to install before using it."
        )

    return SubmitResponse(
        status="submitted",
        skill_name=skill_name,
        warning=" | ".join(warnings) if warnings else None,
        duplicate_reason=review.duplicate_reason,
    )
