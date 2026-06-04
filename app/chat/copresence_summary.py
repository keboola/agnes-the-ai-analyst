"""SR-8 seed: a server-generated summary produced under the intersection
principal — never a raw transcript clone. v1 default seeds the co-session with
a one-line topic note derived from the source session's title only, so no
historical query result a low-grant invitee can't reproduce is leaked."""
from __future__ import annotations

import duckdb


def build_intersection_summary(
    source_session_id: str, participant_emails: list[str],
    conn: duckdb.DuckDBPyConnection,
) -> str:
    row = conn.execute(
        "SELECT title FROM chat_sessions WHERE id = ?", [source_session_id]
    ).fetchone()
    title = (row[0] if row else None) or "a previous session"
    who = ", ".join(participant_emails)
    return (
        f"This is a shared co-drive session forked from “{title}”. "
        f"Participants ({who}) share access limited to the intersection of "
        f"their grants. Prior results from the original session were not "
        f"carried over; re-run any query you need here."
    )
