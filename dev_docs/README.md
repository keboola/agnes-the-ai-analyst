# Developer Documentation

This folder contains documentation for **developers and server administrators** only.

**⚠️ This folder is NOT synced to analyst machines** — it stays on the server and in the git repository only.

## Contents

### Server Administration
- `server.md` — data broker server configuration and management
- `disaster-recovery.md` — recovery procedures for server failures
- `security.md` — security audit report and hardening guidelines

### Application Development
- `desktop-app.md` — macOS desktop app architecture and development
- `telegram_bot.md` — Telegram notification bot technical docs
- `design-system.md` — UI/UX design system for web applications
- `insights.md` — Activity Center dashboard feature documentation
- `session_explore.md` — session exploration tooling

Jira webhook integration and server-side processing is documented in
[`../connectors/jira/README.md`](../connectors/jira/README.md).

## For Analysts

If you're an analyst looking for documentation on how to **use** the platform,
see the `docs/` folder instead — start at [`../docs/README.md`](../docs/README.md)
for the full index. Key entry points:

- `docs/QUICKSTART.md` — quick start guide
- `docs/DATA_SOURCES.md` — data sources and table definitions
- `docs/metrics/` — business metric definitions
- `docs/HOWTO/` — task-oriented analyst cookbook
