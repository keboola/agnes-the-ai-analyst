# Security Policy

## Reporting a vulnerability

Please **do not** report security vulnerabilities through public GitHub
issues, discussions, or pull requests.

Instead, use GitHub's private vulnerability reporting for this repository:
**Security → Advisories → "Report a vulnerability"**
(<https://github.com/keboola/agnes-the-ai-analyst/security/advisories/new>).

Include as much of the following as you can:

- The affected component (endpoint, connector, CLI command, template, …) and
  the commit or release version you tested.
- Reproduction steps or a proof of concept.
- The impact you believe it has, and any deployment assumptions it depends on
  (e.g. reverse-proxy TLS termination, single-tenant vs. multi-group RBAC).

We aim to acknowledge reports within five business days. Please allow us a
reasonable window to remediate before public disclosure; we will credit
reporters in the advisory unless you prefer otherwise.

## Supported versions

Agnes is pre-1.0 and released continuously. Security fixes land on `main` and
ship in the next release (the `:stable` image tag and the corresponding
`v0.X.Y` tag); older releases do not receive backports. Self-hosted operators
should track `:stable`.

## Scope notes

- Agnes is designed to run behind a TLS-terminating reverse proxy. Reports
  that require plaintext-HTTP deployment or an attacker already inside the
  deployment boundary should state that assumption explicitly.
- Application admins are trusted in the current threat model (they hold
  god-mode via the `Admin` group), so "admin can do X" is usually not a
  privilege-boundary crossing on its own — defense-in-depth gaps are still
  welcome reports.
- Contributor-facing secure-coding rules live in
  `.claude/skills/agnes-conventions/references/security.md`.
