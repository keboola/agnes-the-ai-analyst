# Archived documentation

Historical artifacts kept for reference but **not maintained**. Nothing here is
current guidance — paths, line numbers, and APIs cited inside these files
reflect the state of the repo when they were written. For live docs, start at
[`../README.md`](../README.md).

## Contents

- **`superpowers/`** — implementation plans (`plans/`) and design specs
  (`specs/`) from past development sprints. Each file is a point-in-time
  planning artifact for a feature that has since shipped (or been dropped).
  Useful as a record of *why* something was built a certain way; useless as a
  guide to *what the code does now* — read the code and `CHANGELOG.md` for that.
- **`HACKATHON.md`** — condensed deploy + dev playbook written for a hackathon
  sprint. Superseded by [`../QUICKSTART.md`](../QUICKSTART.md),
  [`../DEPLOYMENT.md`](../DEPLOYMENT.md), and [`../ONBOARDING.md`](../ONBOARDING.md).
- **`NOTIFICATIONS.md`** — early spec for Telegram notifications via scripts +
  crontab. The feature shipped as a service; see
  [`../../dev_docs/telegram_bot.md`](../../dev_docs/telegram_bot.md).
- **`pd-ps-comments.md`** — review notes on the corporate-memory v1 branch.
  Accepted decisions live in [`../ADR-corporate-memory-v1.md`](../ADR-corporate-memory-v1.md).
- **`security-audit-2026-04.md`** — point-in-time security audit snapshot.
  Findings addressed in later releases; see `CHANGELOG.md`.

## Policy

Don't edit archived files to "fix" stale references — rewriting historical
planning artifacts is revisionist and loses the record of what was actually
decided when. If an archived doc is genuinely worthless, delete it; otherwise
leave it as-is.
