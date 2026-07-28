# Wave 2-H — Signed-URL Distribution (WS F)

Implements spec §3.6 + §6 row F: **bucket mirror + manifest v2 signed URLs + `agnes pull` support + CI presign contract test**. Last implementation wave of the three-plane epic.

## Goal

When an object store is configured, the worker mirrors distribution parquets to a bucket prefix after each sync, the manifest adds short-TTL presigned GET URLs next to md5/size, and `agnes pull` prefers those URLs (falling back to the app-served `/api/data/{id}/download`). This moves download bandwidth off the single app NIC onto object storage at L tier. Without an object store (S/M default), nothing changes — the app-served path + Caddy `file_server` bypass remain the download path.

## Non-negotiable design decisions

- **Vendor-agnostic → S3-compatible only.** No GCS-/AWS-specific client in the core. One `ObjectStore` protocol with a single **S3-compatible implementation via `boto3`** (covers AWS S3, GCS's S3-interop endpoint, SeaweedFS, managed buckets — the exact set spec §6 "Q1 resolved" names). `boto3` is an **optional extra** (`pip install ".[distribution]"`), so the base install stays lean and the "no bundled object store" decision holds. Presigned URLs use boto3's battle-tested V4 signer — never hand-rolled signing.
- **Extracts tree contract unchanged.** The mirror READS the same `extracts/**/data/{id}.parquet` files `agnes pull` serves today; it never moves or rewrites them. Extracts stay the distribution artifact + rollback source of truth.
- **Do NOT converge with ducklake `data_path`.** `src/ducklake_session.py` hard-codes a local `Path(...).mkdir` before ATTACH; the bucket mirror and the ducklake data_path stay separate concerns this wave.
- **Additive, backward-compatible manifest.** Add `signed_url` (+ `signed_url_expires_at`) to per-table manifest entries only when a store is configured and `signed_urls` is on/auto. Old clients ignore unknown keys; new clients fall back when the key is absent. Keep field-naming consistent with the existing `md5`/`hash` convention.
- **`auto` default.** `distribution.signed_urls: auto|on|off` — `auto` = on when an object store is configured, off otherwise. `off` forces the app-served path even with a store configured (escape hatch).
- **Idempotent mirror keyed on md5.** The mirror skips a parquet whose object already carries the current md5 (object metadata `x-amz-meta-md5` / HEAD compare) — no new DB state, no repo/parity burden.
- **RBAC parity.** A signed_url appears in a table's manifest entry only if the caller can already download that table (same `get_accessible_tables` / `can_access_table` gate). Signed URLs never widen access; TTL ≈ 15 min bounds leakage.
- **SSRF-safe client fetch.** `agnes pull` fetching a signed URL reuses the SSRF-guarded fetch pattern from `src/marketplace_asset_mirror.py`; on ANY failure (network, 403 expired, md5 mismatch) it falls back to `/api/data/{id}/download` and still md5-verifies.

## Tasks

### WF-1 — ObjectStore seam + S3 impl + `distribution` config
- `src/object_store.py`: `ObjectStore` Protocol (`presign_get(key, ttl_s) -> str`, `put_file(local_path, key, md5) -> None`, `head_md5(key) -> str|None`) + `S3ObjectStore` (boto3 client from `distribution.object_store.{endpoint_url,bucket,prefix,region,access_key_env,secret_key_env}`). Factory `object_store()` returns the configured store or `None`.
- `boto3` as optional extra in `pyproject.toml` (`[project.optional-dependencies] distribution = ["boto3>=1.34"]`); import guarded so absence → clean "distribution extra not installed" error only when actually used.
- Config helpers in `app/instance_config.py`: `distribution_signed_urls_mode()` (auto|on|off), `distribution_object_store_config()`. Env overrides `AGNES_DISTRIBUTION_*`.
- Tests: config resolution (auto/on/off + env wins), S3ObjectStore presign URL shape against a stub/moto-free fake, missing-boto3 error path. Guard test: `signed_urls=off` or no store → `object_store()` None.

### WF-2 — Manifest v2 presigned URLs
- `app/api/sync.py`: in `_table_manifest_entry` / the flat-dict builder, when `object_store()` is present and mode on/auto, add `signed_url` + `signed_url_expires_at` for downloadable local/materialized tables (skip `remote`, skip `server_only`). Presign key = `{prefix}/{table_id}.parquet`.
- Only presign for tables the caller can access (already RBAC-filtered upstream — assert no widening).
- Contract test: manifest with store configured carries `signed_url` for a local table, omits it for `remote`/`server_only`, and omits entirely when `signed_urls=off`. TTL within expected bound.

### WF-3 — Bucket mirror (post-sync LIGHT job)
- New LIGHT job kind `distribution-mirror` (`app/worker/kinds.py`), chained off `data-refresh` completion (enqueue on success, like the wave-2G maintenance pattern) — mirrors every downloadable parquet whose md5 differs from the object's `head_md5`. No store / mode off → clean no-op.
- Idempotent + resumable; per-file failure logs + continues (partial mirror is safe — manifest presign only emits for objects that exist, else the client falls back).
- m-tier wiring: worker role only. Metrics: reuse observability counters (mirror uploads, bytes, skips).
- Tests: mirror uploads changed files, skips md5-matches, no-op on legacy/no-store, worker-role gating.

### WF-4 — `agnes pull` prefers signed URL
- `cli/lib/pull.py`: when a manifest entry has `signed_url`, fetch it directly (SSRF-safe) into the existing sidecar→md5-verify→atomic-promote flow; on any failure fall back to `/api/data/{id}/download`. md5 verification is unconditional (already present). Respect `--scope`/existing skip rules.
- `[scope]`-style stderr note when a download used the signed URL vs the app path (observability parity with the command-UX standard).
- Tests: prefers url when present, falls back on 403/expired/mismatch, md5-verify still gates, absent url → unchanged behavior.

### WF-5 — CI presign contract test + docs + CHANGELOG + full suite
- End-to-end contract test (fake in-process S3 or a `moto`-free stub honoring the `ObjectStore` protocol): configure store → run mirror → manifest carries url → pull uses url → forced-failure falls back → md5 gate holds.
- Docs: `docs/DEPLOYMENT.md` object-store config block (`distribution.signed_urls`, `distribution.object_store.*`, the `[distribution]` extra), the SeaweedFS (Apache-2.0) / managed-bucket guidance from spec §6 Q1, and the load-test note (signed-URL path is the L-tier bandwidth offload; S/M stay on the file-server bypass). `docs/architecture.md` distribution section update. `config/instance.yaml.example` `distribution:` block.
- CHANGELOG bullet under `[Unreleased]`. Full suite green (`pytest -n auto`), ratchet + schema-version gates.

## Out of scope
- ducklake `data_path` on object storage (separate FS-coupling removal).
- Marketplace zip / corpus reads over signed URLs (interfaces only, per spec §3.6).
- GCS-/Azure-native SDKs (S3-compatible endpoint covers GCS via interop).
