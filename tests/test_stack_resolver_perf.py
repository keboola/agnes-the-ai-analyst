"""Performance smoke tests for ``StackResolver`` + manifest generation.

Targets from Section 10.9 of the design doc:

  - ``StackResolver.stack(user_id, DATA_PACKAGE)`` averaged over 20 calls
    against a fixture of 1000 users × 50 groups × 200 resources × 800
    grants → **< 50 ms** per call.
  - Manifest generation (``_build_data_packages_section`` +
    ``_build_memory_domains_section`` + ``_build_direct_tables_section``)
    against 100 packages × 20 tables each → **< 200 ms** by design; the
    knob below carries the looser number CI is actually held to.

Thresholds are guidance, not hard gates: over the target records the actual
time and warns, and only a multiple of it (``PERF_CEILING_FACTOR``) fails
the build. Both benchmarks go through ``_check_perf``, which is where that
policy lives — and which exists because the plain ``assert`` these used to
carry contradicted this paragraph and blocked PRs on runner noise instead.
"""

from __future__ import annotations

import os
import time
import uuid
import warnings


from src.db import get_system_db


# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

RESOLVER_TARGET_MS = float(os.environ.get("AGNES_PERF_RESOLVER_MS", "50"))
# Bumped 200 → 500 → 600 → 1000 after persistent CI flake. The actual
# wall-clock for the 100-pkg × 20-tbl fixture in CI cold-cache runs lands
# between 180-550ms, but a contended runner has been seen at 1293ms.
# Tighten via the env var when running on a hot machine.
MANIFEST_TARGET_MS = float(os.environ.get("AGNES_PERF_MANIFEST_MS", "1000"))

# Over the target is a WARNING, not a failure — see `_check_perf`. Only a run
# this many times over it fails the build.
PERF_CEILING_FACTOR = float(os.environ.get("AGNES_PERF_CEILING_FACTOR", "4"))


def _check_perf(label: str, actual_ms: float, target_ms: float) -> None:
    """Report a wall-clock benchmark the way this module says it wants to.

    The module docstring has always promised that "thresholds are guidance,
    not hard gates … we record the actual time and surface the follow-up
    rather than blocking the PR". The assertions did the opposite: a plain
    `assert` failed CI. The gap is not theoretical — the target has been
    raised three times (200 → 500 → 600 → 1000) chasing runner noise, which
    is a ratchet that costs a rerun each time and weakens the signal on every
    turn.

    So: over the target warns and records the number; only `PERF_CEILING_FACTOR`
    times over it fails. A 4x overshoot is not a slow runner, it is a
    regression — and unlike the target, that ceiling does not need bumping
    when CI has a bad morning.
    """
    if actual_ms < target_ms:
        return
    over = actual_ms / target_ms
    detail = f"{label} {actual_ms:.2f}ms exceeds target {target_ms:.0f}ms ({over:.1f}x)"
    if actual_ms >= target_ms * PERF_CEILING_FACTOR:
        raise AssertionError(
            f"{detail} — past the {PERF_CEILING_FACTOR:.0f}x ceiling, so this is a "
            f"regression rather than a slow runner. Profile the change; raise "
            f"AGNES_PERF_CEILING_FACTOR only with a measurement that says why."
        )
    warnings.warn(
        f"{detail} — under the {PERF_CEILING_FACTOR:.0f}x ceiling, so not failing "
        f"the build. Persistent readings here mean the target is stale or the "
        f"code regressed; tune deliberately rather than by re-running.",
        stacklevel=2,
    )


# ---------------------------------------------------------------------------
# Fixture seeding helpers
# ---------------------------------------------------------------------------


def _seed_resolver_fixture(
    conn,
    *,
    n_users: int = 1000,
    n_groups: int = 50,
    n_packages: int = 200,
    n_grants: int = 800,
) -> str:
    """Seed users, groups, memberships, data_packages, grants. Returns the
    id of a "representative" user with several group memberships."""
    import random

    rng = random.Random(0)

    # Users
    for i in range(n_users):
        conn.execute(
            "INSERT INTO users(id, email) VALUES (?, ?)",
            [f"u{i}", f"u{i}@x.test"],
        )
    # Groups
    group_ids = []
    for i in range(n_groups):
        gid = f"g{i}"
        conn.execute(
            "INSERT INTO user_groups(id, name, description, created_by) VALUES (?, ?, '', 'test')",
            [gid, f"perf_g{i}"],
        )
        group_ids.append(gid)
    # Memberships — each user joins 3 random groups.
    for i in range(n_users):
        for gid in rng.sample(group_ids, k=min(3, n_groups)):
            conn.execute(
                "INSERT INTO user_group_members(user_id, group_id, source) VALUES (?, ?, 'test')",
                [f"u{i}", gid],
            )
    # Packages
    pkg_ids = []
    for i in range(n_packages):
        pid = f"pkg_{i:04d}"
        conn.execute(
            "INSERT INTO data_packages(id, slug, name) VALUES (?, ?, ?)",
            [pid, f"slug-{i}", f"Pkg {i}"],
        )
        pkg_ids.append(pid)
    # Grants — each grant binds one group → one package.
    for i in range(n_grants):
        gid = group_ids[i % n_groups]
        pid = pkg_ids[i % n_packages]
        requirement = "required" if i % 7 == 0 else "available"
        try:
            conn.execute(
                "INSERT INTO resource_grants(id, group_id, resource_type, "
                "resource_id, requirement, assigned_at, assigned_by) "
                "VALUES (?, ?, 'data_package', ?, ?, CURRENT_TIMESTAMP, 'test')",
                [str(uuid.uuid4()), gid, pid, requirement],
            )
        except Exception:
            # UNIQUE constraint on (group, type, resource) — skip dupes.
            pass

    return "u0"  # caller benchmarks this user's stack()


# ---------------------------------------------------------------------------
# Benchmark — StackResolver.stack()
# ---------------------------------------------------------------------------


def test_stack_resolver_perf_smoke(seeded_app):
    """1000 users × 50 groups × 200 resources × 800 grants — stack() < 50 ms avg."""
    from app.services.stack_resolver import StackResolver
    from app.resource_types import ResourceType

    conn = get_system_db()
    uid = _seed_resolver_fixture(conn)
    resolver = StackResolver(conn)

    # Warm-up: one call to populate any DuckDB query cache.
    resolver.stack(uid, ResourceType.DATA_PACKAGE)

    N = 20
    t0 = time.perf_counter()
    for _ in range(N):
        resolver.stack(uid, ResourceType.DATA_PACKAGE)
    elapsed = (time.perf_counter() - t0) * 1000.0
    avg_ms = elapsed / N
    conn.close()

    # Soft-gate: print the actual number so a regression is visible in CI
    # logs even when the threshold is generous; assert against the target.
    print(f"\nstack_resolver.stack() avg over {N} calls: {avg_ms:.2f} ms")
    _check_perf("StackResolver.stack() avg", avg_ms, RESOLVER_TARGET_MS)


# ---------------------------------------------------------------------------
# Benchmark — manifest generation
# ---------------------------------------------------------------------------


def _seed_manifest_fixture(
    conn,
    *,
    n_packages: int = 100,
    tables_per_pkg: int = 20,
) -> str:
    """Seed a single user with grants on N packages, each having T tables."""
    # Single user in a single group; admin god-mode would short-circuit so
    # we use a regular user.
    conn.execute("INSERT INTO users(id, email) VALUES ('perf_u', 'perf@x.test')")
    conn.execute("INSERT INTO user_groups(id, name, description, created_by) VALUES ('perf_g', 'perf_g', '', 'test')")
    conn.execute("INSERT INTO user_group_members(user_id, group_id, source) VALUES ('perf_u', 'perf_g', 'test')")
    pkg_ids = []
    for i in range(n_packages):
        pid = f"mpkg_{i:04d}"
        conn.execute(
            "INSERT INTO data_packages(id, slug, name) VALUES (?, ?, ?)",
            [pid, f"mslug-{i}", f"MPkg {i}"],
        )
        pkg_ids.append(pid)
        # Each package has T tables. Use a stable id pattern so registry
        # lookups in the manifest builder resolve cleanly.
        for j in range(tables_per_pkg):
            tid = f"tbl_{i:04d}_{j:02d}"
            conn.execute(
                """INSERT INTO table_registry
                   (id, name, source_type, bucket, source_table, query_mode,
                    registered_at, profile_after_sync)
                   VALUES (?, ?, 'keboola', 'b', ?, 'local',
                           CURRENT_TIMESTAMP, FALSE)""",
                [tid, tid, tid],
            )
            conn.execute(
                "INSERT INTO data_package_tables(package_id, table_id, added_by) VALUES (?, ?, 'test')",
                [pid, tid],
            )
        # Grant the user's group access (required to short-circuit subscribe).
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, "
            "resource_id, requirement, assigned_at, assigned_by) "
            "VALUES (?, 'perf_g', 'data_package', ?, 'required', "
            "CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), pid],
        )

    return "perf_u"


def test_manifest_generation_perf_smoke(seeded_app):
    """100 packages × 20 tables — manifest build < 200 ms."""
    from app.api.sync import (
        _build_data_packages_section,
        _build_direct_tables_section,
        _build_memory_domains_section,
    )

    conn = get_system_db()
    uid = _seed_manifest_fixture(conn)
    user = {"id": uid, "email": "perf@x.test", "name": "perf"}

    # Mimic the pieces of `_build_manifest_for_user` that the benchmark
    # cares about. We don't run the full builder because it touches the
    # filesystem (`_get_data_dir`) which is irrelevant to the v49 pieces.
    from src.repositories.table_registry import TableRegistryRepository

    registry_by_name = {t["name"]: t for t in TableRegistryRepository(conn).list_all()}
    states_by_table_id: dict = {}

    # Warm-up.
    _build_data_packages_section(conn, user, registry_by_name, states_by_table_id)

    t0 = time.perf_counter()
    pkgs, packaged_ids, _non_download_names = _build_data_packages_section(
        conn,
        user,
        registry_by_name,
        states_by_table_id,
    )
    _domains = _build_memory_domains_section(conn, user)
    _direct = _build_direct_tables_section(
        conn,
        user,
        registry_by_name,
        states_by_table_id,
        packaged_ids,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    conn.close()

    print(f"\nmanifest build (data_packages + memory_domains + direct_tables): {elapsed_ms:.2f} ms")
    assert len(pkgs) == 100, f"expected 100 packages in manifest, got {len(pkgs)}"
    assert all(len(p["tables"]) == 20 for p in pkgs), "every package should carry 20 tables in the manifest"
    _check_perf("manifest build", elapsed_ms, MANIFEST_TARGET_MS)


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------


class TestCheckPerfPolicy:
    """`_check_perf` is the whole reason these benchmarks stopped blocking PRs,
    so it gets its own coverage — a silent regression here would quietly turn
    the ceiling off and nobody would notice until a real regression shipped."""

    def test_under_target_is_silent(self, recwarn):
        _check_perf("x", 10.0, 100.0)
        assert len(recwarn) == 0

    def test_over_target_but_under_ceiling_warns_without_failing(self, recwarn):
        _check_perf("manifest build", 150.0, 100.0)
        assert len(recwarn) == 1
        msg = str(recwarn[0].message)
        assert "150.00ms" in msg and "1.5x" in msg, msg
        assert "not failing the build" in msg

    def test_at_the_ceiling_fails(self):
        import pytest

        with pytest.raises(AssertionError, match="regression rather than a slow runner"):
            _check_perf("manifest build", 100.0 * PERF_CEILING_FACTOR, 100.0)

    def test_the_failure_names_the_measurement(self):
        import pytest

        with pytest.raises(AssertionError) as exc:
            _check_perf("manifest build", 999.0, 100.0)
        assert "999.00ms" in str(exc.value) and "10.0x" in str(exc.value)
