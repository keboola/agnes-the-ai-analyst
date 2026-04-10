# Changelog

All notable changes to Agnes AI Data Analyst are documented in this file.

Format: [CalVer](https://calver.org/) `YYYY.MM.N` with channels `stable` and `dev`.

---

## stable-2026.04.1 (unreleased)

Multi-instance deployment and self-service setup.

### Added
- CalVer versioning with `stable` and `dev` release channels
- `/api/health` now returns `version`, `channel`, and `schema_version`
- Auto-generated JWT and session secrets with file persistence (`/data/state/.jwt_secret`)
- Pre-migration snapshot of `system.duckdb` before schema upgrades
- `POST /api/admin/configure` for headless data source configuration
- `POST /api/admin/discover-and-register` combined table discovery and registration
- `/setup` web wizard for first-time instance setup
- `scripts/smoke-test.sh` for post-deploy verification
- Smoke test job in CI (Docker-in-CI after every release)
- OpenAPI snapshot test for breaking change detection
- Custom connector mount support (`connectors/custom/`)
- Startup banner logging version, channel, and schema version
- Schema migration safety tests (idempotency, data preservation, snapshot)
- `CHANGELOG.md` and release notes template

### Breaking Changes
None.

### Migration Guide
No action required. Existing instances upgrade seamlessly.
