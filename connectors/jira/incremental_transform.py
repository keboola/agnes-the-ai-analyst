"""
Incremental Jira transform - update single issue in Parquet files.

Called by webhook handler after issue JSON and attachments are saved.
Updates only the affected monthly Parquet file for efficient rsync.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# Import transform functions from batch transform
from .file_lock import parquet_month_lock
from .transform import (
    ATTACHMENTS_SCHEMA,
    CHANGELOG_SCHEMA,
    COMMENTS_SCHEMA,
    HIVE_PARTITION_PREFIX,
    ISSUELINKS_SCHEMA,
    REMOTE_LINKS_SCHEMA,
    apply_schema,
    get_month_key,
    issues_schema,
    transform_attachments,
    transform_changelog,
    transform_comments,
    transform_issue,
    transform_issuelinks,
    transform_remote_links,
    write_parquet_atomic,
)
from .validation import is_valid_issue_key, safe_join_under

logger = logging.getLogger(__name__)

# Default paths (can be overridden via environment)
# A month partition key: exactly `YYYY-MM`. Used to tell a real month partition
# from any other parquet sitting directly under a table directory.
_MONTH_KEY_PATTERN = re.compile(r"^\d{4}-\d{2}$")

DEFAULT_RAW_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "extracts" / "jira" / "raw"
DEFAULT_OUTPUT_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "extracts" / "jira" / "data"


def upsert_dataframe(
    existing_df: pd.DataFrame | None,
    new_records: list[dict],
    key_column: str,
    issue_key: str,
) -> pd.DataFrame:
    """
    Upsert new records into existing DataFrame.

    - Removes all rows matching issue_key
    - Adds new records

    Args:
        existing_df: Existing DataFrame (or None if new file)
        new_records: List of new records to add
        key_column: Column used for matching (e.g., 'issue_key')
        issue_key: Issue key to remove/replace

    Returns:
        Updated DataFrame
    """
    return upsert_dataframe_many(existing_df, new_records, key_column, [issue_key])


def upsert_dataframe_many(
    existing_df: pd.DataFrame | None,
    new_records: list[dict],
    key_column: str,
    issue_keys: list[str],
) -> pd.DataFrame:
    """Upsert several issues' records in one pass.

    Same contract as :func:`upsert_dataframe`, widened from one key to many:
    every row whose ``key_column`` is in *issue_keys* is dropped, then
    *new_records* is appended. That is what lets a month's whole batch land in a
    single read-modify-write instead of one per issue.

    ``issue_keys`` must list every issue the caller is replacing, INCLUDING any
    whose ``new_records`` came back empty — that is the deletion case, and
    deriving the key set from the records instead would silently skip it.
    """
    new_df = pd.DataFrame(new_records) if new_records else pd.DataFrame()

    if existing_df is None or existing_df.empty:
        return new_df

    keep = existing_df[~existing_df[key_column].isin(issue_keys)].copy()
    if new_df.empty:
        # Removing the issues from existing data (deletion case)
        return keep

    # Old records for these issues dropped above; add the new ones
    return pd.concat([keep, new_df], ignore_index=True)


def _hive_dir(parquet_dir: Path, month_key: str) -> Path:
    """Return the hive partition directory for a given month key."""
    return parquet_dir / f"{HIVE_PARTITION_PREFIX}={month_key}"


def _flat_path(parquet_dir: Path, month_key: str) -> Path:
    """Return the legacy flat parquet path for a given month key."""
    return parquet_dir / f"{month_key}.parquet"


def load_parquet_month(parquet_dir: Path, month_key: str) -> pd.DataFrame | None:
    """Load existing Parquet file for a month, or return None.

    Checks hive layout (``month=YYYY-MM/data.parquet``) first, then falls back
    to the legacy flat layout (``YYYY-MM.parquet``) for backward compatibility
    during the transition period.
    """
    hive_file = _hive_dir(parquet_dir, month_key) / "data.parquet"
    if hive_file.exists():
        return _read_or_raise(hive_file)

    # Backward-compat: flat file from before hive migration
    flat_file = _flat_path(parquet_dir, month_key)
    if flat_file.exists():
        return _read_or_raise(flat_file)
    return None


class UnreadablePartitionError(RuntimeError):
    """A partition file exists but could not be read.

    Distinct from "no partition", which is a legitimate empty state. See
    :func:`_read_or_raise` for why the difference has to be fatal.
    """


def _read_or_raise(path: Path) -> pd.DataFrame:
    """Read *path*, or raise — never answer "no data" for a file that is there.

    This used to `logger.warning(...)` and return ``None``, which conflated two
    very different answers: *"there is no partition"* (legitimately empty) and
    *"there is a partition and I cannot read it"* (unknown contents). Callers in
    `transform_single_issue` feed the result to `upsert_dataframe`, which treats
    ``None`` as an empty frame and returns only the record being upserted — so
    the whole month was republished with a SINGLE row, losing every other issue,
    behind nothing louder than a WARNING. Nothing restored them: the SLA poller
    revisits only tickets whose `status_category != 'Done'`.

    Atomic publishing (`write_parquet_atomic`) removes the most likely producer
    of an unreadable partition, but not the rest — disk errors, a truncated
    restore, a half-finished `rsync`, a filesystem where `os.replace` is not
    atomic (NFS, some overlay mounts). Those all arrive through this same path,
    so the read error has to stop the write rather than silently redefine it.

    Failing here costs one issue's transform, under the month lock its callers
    already hold; the operator repairs the partition (the raw JSON is intact, so
    a batch re-transform rebuilds it) instead of discovering months later that
    the history is gone.
    """
    try:
        return pd.read_parquet(path)
    except Exception as e:
        raise UnreadablePartitionError(
            f"{path} exists but could not be read ({e}). Refusing to treat it as empty — "
            f"overwriting would drop every row it holds. Rebuild it with the batch "
            f"re-transform: see connectors/jira/README.md, 'Batch Transform (Initial "
            f"Load / Recovery)' — --attachments-dir is required, or attachments history "
            f"loses local_path. Deleting the file instead has the next write republish "
            f"the month with only its own records."
        ) from e


def save_parquet_month(
    df: pd.DataFrame,
    schema: dict,
    output_dir: Path,
    month_key: str,
) -> Path:
    """Save DataFrame to the hive-partitioned monthly Parquet layout.

    Writes to ``output_dir/month=<month_key>/data.parquet`` with ZSTD
    compression and column statistics enabled.

    If a legacy flat file (``YYYY-MM.parquet``) exists for the same month it is
    removed after the hive write succeeds, completing the per-month migration.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    hive_dir = _hive_dir(output_dir, month_key)
    output_path = hive_dir / "data.parquet"

    if df.empty:
        # Don't write empty files; remove hive dir and legacy flat file if present.
        if hive_dir.exists():
            import shutil

            shutil.rmtree(hive_dir)
            logger.info(f"Removed empty hive dir {hive_dir}")
        flat = _flat_path(output_dir, month_key)
        if flat.exists():
            flat.unlink()
            logger.info(f"Removed legacy flat file {flat}")
        return output_path

    table = apply_schema(df, schema)
    # Atomic publish — readers glob this directory while it is being written.
    write_parquet_atomic(table, output_path)
    logger.info(f"Saved {len(df)} records to {output_path}")

    # Remove the legacy flat file now that hive layout is written.
    flat = _flat_path(output_dir, month_key)
    if flat.exists():
        flat.unlink()
        logger.info(f"Removed legacy flat file {flat} after hive migration")

    return output_path


def migrate_flat_to_hive(table_dir: Path) -> list[str]:
    """Migrate any remaining flat YYYY-MM.parquet files to hive layout.

    For each ``YYYY-MM.parquet`` found directly under *table_dir*, moves the
    file to ``month=YYYY-MM/data.parquet``.  Skips months that already have a
    hive directory.  Returns the list of month keys that were migrated.

    This is called during ``init_extract`` and after a batch transform run so
    that existing instances transparently transition to the new layout on the
    first webhook or scheduled sync after upgrade.

    Only files whose name really is a month are touched. The glob would otherwise
    treat any parquet directly under *table_dir* as a month and rename it to
    ``month=<stem>/data.parquet`` — so an unpartitioned dimension table
    (``organizations/data.parquet``) would become ``month=data/data.parquet`` and
    disappear from its own view, silently reporting zero rows. Callers are
    expected to skip such tables, but the contract stated above is enforced here
    so that a future caller cannot destroy one by omission.
    """
    migrated: list[str] = []

    for flat_file in sorted(table_dir.glob("*.parquet")):
        month_key = flat_file.stem  # e.g. "2026-01"
        if not _MONTH_KEY_PATTERN.match(month_key):
            logger.debug(
                "Skipping %s during flat->hive migration: %r is not a YYYY-MM month key",
                flat_file,
                month_key,
            )
            continue
        # Per-file fault isolation: a single file that can't be migrated must
        # not abort the whole pass (otherwise the months after it stay flat and
        # invisible to the hive-only view). Hold parquet_month_lock so a
        # concurrent incremental transform for the same month can't race the
        # rename.
        try:
            with parquet_month_lock(table_dir, month_key):
                hive_dir = _hive_dir(table_dir, month_key)
                if hive_dir.exists():
                    # Already migrated to hive — remove the redundant flat file
                    # so the recursive readers don't double-count this month.
                    flat_file.unlink()
                    logger.info("Removed redundant flat parquet %s (hive already present)", flat_file)
                    continue

                hive_dir.mkdir(parents=True, exist_ok=True)
                dest = hive_dir / "data.parquet"
                flat_file.rename(dest)
                logger.info("Migrated flat parquet %s -> %s", flat_file, dest)
                migrated.append(month_key)
        except Exception as exc:
            logger.error(
                "Failed to migrate flat parquet %s to hive: %s — leaving the flat "
                "file in place; it will be retried on the next init_extract",
                flat_file,
                exc,
            )
            continue

    return migrated


@dataclass
class _IssuePayload:
    """One issue's raw JSON, already transformed into per-table records.

    Built without touching a lock or a parquet file, so a batch can build many of
    these and land them together. The month is derived here, from the issue's own
    `created_at` — it is not something a caller can look up, because `month` is a
    hive DIRECTORY key and never a column in the file.
    """

    issue_key: str
    month_key: str
    created_at_missing: bool
    issue: dict
    comments: list[dict]
    comments_incomplete: bool
    attachments: list[dict]
    changelog: list[dict]
    issuelinks: list[dict]
    remote_links: list[dict] | None


def _build_issue_payload(issue_key: str, raw_dir: Path, attachments_dir: Path) -> _IssuePayload | None:
    """Read one issue's JSON and transform it. ``None`` if it cannot be used.

    Pure with respect to the parquet tree: no locks taken, nothing written. The
    batch path calls this *inside* the month lock so a webhook landing mid-run is
    either already visible here or still blocked on the lock and applies after us.
    """
    if not is_valid_issue_key(issue_key):
        logger.error(f"Refusing transform for malformed issue key: {issue_key!r}")
        return None
    try:
        json_path = safe_join_under(raw_dir / "issues", f"{issue_key}.json")
    except ValueError as e:
        logger.error(f"Path traversal blocked in transform for {issue_key!r}: {e}")
        return None
    if not json_path.exists():
        logger.error(f"Issue JSON not found: {json_path}")
        return None

    with open(json_path) as f:
        raw_issue = json.load(f)

    issue_record = transform_issue(raw_issue)
    issue_record["_raw_file"] = json_path.name
    created_at = issue_record.get("created_at")

    return _IssuePayload(
        issue_key=issue_key,
        month_key=get_month_key(created_at),
        created_at_missing=created_at is None,
        issue=issue_record,
        # Always the full transformed list. Whether it may be WRITTEN is the
        # separate `comments_incomplete` flag below — keeping them as two fields
        # rather than a None-means-incomplete list means the reader never has to
        # reconstruct which of two Optionals is live.
        comments=transform_comments(raw_issue, preserve_on_incomplete=False) or [],
        comments_incomplete=transform_comments(raw_issue) is None,
        attachments=transform_attachments(raw_issue, attachments_dir),
        changelog=transform_changelog(raw_issue),
        issuelinks=transform_issuelinks(raw_issue),
        remote_links=transform_remote_links(raw_issue),
    )


def _comment_records(payload: _IssuePayload, existing: pd.DataFrame | None) -> list[dict] | None:
    """Rows to write for this issue's comments, or ``None`` to preserve what is stored.

    `complete_issue_comments` marks an issue `_comments_incomplete` when it hit a
    page-fetch failure mid-pagination. The upsert is an issue-scoped
    delete-then-insert, so writing the known-truncated list would replace a
    previously-complete stored thread with a shorter one.

    Unless there is nothing to preserve: on a first fetch the marker would
    otherwise mean no comment row is ever written. `month_key` is only a genuine
    signal when `created_at` parsed — `get_month_key(None)` falls back to the
    CURRENT month, so probing an unrelated (empty) month would read "nothing
    stored" and write the partial list THERE while the real thread sits in the
    creation month; views glob `month=*`, so they would double-count.
    """
    if not payload.comments_incomplete:
        return payload.comments
    if payload.created_at_missing or _has_stored_rows(existing, payload.issue_key) or not payload.comments:
        logger.warning(
            f"Skipping comments upsert for {payload.issue_key}: pagination incomplete "
            f"(fetch failure). Existing rows preserved."
        )
        return None
    logger.warning(
        f"Writing {len(payload.comments)} partially-fetched comments for "
        f"{payload.issue_key}: pagination incomplete (fetch failure), but no stored rows "
        f"to preserve."
    )
    return payload.comments


def _remote_link_records(payload: _IssuePayload, _existing: pd.DataFrame | None) -> list[dict] | None:
    if payload.remote_links is None:
        # The writer (save_issue / backfill / backfill_remote_links) skipped the
        # _remote_links overlay due to a Jira fetch failure. Preserve the existing
        # parquet rows for this issue instead of wiping them.
        logger.warning(
            f"Skipping remote_links upsert for {payload.issue_key}: overlay absent "
            f"(fetch failure). Existing rows preserved."
        )
        return None
    return payload.remote_links


def _has_stored_rows(existing: pd.DataFrame | None, issue_key: str) -> bool:
    return (
        existing is not None
        and not existing.empty
        and "issue_key" in existing.columns
        and bool((existing["issue_key"] == issue_key).any())
    )


# Every table this transform maintains, as (name, schema, selector). The selector
# returns the rows to write for one issue, or None to leave that issue's stored
# rows alone — which is what makes the two "preserve on fetch failure" rules a
# per-issue decision without giving either table its own write path.
_TABLES = (
    ("issues", lambda p, _e: [p.issue]),
    ("comments", _comment_records),
    ("attachments", lambda p, _e: p.attachments),
    ("changelog", lambda p, _e: p.changelog),
    ("issuelinks", lambda p, _e: p.issuelinks),
    ("remote_links", _remote_link_records),
)


def _table_schemas() -> dict:
    return {
        "issues": issues_schema(),
        "comments": COMMENTS_SCHEMA,
        "attachments": ATTACHMENTS_SCHEMA,
        "changelog": CHANGELOG_SCHEMA,
        "issuelinks": ISSUELINKS_SCHEMA,
        "remote_links": REMOTE_LINKS_SCHEMA,
    }


def _apply_payloads(payloads: list[_IssuePayload], month_key: str, output_dir: Path) -> None:
    """Land every payload in *month_key*: one load and one save per table.

    The caller holds ``parquet_month_lock``. This is the single write path — both
    `transform_single_issue` (one payload) and `transform_issues` (many) go
    through it, so the per-issue rules cannot drift between them.

    `_meta` is deliberately NOT refreshed here — see
    `app.worker.kinds._run_jira_refresh`, which does it once per coalesced rebuild
    instead of once per event. Doing it per write cost a write-open of
    extract.duckdb plus a full count over every partition of all six tables;
    DuckDB is single-writer, and the same event enqueues a rebuild that ATTACHes
    that file, so the two raced — a lost ATTACH is only logged, and the rebuild
    then swaps in a freshly built analytics DB with no Jira views, so the tables
    vanish until a later rebuild wins. The parquet written here is queryable
    regardless: the extract views glob `month=*` per query.
    """
    if not payloads:
        return
    schemas = _table_schemas()

    for name, records_of in _TABLES:
        table_dir = output_dir / name
        existing = load_parquet_month(table_dir, month_key)
        keys: list[str] = []
        records: list[dict] = []
        for payload in payloads:
            rows = records_of(payload, existing)
            if rows is None:
                continue  # the selector logged why
            keys.append(payload.issue_key)
            records.extend(rows)
        if keys:
            save_parquet_month(
                upsert_dataframe_many(existing, records, "issue_key", keys),
                schemas[name],
                table_dir,
                month_key,
            )


def transform_issues(
    issue_keys: list[str],
    raw_dir: Path | None = None,
    output_dir: Path | None = None,
    attachments_dir: Path | None = None,
) -> list[str]:
    """Transform many issues, one read-modify-write per table per month.

    `transform_single_issue` is the right granularity for a webhook — one event,
    one issue. A caller holding a LIST of issues was calling it per key, which
    rewrites that key's whole month partition across all six tables. `poll_sla`
    (every open ticket, every cycle) is the caller this exists for;
    `consistency_check` has the same shape but still shells out to the CLI per
    key, so converting it is a separate change. A month
    holding N of them was therefore rewritten N times, re-emitting bytes the
    previous key had just written.

    The month is derived from each issue's own `created_at`, never supplied by the
    caller: `month` is a hive DIRECTORY key, so it is not a column any caller can
    read back, and a caller repairing missing rows has no parquet to read it from
    anyway.

    Returns the keys that were applied. A key whose JSON is missing or malformed is
    skipped and logged rather than sinking its batch.

    Lock discipline: takes ONLY ``parquet_month_lock``, never an issue lock inside
    it, since `file_lock.py` documents the nesting as issue (outer) -> month
    (inner) and the webhook path relies on it. Payloads are rebuilt *inside* the
    lock so a concurrent webhook cannot land between the read and the write.
    """
    raw_dir = raw_dir or DEFAULT_RAW_DIR
    output_dir = output_dir or DEFAULT_OUTPUT_DIR
    attachments_dir = attachments_dir or (raw_dir / "attachments")
    # De-duplicate first. `_apply_payloads` extends its record list once per
    # payload, so a key appearing twice is deleted once and re-inserted twice —
    # the function this replaces was idempotent under repetition and this must be
    # too. Repeats are not hypothetical: the same `issue_key` legitimately sits in
    # two partitions when `created_at` is missing (the current-month fallback
    # documented in `_comment_records`), and a half-finished flat->hive migration
    # lets `rglob("*.parquet")` see one month twice.
    issue_keys = list(dict.fromkeys(issue_keys))
    if not issue_keys:
        return []

    # Group first, on a throwaway pass, so each month is opened once. The payloads
    # built here are deliberately DISCARDED: the authoritative ones are rebuilt
    # under the month lock below, which is what keeps a concurrent webhook safe.
    # Re-reading a handful of KB of JSON is far cheaper than the rewrites this
    # avoids.
    by_month: dict[str, list[str]] = {}
    for issue_key in issue_keys:
        try:
            payload = _build_issue_payload(issue_key, raw_dir, attachments_dir)
        except Exception as e:
            logger.error(f"Error transforming {issue_key}: {e}", exc_info=True)
            continue
        if payload is None:
            continue
        by_month.setdefault(payload.month_key, []).append(issue_key)

    applied: list[str] = []
    for month_key in sorted(by_month):
        try:
            _apply_month(by_month[month_key], month_key, raw_dir, output_dir, attachments_dir, applied)
        except Exception as e:
            # Per-month fault isolation, the same contract `migrate_flat_to_hive`
            # states for its own pass. Without it an `UnreadablePartitionError` from
            # one corrupt partition propagates out of the whole run: every month
            # after it in sort order goes unwritten, AND the caller's coalesced
            # `jira-refresh` enqueue is skipped — so the months that DID write are
            # never announced to the catalog. One bad month costs that month.
            logger.error(f"Could not apply month {month_key}: {e}", exc_info=True)

    return applied


def _apply_month(
    issue_keys: list[str],
    month_key: str,
    raw_dir: Path,
    output_dir: Path,
    attachments_dir: Path,
    applied: list[str],
) -> None:
    """Rebuild one month's payloads under its lock and land them."""
    with parquet_month_lock(output_dir, month_key):
        payloads: list[_IssuePayload] = []
        for issue_key in issue_keys:
            try:
                payload = _build_issue_payload(issue_key, raw_dir, attachments_dir)
            except Exception as e:
                logger.error(f"Error transforming {issue_key}: {e}", exc_info=True)
                continue
            if payload is None:
                continue
            if payload.month_key != month_key:
                # Rare but reachable: `get_month_key(None)` falls back to the
                # CURRENT month, so an issue with an absent/unparseable
                # `created_at` can be grouped under one month in pass 1 and
                # another in pass 2 when a ~45-minute run straddles a month
                # boundary. Skipping is the safe direction — writing it here
                # would file it in the wrong hive directory, and the views glob
                # `month=*`, so it would double-count. Say so rather than
                # letting it vanish into the applied/refreshed delta.
                logger.warning(
                    f"Skipping {issue_key}: grouped under {month_key} but its "
                    f"created_at now resolves to {payload.month_key}. It will be "
                    f"picked up by its own month on the next pass."
                )
                continue
            payloads.append(payload)
            applied.append(issue_key)
        _apply_payloads(payloads, month_key, output_dir)
    logger.info(f"Applied {len(payloads)} issue(s) to month {month_key} in one pass")


def transform_single_issue(
    issue_key: str,
    raw_dir: Path | None = None,
    output_dir: Path | None = None,
    attachments_dir: Path | None = None,
    deleted: bool = False,
) -> bool:
    """
    Transform a single issue and update monthly Parquet files.

    This is called by webhook handler after issue JSON is saved.
    Only updates the month that the issue belongs to.

    Args:
        issue_key: Jira issue key (e.g., "SUPPORT-1234")
        raw_dir: Directory with raw JSON files
        output_dir: Output directory for Parquet files
        attachments_dir: Directory with downloaded attachments
        deleted: If True, remove issue from Parquet (deletion event)

    Returns:
        True if successful, False otherwise
    """
    raw_dir = raw_dir or DEFAULT_RAW_DIR
    output_dir = output_dir or DEFAULT_OUTPUT_DIR
    attachments_dir = attachments_dir or (raw_dir / "attachments")

    # Defense-in-depth: even if a stale/legacy code path bypasses webhook
    # validation, the transform step will refuse a malformed key (issue #83).
    if not is_valid_issue_key(issue_key):
        logger.error(f"Refusing transform for malformed issue key: {issue_key!r}")
        return False
    issues_dir = raw_dir / "issues"
    try:
        json_path = safe_join_under(issues_dir, f"{issue_key}.json")
    except ValueError as e:
        logger.error(f"Path traversal blocked in transform for {issue_key!r}: {e}")
        return False

    if deleted:
        # For deletion, we need to find which month the issue was in
        # Check all monthly files - this is rare so OK to be slower
        logger.info(f"Processing deletion for {issue_key}")
        return _handle_deletion(issue_key, output_dir)

    if not json_path.exists():
        logger.error(f"Issue JSON not found: {json_path}")
        return False

    try:
        # One payload, then the shared apply path — the same one
        # `transform_issues` uses, so the per-issue rules for an
        # incomplete comment thread and an absent remote-links overlay cannot
        # drift between the single and batched callers.
        payload = _build_issue_payload(issue_key, raw_dir, attachments_dir)
        if payload is None:
            return False

        # Parquet read-modify-write under per-month lock to prevent
        # "last writer wins" race when concurrent webhooks touch the
        # same monthly partition (see issue #205).
        logger.info(f"Updating {issue_key} in month {payload.month_key}")
        with parquet_month_lock(output_dir, payload.month_key):
            _apply_payloads([payload], payload.month_key, output_dir)
        logger.info(f"Successfully updated {issue_key} in Parquet files")
        return True

    except Exception as e:
        logger.error(f"Error transforming {issue_key}: {e}", exc_info=True)
        return False


def _handle_deletion(
    issue_key: str,
    output_dir: Path,
) -> bool:
    """Handle issue deletion by removing from all monthly files."""
    found = False

    for table_name, schema in [
        ("issues", issues_schema()),
        ("comments", COMMENTS_SCHEMA),
        ("attachments", ATTACHMENTS_SCHEMA),
        ("changelog", CHANGELOG_SCHEMA),
        ("issuelinks", ISSUELINKS_SCHEMA),
        ("remote_links", REMOTE_LINKS_SCHEMA),
    ]:
        table_dir = output_dir / table_name
        if not table_dir.exists():
            continue

        # Collect all month keys from both hive dirs and legacy flat files.
        month_keys: set[str] = set()
        for hive_subdir in table_dir.glob(f"{HIVE_PARTITION_PREFIX}=*"):
            if hive_subdir.is_dir():
                month_keys.add(hive_subdir.name.split("=", 1)[1])
        for flat_file in table_dir.glob("*.parquet"):
            month_keys.add(flat_file.stem)

        for month_key in sorted(month_keys):
            parquet_file = _hive_dir(table_dir, month_key) / "data.parquet"
            if not parquet_file.exists():
                # Fall back to flat layout for backward compat
                parquet_file = _flat_path(table_dir, month_key)
            if not parquet_file.exists():
                continue
            try:
                with parquet_month_lock(output_dir, month_key):
                    df = pd.read_parquet(parquet_file)
                    if "issue_key" in df.columns and issue_key in df["issue_key"].values:
                        df = df[df["issue_key"] != issue_key]
                        save_parquet_month(df, schema, table_dir, month_key)

                        found = True
                        logger.info(f"Removed {issue_key} from {parquet_file}")
            except Exception as e:
                logger.warning(f"Error checking {parquet_file}: {e}")

    return found


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Incremental Jira transform")
    parser.add_argument("issue_key", help="Jira issue key (e.g., SUPPORT-1234)")
    parser.add_argument("--raw-dir", type=Path, help="Raw JSON directory")
    parser.add_argument("--output-dir", type=Path, help="Output Parquet directory")
    parser.add_argument("--attachments-dir", type=Path, help="Attachments directory")
    parser.add_argument("--deleted", action="store_true", help="Issue was deleted")

    args = parser.parse_args()

    success = transform_single_issue(
        issue_key=args.issue_key,
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        attachments_dir=args.attachments_dir,
        deleted=args.deleted,
    )

    exit(0 if success else 1)
