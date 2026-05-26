"""Verification detector standalone runner.

Used by ``docker compose run verification-detector`` and by the
scheduler entry that fires the verification processor outside of the
HTTP path. Constructs the VerificationProcessor and runs it through
the shared session-pipeline runner.
"""

import argparse
import logging
import sys

from app.logging_config import setup_logging
from services.session_pipeline.runner import run_processor
from services.session_processors.verification import build_verification_processor

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

    if args.reset:
        from src.repositories import session_processor_state_repo
        logger.info("Resetting verification processor state...")
        deleted = session_processor_state_repo().reset_processor(processor.name)
        logger.info("Cleared %d state rows", deleted)

    stats = run_processor(processor)

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
