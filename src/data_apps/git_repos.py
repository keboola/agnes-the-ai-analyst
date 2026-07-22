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

LIVE_REF = "refs/heads/agnes-live"


def repo_path(slug: str) -> Path:
    return Path(os.environ.get("DATA_DIR", "/data")) / "apps" / "git" / f"{slug}.git"


def init_app_repo(slug: str) -> Path:
    p = repo_path(slug)
    if not (p / "HEAD").exists():
        p.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "-b", "main", str(p)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(p), "config", "http.receivepack", "true"], check=True, capture_output=True)
    return p


def resolve_ref(slug: str, ref: str = "HEAD") -> Optional[str]:
    r = subprocess.run(["git", "-C", str(repo_path(slug)), "rev-parse", ref], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def fast_forward_live(slug: str, sha: Optional[str] = None) -> str:
    target = sha or resolve_ref(slug, "main") or resolve_ref(slug, "HEAD")
    if not target:
        raise ValueError(f"app repo {slug} has no commits to deploy")
    subprocess.run(["git", "-C", str(repo_path(slug)), "update-ref", LIVE_REF, target], check=True, capture_output=True)
    return target
