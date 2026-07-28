"""Per-user aggregated Claude Code marketplace endpoint.

Two channels serve the same filtered content:
  - /marketplace.zip            (Bearer-authenticated ZIP download)
  - /marketplace.git/*          (git smart-HTTP with HTTP Basic; password = PAT)

Both route through `src.marketplace_filter.resolve_allowed_plugins` and share
the same content-addressed ETag so a git fetch is a no-op when nothing changed.
"""
