"""Demo-seed helpers: populate a fresh instance with the bundled sample
content so a brand-new deployment has something to explore out of the box.

All seeders are idempotent — safe to call on every boot.
"""

import logging
from pathlib import Path

from src.repositories.metrics import MetricRepository

log = logging.getLogger(__name__)

_METRICS_DIR = Path(__file__).resolve().parent.parent / "docs" / "metrics"


def seed_metrics(conn) -> int:
    """Import bundled metric definitions from ``docs/metrics/``.

    Idempotent across boots: ``MetricsRepository.import_from_yaml`` upserts each
    metric keyed on its ``category/name`` id (``INSERT ... ON CONFLICT (id) DO
    UPDATE``), so re-running replaces rather than duplicating.

    Returns the number of metrics imported on this call.
    """
    count = MetricRepository(conn).import_from_yaml(_METRICS_DIR)
    log.info("seed_metrics: imported %d metric definitions from %s", count, _METRICS_DIR)
    return count
