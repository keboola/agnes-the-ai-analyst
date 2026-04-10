# Release Notes Template

Use this template when adding a new entry to `CHANGELOG.md`.

---

## stable-YYYY.MM.N

**Image:** `ghcr.io/keboola/agnes-the-ai-analyst:stable-YYYY.MM.N`
**Digest:** `sha256:...` (from `docker inspect --format='{{index .RepoDigests 0}}'`)
**Date:** YYYY-MM-DD

### Added
- Feature description

### Changed
- Change description

### Fixed
- Bug fix description

### Breaking Changes
- Description of breaking change
- **Migration guide:** Steps to upgrade from previous version

### Deprecated
- Description of deprecated feature (will be removed in YYYY.MM.N)

---

## Guidelines

- Every merge to `main` creates a new `stable-YYYY.MM.N` release
- Include the image digest for verification with `cosign verify`
- Breaking changes require `BREAKING:` prefix in commit message
- Migration guides must include exact commands or config changes
- If a release deprecates the previous stable, note it explicitly
