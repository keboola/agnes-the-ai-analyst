# Changelog

All notable changes to Agnes AI Data Analyst are documented in this file.

Format: [CalVer](https://calver.org/) `YYYY.MM.N` with channels `stable` and `dev`.

---

## [0.12.0] - 2026-04-26

### Added
- Corporate memory V1.5: audience-based distribution — `knowledge_items.audience` column; `GET /api/memory` filters by caller's group membership; `users.groups` JSON column (schema v10)
- `GET /api/memory/admin/contradictions` gains `exclude_personal: bool = True` (default) — personal item content is hidden from contradiction enrichment responses
- Confidence externalization — `corporate_memory.confidence` section in `instance.yaml` now configures base scores, modifier weights, and decay parameters at startup via `confidence.configure()`
- Exponential confidence decay (default); per-source-type floors: `admin_mandate` >= 0.50, `user_verification` >= 0.40

### Fixed
- Admin categories filter now generated dynamically from seeded data (removed hardcoded `Data Analysis / API / Performance` buttons)
- Dashboard stats URL corrected from `/api/corporate-memory/stats` to `/api/memory/stats`

### Breaking Changes
- `apply_decay()` signature changed: `decay_rate_monthly=` keyword argument removed; use `confidence.configure()` to set decay parameters
- Default decay model switched from linear (-0.02/month) to exponential (half-life 12 months). Existing items will score lower after upgrade — migration note: `admin_mandate` items are protected by a 0.50 floor; `user_verification` items by a 0.40 floor; other source types may see score reductions up to ~50% at 12 months
- DuckDB schema bumped v8 -> v10: adds `knowledge_items.audience VARCHAR` (v9) and `users.groups JSON` (v10); migration runs automatically on startup

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
