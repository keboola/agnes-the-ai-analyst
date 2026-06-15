"""First-boot seeding of default source connections (spec 2026-06-12 §3.4).

One-time: if no connection of a given source_type exists, seed it from
today's env vars / instance.yaml. Afterwards the registry is the sole
source of truth; a set-but-ignored env var earns a deprecation WARNING
(step 1 of the three-step env retirement).
"""

from __future__ import annotations

import logging
import os
import uuid

logger = logging.getLogger(__name__)


def _yaml_value(*path: str) -> str:
    try:
        from app.instance_config import get_value

        return str(get_value(*path, default="") or "")
    except Exception:
        return ""


def seed_default_connections() -> None:
    from src.connection_specs import validate_connection_config
    from src.repositories import source_connections_repo

    repo = source_connections_repo()

    # --- keboola ---
    stack_url = os.environ.get("KEBOOLA_STACK_URL", "") or _yaml_value("data_source", "keboola", "stack_url")
    existing = repo.list(source_type="keboola")
    if existing:
        if stack_url and all(r["config"].get("stack_url") != stack_url.rstrip("/") for r in existing):
            logger.warning(
                "KEBOOLA_STACK_URL is set but connections are managed in the "
                "registry (/admin/connections); the env value is ignored."
            )
    elif stack_url:
        cfg = validate_connection_config("keboola", {"stack_url": stack_url})
        repo.create(
            id=str(uuid.uuid4()),
            name="keboola",
            source_type="keboola",
            config=cfg,
            token_env="KEBOOLA_STORAGE_TOKEN",
            is_default=True,
            created_by="seed",
        )
        logger.info("Seeded default keboola connection from env/yaml")

    # --- bigquery ---
    project = os.environ.get("BIGQUERY_PROJECT", "") or _yaml_value("data_source", "bigquery", "project")
    if project and not repo.list(source_type="bigquery"):
        cfg = validate_connection_config(
            "bigquery",
            {
                "project": project,
                "location": os.environ.get("BIGQUERY_LOCATION", "")
                or _yaml_value("data_source", "bigquery", "location")
                or "us",
            },
        )
        billing = _yaml_value("data_source", "bigquery", "billing_project")
        if billing:
            cfg["billing_project"] = billing
        repo.create(
            id=str(uuid.uuid4()),
            name="bigquery",
            source_type="bigquery",
            config=cfg,
            is_default=True,
            created_by="seed",
        )
        logger.info("Seeded default bigquery connection from env/yaml")
