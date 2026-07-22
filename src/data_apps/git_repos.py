"""Persistent bare git repos for internal-mode data apps.

One bare repo per app slug at ``${DATA_DIR}/apps/git/<slug>.git``, served
over git smart-HTTP by ``app/api/data_apps_git.py`` and pushed to by
analysts. Deploys promote a commit to the ``agnes-live`` branch —
``fast_forward_live`` is what the deploy pipeline (Task 7) calls after a
push lands on the default branch, so the runtime container always clones a
pinned, deploy-gated ref rather than whatever the analyst last pushed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

from src.data_apps.spec import SLUG_RE

LIVE_REF = "refs/heads/agnes-live"


def repo_path(slug: str) -> Path:
    if not SLUG_RE.match(slug):
        raise ValueError(f"invalid data app slug: {slug!r}")
    return Path(os.environ.get("DATA_DIR", "/data")) / "apps" / "git" / f"{slug}.git"


def init_app_repo(slug: str) -> Path:
    p = repo_path(slug)
    if not (p / "HEAD").exists():
        p.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "-b", "main", str(p)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(p), "config", "http.receivepack", "true"], check=True, capture_output=True)
    return p


def resolve_ref(slug: str, ref: str = "HEAD") -> Optional[str]:
    # `--verify <ref>^{commit}` fails (non-zero exit) for an unborn/unresolvable
    # ref instead of `rev-parse`'s lenient bare-name echo (e.g. a fresh bare
    # repo's `HEAD` symbolic-refs to a branch with no commits yet — plain
    # `git rev-parse HEAD` there prints the literal string "HEAD" with exit 0,
    # which would otherwise look like a valid (but bogus) resolved sha).
    r = subprocess.run(
        ["git", "-C", str(repo_path(slug)), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def fast_forward_live(slug: str, sha: Optional[str] = None) -> str:
    target = sha or resolve_ref(slug, "main") or resolve_ref(slug, "HEAD")
    if not target:
        raise ValueError(f"app repo {slug} has no commits to deploy")
    subprocess.run(["git", "-C", str(repo_path(slug)), "update-ref", LIVE_REF, target], check=True, capture_output=True)
    return target
