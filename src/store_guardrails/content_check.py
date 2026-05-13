"""Hard-fail content guardrail for uploaded bundles.

Iterates over each component in the baked plugin tree (plugin manifest,
agents, skills, commands) and rejects submissions where component
descriptions don't meet a mechanical floor — empty, placeholder-only,
single-word padding, unfilled ``{{var}}`` tokens inside the description
string itself.

This is the cheap inline tier of the two-tier content guardrail. The
substantive "is this description actually helpful?" judgement runs as
part of ``llm_review.py`` against the same bundle; mechanical failures
short-circuit before any LLM call so we don't burn an Anthropic round
on `description: TODO`.

The criteria are intentionally low. A description that passes here can
still be flagged by the LLM tier for being vague or generic. The point
of this module is to catch the "you pasted a template and forgot to
fill it in" cases that have no business reaching the model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._frontmatter import frontmatter_body_offset, parse_frontmatter


# ---------------------------------------------------------------------------
# Criteria constants
# ---------------------------------------------------------------------------

# Hard-floor defaults. Calibrated against real ecosystem norms (Claude
# skill packs cluster 150–220 chars per description; npm / Docker Hub /
# VS Code 100–120). 60 chars + 5 distinct words catches the "didn't
# bother" cases; the LLM tier judges substantive quality on top.
#
# Defaults are overridable via `instance.yaml.guardrails.*` keys —
# operators tune the floor without code changes. Resolution helpers
# below read the live config on every call so /admin/server-config
# patches take effect on the next request (no app restart needed).
_DEFAULT_MIN_DESC_CHARS = 60
_DEFAULT_MIN_COMMAND_DESC_CHARS = 25
_DEFAULT_MIN_DISTINCT_WORDS = 5
_DEFAULT_MIN_BODY_CHARS = 200


def _min_desc_chars() -> int:
    try:
        from app.instance_config import get_guardrails_min_description_chars
        return get_guardrails_min_description_chars()
    except ImportError:
        return _DEFAULT_MIN_DESC_CHARS


def _min_command_desc_chars() -> int:
    try:
        from app.instance_config import get_guardrails_min_command_description_chars
        return get_guardrails_min_command_description_chars()
    except ImportError:
        return _DEFAULT_MIN_COMMAND_DESC_CHARS


def _min_distinct_words() -> int:
    try:
        from app.instance_config import get_guardrails_min_distinct_words
        return get_guardrails_min_distinct_words()
    except ImportError:
        return _DEFAULT_MIN_DISTINCT_WORDS


def _min_body_chars() -> int:
    try:
        from app.instance_config import get_guardrails_min_body_chars
        return get_guardrails_min_body_chars()
    except ImportError:
        return _DEFAULT_MIN_BODY_CHARS

# Case-insensitive substring matches that mark an unfilled template.
_PLACEHOLDER_PHRASES = (
    "your description here",
    "brief description",
    "add a description",
    "fill in description",
    "lorem ipsum",
)
# Whole-string equality (after .strip().lower()) for short placeholders.
_PLACEHOLDER_LITERALS = {
    "todo",
    "tbd",
    "description",
    "n/a",
    "...",
}
# Unfilled jinja-style placeholder inside the description string itself.
_PLACEHOLDER_TOKEN_RE = re.compile(r"\{\{\s*[A-Za-z_][A-Za-z0-9_\-]*[^}]*\}\}")
# Placeholder tokens at the START of the description string. Tightened
# to start-of-string only so a legitimate description like "Use when
# refactoring TODO-tagged code" doesn't false-positive.
_PLACEHOLDER_HEAD_RE = re.compile(r"^\s*(TODO|TBD|FIXME|XXX)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check(plugin_dir: Path) -> Dict[str, Any]:
    """Walk the baked tree, evaluate each component, aggregate.

    Returns ``{"status": "pass"|"fail", "issues": [...]}`` where each
    issue carries enough context for the rejection banner to render an
    actionable line (file, field, code, hint, name).
    """
    if not plugin_dir.is_dir():
        return {"status": "fail", "issues": [{
            "file": "<plugin>",
            "field": "tree",
            "code": "plugin_dir_missing",
            "hint": "The plugin directory wasn't created — re-upload the ZIP.",
        }]}

    components = list(_iter_components(plugin_dir))
    issues: List[Dict[str, Any]] = []
    for comp in components:
        issues.extend(_evaluate(comp))

    return {
        "status": "fail" if issues else "pass",
        "issues": issues,
    }


def summarize_for_preview(scratch_root: Path, type_: str) -> List[Dict[str, Any]]:
    """Preview-time component summary against the raw extracted ZIP.

    Unlike ``summarize_components`` (which expects the baked tree under
    ``skills/`` / ``agents/`` / ``.claude-plugin/``), this walks the raw
    upload and locates components flexibly so the upload form can show
    red/green dots before the bake step.

    For skill / agent uploads the result is a single component row. For
    plugin uploads the result mirrors the baked layout (the bake just
    mirrors the upload tree).
    """
    if not scratch_root.is_dir():
        return []
    if type_ == "skill":
        skill_md = _find_first(scratch_root, lambda p: p.name.lower() == "skill.md")
        if skill_md is None:
            return []
        text = _read_text(skill_md)
        fm = parse_frontmatter(text)
        body_offset = frontmatter_body_offset(text)
        comp = {
            "type": "skill",
            "file": skill_md.relative_to(scratch_root).as_posix(),
            "name": fm.get("name"),
            "description": fm.get("description"),
            "body": text[body_offset:].strip(),
        }
        return _comp_rows([comp])
    if type_ == "agent":
        agent_md = None
        for p in sorted(scratch_root.rglob("*.md")):
            if not p.is_file():
                continue
            if p.name.lower() == "skill.md":
                continue
            if ".claude-plugin" in p.parts:
                continue
            fm = parse_frontmatter(_read_text(p))
            if fm.get("name") or fm.get("description") is not None:
                agent_md = p
                break
        if agent_md is None:
            return []
        text = _read_text(agent_md)
        fm = parse_frontmatter(text)
        body_offset = frontmatter_body_offset(text)
        comp = {
            "type": "agent",
            "file": agent_md.relative_to(scratch_root).as_posix(),
            "name": fm.get("name"),
            "description": fm.get("description"),
            "body": text[body_offset:].strip(),
        }
        return _comp_rows([comp])
    # Plugin uploads carry the same layout as the baked tree, so reuse.
    return summarize_components(scratch_root)


def _find_first(root: Path, predicate) -> Optional[Path]:
    for p in sorted(root.rglob("*")):
        if p.is_file() and predicate(p):
            return p
    return None


def _comp_rows(comps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Shared issue-evaluation step used by summarize_* functions."""
    rows: List[Dict[str, Any]] = []
    for comp in comps:
        comp_issues = _evaluate(comp)
        rows.append({
            "type": comp["type"],
            "name": comp.get("name") or "",
            "file": comp["file"],
            "description": (comp.get("description") or "").strip(),
            "ok": not comp_issues,
            "issues": comp_issues,
        })
    return rows


def summarize_components(plugin_dir: Path) -> List[Dict[str, Any]]:
    """Per-component summary for the upload preview UI.

    Returns one row per component: ``{type, name, description, ok,
    issues}``. ``issues`` is the same shape as in ``check()`` but
    scoped to that component. Used by the upload form to show
    red/green dots before the submitter hits Finish.
    """
    if not plugin_dir.is_dir():
        return []
    return _comp_rows(list(_iter_components(plugin_dir)))


def check_submission_description(description: Optional[str]) -> Dict[str, Any]:
    """Evaluate the form-level description (the one on the marketplace tile).

    Same criteria as a plugin component description — denylist + length
    + word count. Returns the same shape as ``check()`` but with a
    single synthetic ``file`` = ``<submission>``.
    """
    issues = _evaluate_description_string(
        description, min_chars=_min_desc_chars(),
        component_kind="submission",
    )
    if issues:
        for issue in issues:
            issue.setdefault("file", "<submission>")
            issue.setdefault("field", "description")
            issue.setdefault("component_type", "submission")
    return {
        "status": "fail" if issues else "pass",
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Component walker
# ---------------------------------------------------------------------------


def _iter_components(plugin_dir: Path):
    """Yield a ``Component`` dict per discoverable component in the tree.

    Shape::

        {"type": "plugin" | "agent" | "skill" | "command",
         "file": "<rel path>", "name": str | None,
         "description": str | None, "body": str | None}

    Skills include their SKILL.md body; agents include their post-frontmatter
    body; plugins have no body (JSON-only); commands have a body but it
    rarely matters (1-liner commands are common) so we don't enforce it.
    """
    # Plugin manifest first — every plugin tree has at most one.
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        yield {
            "type": "plugin",
            "file": ".claude-plugin/plugin.json",
            "name": (data.get("name") if isinstance(data, dict) else None),
            "description": (data.get("description") if isinstance(data, dict) else None),
            "body": None,
        }

    # Skills: skills/<suffixed>/SKILL.md (case-insensitive).
    skills_root = plugin_dir / "skills"
    if skills_root.is_dir():
        for skill_md in sorted(skills_root.rglob("*")):
            if not skill_md.is_file():
                continue
            if skill_md.name.lower() != "skill.md":
                continue
            text = _read_text(skill_md)
            fm = parse_frontmatter(text)
            body_offset = frontmatter_body_offset(text)
            yield {
                "type": "skill",
                "file": skill_md.relative_to(plugin_dir).as_posix(),
                "name": fm.get("name"),
                "description": fm.get("description"),
                "body": text[body_offset:].strip(),
            }

    # Agents: agents/*.md at the top level (the baked tree puts them there).
    # Skip files that obviously aren't agents — README and other helper docs
    # under agents/ would otherwise trip a `frontmatter.description empty`
    # rejection. Mirror the filter `summarize_for_preview` already applies
    # for type=agent (skip files without `name`/`description` in frontmatter)
    # so the upload preview's red/green dots match the post-bake decision.
    agents_root = plugin_dir / "agents"
    if agents_root.is_dir():
        for agent_md in sorted(agents_root.rglob("*.md")):
            if not agent_md.is_file():
                continue
            text = _read_text(agent_md)
            fm = parse_frontmatter(text)
            # No frontmatter at all → not an agent file (README, NOTES, etc.).
            # The preview walker uses the same heuristic; keeping the two
            # in sync prevents the "preview says OK, submit says fail" UX.
            if not fm.get("name") and fm.get("description") is None:
                continue
            body_offset = frontmatter_body_offset(text)
            yield {
                "type": "agent",
                "file": agent_md.relative_to(plugin_dir).as_posix(),
                "name": fm.get("name"),
                "description": fm.get("description"),
                "body": text[body_offset:].strip(),
            }

    # Commands: commands/*.md anywhere in the tree.
    commands_root = plugin_dir / "commands"
    if commands_root.is_dir():
        for cmd_md in sorted(commands_root.rglob("*.md")):
            if not cmd_md.is_file():
                continue
            text = _read_text(cmd_md)
            fm = parse_frontmatter(text)
            yield {
                "type": "command",
                "file": cmd_md.relative_to(plugin_dir).as_posix(),
                "name": fm.get("name"),
                "description": fm.get("description"),
                "body": None,  # Not enforced for commands.
            }


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------


def _evaluate(comp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the issue list for one component (empty when it passes)."""
    type_ = comp["type"]
    min_chars = _min_command_desc_chars() if type_ == "command" else _min_desc_chars()

    issues = _evaluate_description_string(
        comp.get("description"),
        min_chars=min_chars,
        component_kind=type_,
    )
    for issue in issues:
        issue.setdefault("file", comp["file"])
        issue.setdefault("field", _desc_field_label(type_))
        issue.setdefault("component_type", type_)
        if comp.get("name"):
            issue.setdefault("name", comp["name"])

    # Body check applies only to skills + agents (plugins are JSON-only;
    # commands often legitimately have a one-line body).
    if type_ in {"skill", "agent"}:
        body = (comp.get("body") or "").strip()
        if len(body) < _min_body_chars():
            issues.append({
                "file": comp["file"],
                "field": "body",
                "code": "body_too_short",
                "hint": _hint_for(type_, "body_too_short", min_chars=_min_body_chars()),
                "name": comp.get("name"),
                "component_type": type_,
            })

    return issues


def _desc_field_label(type_: str) -> str:
    if type_ == "plugin":
        return "plugin.json.description"
    return "frontmatter.description"


def _evaluate_description_string(
    description: Optional[str],
    *,
    min_chars: int,
    component_kind: str,
) -> List[Dict[str, Any]]:
    """Run the denylist + length + word-count checks against a single string."""
    raw = (description or "").strip()
    if not raw:
        return [{
            "code": "empty",
            "hint": _hint_for(component_kind, "empty"),
        }]

    lowered = raw.lower()
    if lowered in _PLACEHOLDER_LITERALS:
        return [{
            "code": "placeholder_text",
            "hint": _hint_for(component_kind, "placeholder_text"),
        }]
    for phrase in _PLACEHOLDER_PHRASES:
        if phrase in lowered:
            return [{
                "code": "placeholder_text",
                "hint": _hint_for(component_kind, "placeholder_text"),
            }]
    if _PLACEHOLDER_TOKEN_RE.search(raw):
        return [{
            "code": "placeholder_text",
            "hint": _hint_for(component_kind, "placeholder_text"),
        }]
    if _PLACEHOLDER_HEAD_RE.search(raw):
        # TODO / TBD / FIXME / XXX at the start — "TODO add later" shape.
        return [{
            "code": "placeholder_text",
            "hint": _hint_for(component_kind, "placeholder_text"),
        }]

    if len(raw) < min_chars:
        return [{
            "code": "too_short",
            "hint": _hint_for(component_kind, "too_short", min_chars=min_chars),
        }]

    # Distinct words — split on whitespace, strip punctuation, lowercase.
    tokens = [
        re.sub(r"[^\w]+", "", t).lower()
        for t in raw.split()
    ]
    distinct = {t for t in tokens if t}
    if len(distinct) < _min_distinct_words():
        return [{
            "code": "low_word_count",
            "hint": _hint_for(component_kind, "low_word_count"),
        }]

    return []


# ---------------------------------------------------------------------------
# Hints (the "next-round tips" surfaced to the submitter)
# ---------------------------------------------------------------------------


def _hint_for(component_kind: str, code: str, **fmt: Any) -> str:
    base = _HINTS.get((component_kind, code)) or _HINTS.get(("*", code))
    if base is None:
        return "Re-upload with a clearer description."
    try:
        return base.format(**fmt)
    except (KeyError, IndexError):
        return base


# Hints are intentionally plain-language. No inline examples, no
# ecosystem jargon — the rendering layer surfaces a "See example ↗"
# link next to every hint that deep-links to the matching section on
# /store/examples (anchors: #skill / #agent / #plugin / #command /
# #submission).
_HINTS: Dict[Tuple[str, str], str] = {
    # ---- Skills --------------------------------------------------------
    ("skill", "empty"): (
        "Skill description is missing. Add a frontmatter "
        "`description:` line — this is what the assistant reads to "
        "decide whether to use the skill."
    ),
    ("skill", "placeholder_text"): (
        "Description is still a placeholder. Replace `TODO` / "
        "`description` / `{{var}}` with a real sentence that says "
        "when to use the skill and what it does."
    ),
    ("skill", "too_short"): (
        "Skill description is too short (minimum {min_chars} "
        "characters). Say when to use the skill AND what it does in "
        "one sentence — the assistant uses this string to decide "
        "whether to call the skill."
    ),
    ("skill", "low_word_count"): (
        "Skill description doesn't have enough distinct words. "
        "Rewrite as a real sentence — repeating the same word "
        "doesn't help the assistant decide when to invoke it."
    ),
    ("skill", "body_too_short"): (
        "Skill content is too short (minimum {min_chars} characters). "
        "Explain what the skill does, when to use it, and what inputs "
        "it expects."
    ),
    # ---- Agents --------------------------------------------------------
    ("agent", "empty"): (
        "Agent description is missing. Add a frontmatter "
        "`description:` line — this is what the routing layer reads "
        "to decide when to dispatch the agent."
    ),
    ("agent", "placeholder_text"): (
        "Description is still a placeholder. Replace `TODO` / "
        "`description` / `{{var}}` with a real sentence that says "
        "what the agent does and when to dispatch it."
    ),
    ("agent", "too_short"): (
        "Agent description is too short (minimum {min_chars} "
        "characters). Say what the agent does AND when to dispatch "
        "it in one sentence."
    ),
    ("agent", "low_word_count"): (
        "Agent description doesn't have enough distinct words. "
        "Rewrite as a real sentence — routing depends on it."
    ),
    ("agent", "body_too_short"): (
        "Agent content is too short (minimum {min_chars} characters). "
        "Explain the agent's behaviour, expected inputs, and when to "
        "dispatch it."
    ),
    # ---- Plugins -------------------------------------------------------
    ("plugin", "empty"): (
        "Plugin description is missing. Add a `description` field to "
        "`.claude-plugin/plugin.json` — this is what people see on "
        "the marketplace tile."
    ),
    ("plugin", "placeholder_text"): (
        "Plugin description is still a placeholder. Replace with a "
        "real one-sentence pitch."
    ),
    ("plugin", "too_short"): (
        "Plugin description is too short (minimum {min_chars} "
        "characters). Treat it like an elevator pitch for the "
        "marketplace tile — what does it do, and who is it for?"
    ),
    ("plugin", "low_word_count"): (
        "Plugin description doesn't have enough distinct words. "
        "Rewrite as a real sentence."
    ),
    # ---- Commands ------------------------------------------------------
    ("command", "empty"): (
        "Command description is missing. Add a frontmatter "
        "`description:` line — this is shown in slash-command help."
    ),
    ("command", "placeholder_text"): (
        "Command description is still a placeholder. Replace with a "
        "short summary of what the command does."
    ),
    ("command", "too_short"): (
        "Command description is too short (minimum {min_chars} "
        "characters). State the action clearly."
    ),
    ("command", "low_word_count"): (
        "Command description doesn't have enough distinct words. "
        "Rewrite as a real sentence describing the action."
    ),
    # ---- Submission-level (form field) --------------------------------
    ("submission", "empty"): (
        "Description is empty. This is the copy people see on the "
        "marketplace tile — say what you built and who it's for."
    ),
    ("submission", "placeholder_text"): (
        "Description is still a placeholder. Replace with a real "
        "sentence."
    ),
    ("submission", "too_short"): (
        "Description is too short (minimum {min_chars} characters). "
        "It carries the marketplace tile copy, so make the first "
        "sentence count."
    ),
    ("submission", "low_word_count"): (
        "Description doesn't have enough distinct words. Rewrite as "
        "a real sentence."
    ),
}
