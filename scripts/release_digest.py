#!/usr/bin/env python3
"""Daily Slack digest of GitHub Releases — one summary message, not per-release spam.

Driven by ``.github/workflows/release-digest.yml`` (cron + workflow_dispatch).
The window is "everything since the previous successful digest run" (the
workflow computes ``SINCE`` from its own run history), so a skipped night is
caught up automatically and quiet days post nothing at all.

Stdlib-only on purpose — runs on a bare GitHub runner without installing the
project. Pure helpers (`filter_releases`, `build_message`) are unit-tested in
``tests/test_release_digest.py``; ``main()`` is the thin IO shell.

Env contract:
    GITHUB_REPOSITORY   owner/repo (provided by Actions)
    GITHUB_TOKEN        API token for the releases listing
    SINCE               ISO-8601 UTC timestamp; releases created after this are included
    SLACK_WEBHOOK_URL   Slack Incoming Webhook; when empty the script prints the
                        payload and exits 0 (dry-run — vendor-neutral default)
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from typing import Any, Dict, List

# Slack hard-caps a section text block at 3000 chars; stay well under it and
# keep the digest scannable.
MAX_HIGHLIGHTS = 14
MAX_BULLET_CHARS = 160

_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)")
_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")
_DIGEST_SECTIONS = ("Added", "Changed", "Fixed", "Removed")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_EMPH_RE = re.compile(r"(\*\*|__|`)")


def _iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def filter_releases(releases: List[Dict[str, Any]], since_iso: str) -> List[Dict[str, Any]]:
    """Releases created strictly after ``since_iso``, oldest first, drafts/prereleases out."""
    since = _iso(since_iso)
    picked = [r for r in releases if not r.get("draft") and not r.get("prerelease") and _iso(r["created_at"]) > since]
    return sorted(picked, key=lambda r: _iso(r["created_at"]))


def _clean_bullet(line: str) -> str:
    text = _MD_LINK_RE.sub(r"\1", line)
    text = _MD_EMPH_RE.sub("", text).strip()
    if len(text) > MAX_BULLET_CHARS:
        text = text[: MAX_BULLET_CHARS - 1].rstrip() + "…"
    return text


def extract_highlights(body: str) -> Dict[str, List[str]]:
    """First-level bullets from the release body, grouped by changelog section."""
    out: Dict[str, List[str]] = {}
    section = None
    for line in (body or "").splitlines():
        m = _HEADING_RE.match(line)
        if m:
            # Any ### heading switches context; only the four user-facing
            # changelog sections collect bullets (### Internal etc. reset to None).
            name = m.group(1)
            section = name if name in _DIGEST_SECTIONS else None
            continue
        if section is None:
            continue
        b = _BULLET_RE.match(line)
        if b and not line.startswith((" ", "\t")):
            out.setdefault(section, []).append(_clean_bullet(b.group(1)))
        elif line.startswith((" ", "\t")) and line.strip() and not b and out.get(section):
            # Wrapped continuation of the previous top-level bullet — join it so
            # multi-line CHANGELOG entries don't end mid-sentence. Nested sub-
            # bullets (indented "-"/"*") stay excluded.
            joined = out[section][-1].rstrip("…") + " " + line.strip()
            out[section][-1] = _clean_bullet(joined)
    return out


def build_message(releases: List[Dict[str, Any]], repo: str) -> Dict[str, Any]:
    """Slack Block Kit payload summarizing ``releases`` (assumed non-empty, oldest first)."""
    first, last = releases[0]["tag_name"], releases[-1]["tag_name"]
    span = last if first == last else f"{first} → {last}"
    header = f"🚀 {repo.split('/')[-1]}: {len(releases)} release{'s' if len(releases) > 1 else ''} ({span})"

    merged: Dict[str, List[str]] = {}
    for r in releases:
        for section, bullets in extract_highlights(r.get("body") or "").items():
            merged.setdefault(section, []).extend(bullets)

    lines: List[str] = []
    total = 0
    truncated = False
    for section in ("Added", "Changed", "Fixed", "Removed"):
        for bullet in merged.get(section, []):
            if total >= MAX_HIGHLIGHTS:
                truncated = True
                break
            lines.append(f"• *{section}*: {bullet}")
            total += 1
        if truncated:
            break
    if truncated:
        lines.append("_…and more — see the linked releases._")
    if not lines:
        lines.append("_No changelog bullets found in the release notes._")

    links = " · ".join(f"<https://github.com/{repo}/releases/tag/{r['tag_name']}|{r['tag_name']}>" for r in releases)
    footer = f"{links}\n<https://github.com/{repo}/blob/main/CHANGELOG.md|Full changelog>"

    return {
        "text": header,  # notification fallback
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": footer[:2900]}]},
        ],
    }


def _fetch_releases(repo: str, token: str) -> List[Dict[str, Any]]:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases?per_page=50",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "release-digest",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _post_slack(webhook: str, payload: Dict[str, Any]) -> None:
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Slack webhook returned {resp.status}")


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    since = os.environ["SINCE"]
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")

    releases = filter_releases(_fetch_releases(repo, token), since)
    if not releases:
        print(f"No releases since {since} — nothing to post.")
        return 0

    payload = build_message(releases, repo)
    print(f"{len(releases)} release(s) since {since}: {', '.join(r['tag_name'] for r in releases)}")
    if not webhook:
        print("SLACK_WEBHOOK_URL unset — dry run, payload below:")
        print(json.dumps(payload, indent=2))
        return 0
    _post_slack(webhook, payload)
    print("Posted to Slack.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
