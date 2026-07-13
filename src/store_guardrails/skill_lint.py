"""Skill linter — composition of quality checks and guardrail rules (SL002, SL010, SL011, SL012).

Provides a fault-tolerant linting engine that never raises. Rules are wrapped
individually so one rule's error doesn't prevent others from running.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from src.store_guardrails.craft_review import CraftUnavailable
from src.store_guardrails.quality_check import check as quality_check

if TYPE_CHECKING:
    from src.store_guardrails.craft_review import CraftCaller

logger = logging.getLogger(__name__)


class LintFinding(TypedDict):
    """A linter finding with rule ID, severity, message, evidence, and doc URL."""

    rule_id: str  # "SL002" | "SL010" | "SL011" | "SL012" | quality-check passthrough ids
    severity: str  # "info" | "warn"
    message: str
    evidence: dict
    doc_url: str  # "/docs/skill-guidelines#sl002"


class LintReport(TypedDict):
    """Result of linting a skill."""

    findings: list[LintFinding]
    rules_run: list[str]
    llm_used: bool
    content_hash: str


def compute_content_hash(skill_md: str) -> str:
    """Compute SHA256 hex digest of the skill markdown (stripped)."""
    return hashlib.sha256(skill_md.strip().encode("utf-8")).hexdigest()


def _extract_body_from_markdown(skill_md: str) -> str:
    """Extract the body (non-frontmatter) portion of a SKILL.md file.

    Returns the text after the closing --- of YAML frontmatter,
    or the full text if no frontmatter is detected.
    """
    lines = skill_md.split("\n")
    if not lines or lines[0] != "---":
        return skill_md

    # Find the closing --- of YAML frontmatter
    for i in range(1, len(lines)):
        if lines[i] == "---":
            # Body starts after the closing ---
            return "\n".join(lines[i + 1 :])

    # No closing ---, treat entire content as body
    return skill_md


def lint_skill(
    entity: dict,
    skill_md: str,
    *,
    plugin_dir: Path | None = None,
    candidates: list[tuple[Any, float]] | None = None,
    craft: "CraftCaller | None" = None,
) -> LintReport:
    """Lint a skill for marketplace guidelines (SL002, SL010, SL011, SL012, composition).

    Args:
        entity: dict with keys id?, name, description (may be None), type
        skill_md: skill markdown content
        plugin_dir: baked tree when caller has one; None → synthesize temp tree
        candidates: lexical top-N (Task 3); None → skip dup rules
        craft: CraftCaller injectable (Task 4); None, or a failed call, →
            degraded mode (SL011/SL012 run instead of SL010)

    Returns:
        LintReport with findings, rules_run, llm_used, and content_hash.

    Never raises — all rules are wrapped and logged on error.
    """
    findings: list[LintFinding] = []
    rules_run: list[str] = []
    llm_used = False

    content_hash = compute_content_hash(skill_md)

    # --- SL002: Body length check ---
    try:
        rules_run.append("SL002")
        from app.instance_config import get_lint_max_body_chars

        max_chars = get_lint_max_body_chars()
        body = _extract_body_from_markdown(skill_md)
        body_len = len(body)

        if body_len > max_chars:
            findings.append(
                LintFinding(
                    rule_id="SL002",
                    severity="warn",
                    message=f"SKILL.md body is {body_len} chars (limit {max_chars}). "
                    f"Move detail into references/ files the agent loads on demand.",
                    evidence={"body_length": body_len, "limit": max_chars},
                    doc_url="/docs/skill-guidelines#sl002",
                )
            )
    except Exception as e:
        logger.exception("SL002 rule failed: %s", e)

    # --- SL010: holistic LLM craft review ---
    # `craft` is a CraftCaller (see craft_review.default_craft_caller) —
    # a 3-arg callable bound to the configured LLM, or None when the
    # caller passed no craft review (or none is configured). It raises
    # CraftUnavailable on failure (LLM error, malformed verdict) rather
    # than returning [] — that distinction lets us fall back to the
    # degraded-mode SL011/SL012 heuristics on failure instead of treating
    # a transient outage as "the LLM confirmed this skill is clean".
    if craft is not None:
        try:
            rules_run.append("SL010")
            craft_findings = craft(entity, skill_md, candidates or [])
            findings.extend(craft_findings)
            llm_used = True
        except CraftUnavailable:
            logger.warning("SL010 craft review unavailable; falling back to degraded-mode rules")
        except Exception as e:
            logger.exception("SL010 craft review raised unexpectedly: %s", e)

    # --- SL011: Trigger phrase detection (degraded mode only) ---
    if not llm_used:
        try:
            rules_run.append("SL011")
            description = entity.get("description") or ""
            # Check for trigger phrasing in description
            trigger_regex = re.compile(
                r"\b(use when|use this when|triggers? on|activates? when|invoke when)\b",
                re.IGNORECASE,
            )
            if not trigger_regex.search(description):
                findings.append(
                    LintFinding(
                        rule_id="SL011",
                        severity="info",
                        message="Description lacks trigger phrasing (e.g., 'Use when', 'Activate when'). "
                        "Users decide whether to invoke skills based on the trigger context.",
                        evidence={"description": description},
                        doc_url="/docs/skill-guidelines#sl011",
                    )
                )
        except Exception as e:
            logger.exception("SL011 rule failed: %s", e)

    # --- SL012: Lexical duplicate recall (degraded mode only) ---
    # `candidates` is the caller's pre-computed lexical top-N (see
    # `src.store_guardrails.lint_corpus.top_candidates`); this rule only
    # renders it as a finding. When SL010 actually ran (llm_used), the LLM
    # duplicate-review rule takes over and this info-level heads-up is
    # redundant, so it's degraded-mode only.
    if not llm_used and candidates:
        try:
            rules_run.append("SL012")
            scored = [
                {"name": (c.get("name") if isinstance(c, dict) else None) or "", "score": round(float(score), 2)}
                for c, score in candidates
            ]
            listing = ", ".join(f"{s['name']} ({s['score']:.2f})" for s in scored)
            findings.append(
                LintFinding(
                    rule_id="SL012",
                    severity="info",
                    message=f"Lexically similar to existing marketplace skill(s): {listing}. "
                    f"LLM-based duplicate review was unavailable (degraded mode) — verify manually.",
                    evidence={"candidates": scored, "degraded": True},
                    doc_url="/docs/skill-guidelines#sl012",
                )
            )
        except Exception as e:
            logger.exception("SL012 rule failed: %s", e)

    # --- Compose quality_check when plugin_dir is None ---
    try:
        rules_run.append("quality_check")
        actual_plugin_dir = plugin_dir
        tmpdir: str | None = None

        if actual_plugin_dir is None:
            # Synthesize a temp tree <name>/SKILL.md
            name = entity.get("name", "skill").replace("/", "_").replace(" ", "_")
            tmpdir = tempfile.mkdtemp(prefix="skill-lint-")
            actual_plugin_dir = Path(tmpdir) / name
            actual_plugin_dir.mkdir(parents=True, exist_ok=True)
            (actual_plugin_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

        # Call quality_check with the synthesized or provided tree. The temp
        # tree is removed in a finally so it never leaks, even when
        # quality_check raises.
        try:
            description = entity.get("description")
            qc_result = quality_check(actual_plugin_dir, description=description)
        finally:
            if tmpdir is not None:
                import shutil

                shutil.rmtree(tmpdir, ignore_errors=True)

        # Map quality_check issues to LintFindings
        # Issues format: ["description_too_short", "missing_primary_doc", "doc_too_short",
        #                 "lorem_ipsum:path.md", "insert_placeholder:path.md", "todo_floor:path.md"]
        for issue in qc_result.get("issues", []):
            # Split on : to separate rule label from path (if any)
            parts = issue.split(":", 1)
            rule_label = parts[0]
            path_hint = parts[1] if len(parts) > 1 else ""

            # Human-readable messages describing what actually fired.
            issue_messages = {
                "description_too_short": "Description is too short (must be at least 20 characters).",
                "missing_primary_doc": "Missing primary documentation file (SKILL.md, agent.md, README.md, or .md).",
                "doc_too_short": "Primary documentation body is too short (must be at least 200 characters).",
                "lorem_ipsum": "Detected 'lorem ipsum' placeholder text.",
                "insert_placeholder": "Detected <INSERT_X_HERE> style placeholder.",
                "todo_floor": "Detected 'TODO' marker on an otherwise empty line.",
            }
            message = issue_messages.get(rule_label, f"Quality issue: {rule_label}")
            if path_hint:
                message += f" (found in {path_hint})"

            findings.append(
                LintFinding(
                    rule_id=f"QC-{rule_label.upper()}",
                    severity="info",
                    message=message,
                    evidence={"issue": issue},
                    doc_url="/docs/skill-guidelines",
                )
            )

    except Exception as e:
        logger.exception("quality_check composition failed: %s", e)

    return LintReport(
        findings=findings,
        rules_run=rules_run,
        llm_used=llm_used,
        content_hash=content_hash,
    )
