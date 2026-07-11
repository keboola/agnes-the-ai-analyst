"""CLI entry point for ad-hoc local runs of the verification processor.

Usage:
    python -m services.verification_detector [--verbose] [--reset]

After the session-pipeline refactor the canonical execution path is the
admin endpoint POST /api/admin/run-session-processor?processor=verification
driven by the scheduler. This CLI shim is kept as a developer convenience
for running the verification flow against a local instance without going
through HTTP — it constructs the VerificationProcessor and runs it through
the shared runner.
"""

import argparse
import logging
import sys

from app.logging_config import setup_logging
from services.session_pipeline.runner import run_processor
from services.session_processors.verification import build_verification_processor
from src.db import get_system_db
from src.repositories import session_processor_state_repo

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract verified organizational knowledge from analyst session transcripts."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset the verification processor's session-processed state before running.",
    )
    args = parser.parse_args()

    setup_logging(__name__, level="DEBUG" if args.verbose else "INFO")

    try:
        processor = build_verification_processor()
    except (ValueError, FileNotFoundError) as e:
        logger.error(
            "Failed to initialize verification processor: %s. "
            "Configure ai: in instance.yaml or set ANTHROPIC_API_KEY / LLM_API_KEY.",
            e,
        )
        sys.exit(1)

    conn = get_system_db()

    if args.reset:
        logger.info("Resetting verification processor state...")
        # Routed through the factory (not a raw DELETE on the always-DuckDB
        # connection) so the reset hits the active backend.
        session_processor_state_repo().delete_for_processors([processor.name])

    stats = run_processor(conn, processor)

    print("\n--- Verification Processor Summary ---")
    print(f"Sessions scanned:        {stats['scanned']}")
    print(f"Sessions processed:      {stats['processed']}")
    print(f"Sessions skipped:        {stats['skipped']}")
    print(f"Items created:           {stats['items_extracted']}")
    if stats["errors"]:
        print(f"Errors:                  {stats['errors']}")
        for err in stats["errors_detail"]:
            print(f"  - {err}")

    if stats["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
