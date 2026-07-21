"""Builders for the upstream python-js runtime contract.

The runtime image reads /data/config.json (dataApp.git + dataApp.secrets) and
never sees the platform: DATA_LOADER_API_URL stays unset by design (spec §2).
"""

from __future__ import annotations

import json
import re

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
LIVE_BRANCH = "agnes-live"
NETWORK = "agnes-apps"
AGNES_INTERNAL_URL = "http://app:8000"


def build_config_json(app_row: dict, *, secrets: dict[str, str], clone_url: str, clone_token: str) -> dict:
    if app_row["repo_mode"] == "internal":
        git = {"repository": clone_url, "branch": LIVE_BRANCH, "username": "agnes", "#password": clone_token}
    else:
        git = {"repository": app_row["repo_url"], "branch": app_row["repo_branch"] or "main"}
    out_secrets = {f"#{k}": v for k, v in secrets.items()}
    out_secrets["AGNES_TOKEN"] = clone_token
    out_secrets["AGNES_URL"] = AGNES_INTERNAL_URL
    return {"dataApp": {"git": git, "secrets": out_secrets}}


def build_container_spec(app_row: dict, *, defaults: dict, data_dir: str) -> dict:
    slug = app_row["slug"]
    env = {k: str(v) for k, v in json.loads(app_row.get("env") or "{}").items()}
    env["AGNES_URL"] = AGNES_INTERNAL_URL
    env["AGNES_APP_ID"] = app_row["id"]
    image = defaults["runtime_image"]
    if app_row.get("runtime_tag"):
        image = image.rsplit(":", 1)[0] + ":" + app_row["runtime_tag"]
    return {
        "name": f"agnes-dataapp-{slug}",
        "image": image,
        "labels": {"agnes.data-app": app_row["id"]},
        "network": NETWORK,
        "config_dir": f"{data_dir}/apps/{slug}",
        "cache_volume": f"agnes-dataapp-cache-{slug}",
        "mem_limit": app_row.get("mem_limit") or defaults["default_mem_limit"],
        "cpus": float(app_row.get("cpu_limit") or defaults["default_cpus"]),
        "env": env,
    }
