"""One definition of "the same person's address", shared by every write path.

Agnes resolves accounts by email string. Two spellings of one address —
``Ada@Example.com`` from an admin-created row, ``ada@example.com`` from an
OAuth claim — become two accounts with two sets of group memberships, and the
one the person lands on depends on which provider they used. The read side
folds case (``users_repo().get_by_email_ci``); this is the write side, so new
rows stop adding to the pile.

Deliberately minimal: strip surrounding whitespace, lower-case. No local-part
rewriting (dot-stripping, ``+tag`` removal) — those are provider-specific
conventions, and applying them here would silently merge addresses that a mail
system treats as distinct people.
"""


def normalize_email(value: str | None) -> str:
    """Canonical storage form of an email address: stripped and lower-cased."""
    return (value or "").strip().lower()
