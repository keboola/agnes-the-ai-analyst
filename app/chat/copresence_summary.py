"""SR-8 seed: a server-generated summary produced under the intersection
principal — never a raw transcript clone. v1 default seeds the co-session with
a one-line topic note derived from the source session's title only, so no
historical query result a low-grant invitee can't reproduce is leaked."""
from __future__ import annotations


def build_intersection_summary(
    source_session_id: str, participant_emails: list[str],
) -> str:
    from src.repositories import chat_session_repo

    # Resolved through the factory (not a raw DuckDB conn) so the source
    # session's title is read from whichever backend the deployment runs on.
    session = chat_session_repo().get_session(source_session_id)
    title = (session.title if session else None) or "a previous session"
    who = ", ".join(participant_emails)
    return (
        f"This is a shared co-drive session forked from “{title}”. "
        f"Participants ({who}) share access limited to the intersection of "
        f"their grants. Prior results from the original session were not "
        f"carried over; re-run any query you need here."
    )
