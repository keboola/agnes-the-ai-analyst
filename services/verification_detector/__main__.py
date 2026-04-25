"""CLI entry point for the verification detector service.

Usage:
    python -m services.verification_detector [--dry-run] [--verbose] [--reset]

TODO(scheduler-v2): Trigger is manual-only today (CLI). Wire into
services/scheduler/__main__.py JOBS list (e.g. hourly) and expose an admin
endpoint /api/admin/run-verification that calls detector.run() so the
scheduler stays the single source of truth for cadence.

TODO(notifications): When new pending items land in knowledge_items via
detector.run(), there is no admin notification. Hook into services/telegram_bot
or email so km_admins are pinged with a digest of pending items to triage.
"""

import argparse
import logging
import sys

from src.db import get_system_db

from . import detector

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract verified organizational knowledge from analyst session transcripts."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze sessions but do not write results to the database.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset session processing state before running.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Load AI config lazily (same pattern as corporate memory collector)
    try:
        from config.loader import load_instance_config
        config = load_instance_config()
        ai_config = config.get("ai")
        if not ai_config:
            logger.error("No ai: section in instance.yaml, cannot run verification detector")
            sys.exit(1)
    except (ValueError, FileNotFoundError) as e:
        logger.error("Failed to load config: %s", e)
        sys.exit(1)

    from connectors.llm import create_extractor
    extractor = create_extractor(ai_config)
    conn = get_system_db()

    if args.reset:
        logger.info("Resetting session extraction state...")
        conn.execute("DELETE FROM session_extraction_state")
        logger.info("Session extraction state cleared.")

    stats = detector.run(conn, extractor, dry_run=args.dry_run)

    print("\n--- Verification Detector Summary ---")
    print(f"Sessions scanned:        {stats['sessions_scanned']}")
    print(f"Sessions processed:      {stats['sessions_processed']}")
    print(f"Sessions skipped:        {stats['sessions_skipped']}")
    print(f"Verifications extracted:  {stats['verifications_extracted']}")
    print(f"Items created:           {stats['items_created']}")
    if stats["errors"]:
        print(f"Errors:                  {len(stats['errors'])}")
        for err in stats["errors"]:
            print(f"  - {err}")
    if args.dry_run:
        print("\n(dry-run mode -- no changes were written)")

    if stats["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
