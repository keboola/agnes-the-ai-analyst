"""Guard tests for the `/admin` dashboard signal registry
(`app/web/admin_signals.py`) and its resolution layer
(`app/services/admin_dashboard.py`).

What these pin, and why each earns its place:

(a) The three rules in `admin_signals`'s docstring are structural promises,
    not style notes. A zero-count row that renders, or an href that lands
    nowhere, is how a dashboard stops being read — and neither shows up in a
    behavioural test of any single page.
(b) One raising resolver must never take `/admin` down with it. This page is
    what an admin opens WHEN something is already wrong, so it is the worst
    possible page to have a fragile render path.
(c) Every declared href must resolve to a real route. The signal registry is
    a second place (after `admin_nav.py`) that hardcodes admin URLs, so it
    needs the same dead-link guard the sidebar has.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services import admin_dashboard
from app.web.admin_nav import ADMIN_NAV_SECTIONS
from app.web.admin_signals import (
    ADMIN_SIGNALS,
    SEVERITIES,
    ZONE_NEEDS_FIXING,
    ZONE_NEEDS_YOU,
    ZONES,
    Signal,
    SignalSpec,
    signals_for_zone,
)

ROUTER_SRC = Path("app/web/router.py").read_text(encoding="utf-8")
_ROUTE_RE = re.compile(r'@router\.get\("(/admin[^"]*)"')


def _declared_hrefs() -> list[str]:
    """Every ``/admin…`` string literal in the signal registry — the
    destinations, read statically.

    Static rather than by calling the resolvers: on an empty test DB every
    resolver correctly returns None, so a runtime scan would check nothing
    exactly when the guard matters most.
    """
    src = Path("app/web/admin_signals.py").read_text(encoding="utf-8")
    return re.findall(r'"(/admin[^"]*)"', src)


def _handler_source(path: str, span: int = 40) -> str:
    """The first ~`span` lines following the `@router.get("<path>")`
    decorator — enough to see the handler's signature, where FastAPI query
    parameters are declared."""
    lines = ROUTER_SRC.split("\n")
    needle = f'@router.get("{path}"'
    for i, line in enumerate(lines):
        if needle in line:
            return "\n".join(lines[i : i + span])
    return ""


def _known_admin_paths() -> set[str]:
    """Literal prefixes of every GET /admin route in the web router, plus the
    hrefs the sidebar already vouches for (its own guard proves those are
    real, so reusing them keeps this test from re-deriving the same set)."""
    paths = {m.split("{")[0].rstrip("/") for m in _ROUTE_RE.findall(ROUTER_SRC)}
    for section in ADMIN_NAV_SECTIONS:
        for item in section["items"]:
            paths.add(item["href"])
    return paths


class TestRegistryShape:
    def test_keys_are_unique(self) -> None:
        keys = [s.key for s in ADMIN_SIGNALS]
        assert len(keys) == len(set(keys)), keys

    def test_every_spec_declares_a_known_zone_and_severity(self) -> None:
        for spec in ADMIN_SIGNALS:
            assert spec.zone in ZONES, f"{spec.key}: unknown zone {spec.zone!r}"
            assert spec.severity in SEVERITIES, f"{spec.key}: unknown severity {spec.severity!r}"

    def test_both_zones_are_populated(self) -> None:
        """A zone that silently emptied would render as a permanent all-clear
        — the most dangerous failure mode this page has."""
        assert signals_for_zone(ZONE_NEEDS_YOU), "the decisions zone has no signals"
        assert signals_for_zone(ZONE_NEEDS_FIXING), "the breakage zone has no signals"

    def test_zone_membership_partitions_the_registry(self) -> None:
        covered = sum(len(signals_for_zone(z)) for z in ZONES)
        assert covered == len(ADMIN_SIGNALS), "a signal declares a zone no renderer walks"


class TestRuleThreeDestinationsAreReal:
    """Rule 3 — a row must land somewhere the work can actually be done."""

    def test_every_href_points_at_a_real_admin_route(self) -> None:
        """Scanned from the SOURCE, not from resolver return values.

        Calling the resolvers would make this vacuous on the empty test DB —
        they all correctly return None when there is nothing to report, so
        every href would go unchecked exactly when the guard matters. The
        static scan sees all of them regardless of instance state.
        """
        dead = []
        for href in _declared_hrefs():
            # Strip query/fragment: the ROUTE must exist; the filter on it is
            # the destination page's business.
            base = href.split("?")[0].split("#")[0].rstrip("/")
            if base not in _known_admin_paths():
                dead.append(href)
        assert not dead, f"signal(s) linking to a non-existent admin route: {dead}"

    def test_query_filters_are_read_by_their_destination(self) -> None:
        """A row's `?param=` must be one the destination actually reads.

        This exists because it already caught a live bug: the dead-letter-jobs
        row (since removed for a different reason) linked to
        `/admin/activity?resource=job:`, and that page reads `resource_prefix`
        — so the link landed on an unfiltered audit log. The count would have
        been right and the destination silently wrong, which is the failure
        mode rule 3 is meant to make impossible. The base-path scan above
        cannot see it: the route resolves fine, the filter is what does not.

        Note the shape of the check: it looks for a SERVER-side query
        parameter. Pages that hydrate their filters in JS from
        `location.search` (the Activity Center is one) declare nothing here,
        so any filtered link to such a page will fail this guard — which is
        the conservative outcome. Widen it deliberately if that day comes;
        do not weaken the regex to make a link pass.
        """
        unread = []
        for href in _declared_hrefs():
            if "?" not in href:
                continue
            path, query = href.split("?", 1)
            handler = _handler_source(path)
            assert handler, f"no handler found for {path} — the route parse drifted"
            for pair in query.split("&"):
                param = pair.split("=", 1)[0]
                # A FastAPI handler declares its query params as annotated
                # arguments, so the name appears followed by ':' or '='.
                if not re.search(rf"\b{re.escape(param)}\s*[:=]", handler):
                    unread.append((href, param))
        assert not unread, f"signal href(s) passing a filter the destination ignores: {unread}"

    def test_scan_covers_every_declared_signal(self) -> None:
        """Pairs with the scan above — it is only as good as its coverage.

        Matches on any ``/admin…`` string literal rather than on ``href="…"``
        specifically, because one resolver legitimately CONCATENATES its href
        (the submissions row appends a filter built from the same tuple its
        count queried — that is rule 2, not a smell). Anchoring on `href=`
        would have silently skipped it.
        """
        assert len(_declared_hrefs()) == len(ADMIN_SIGNALS), (
            "a signal's destination is not a literal /admin string — the dead-link scan above cannot see it"
        )


class TestRuleOneZeroRendersNothing:
    """Rule 1 — a resolver returns None, never Signal(count=0)."""

    def test_no_resolver_can_return_a_zero_count_signal(self) -> None:
        for spec in ADMIN_SIGNALS:
            signal = _force_signal(spec)
            if signal is not None:
                assert signal.count > 0, f"{spec.key} returned a zero-count Signal instead of None"

    def test_resolve_zone_drops_clear_signals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "app.web.admin_signals.ADMIN_SIGNALS",
            [
                SignalSpec("clear", "Clear", ZONE_NEEDS_YOU, "action", lambda: None),
                SignalSpec(
                    "loud",
                    "Loud",
                    ZONE_NEEDS_YOU,
                    "action",
                    lambda: Signal(count=3, href="/admin/users", blurb="x"),
                ),
            ],
        )
        rows = admin_dashboard.resolve_zone(ZONE_NEEDS_YOU)
        assert [r.key for r in rows] == ["loud"]


class TestRuleTwoCountAndFilterShareAConstant:
    """Rule 2 — the submission row's href filter is built from the same tuple
    its count queried, so the dashboard can never disagree with the page it
    opens."""

    def test_submission_href_filter_is_derived_not_retyped(self) -> None:
        """The invariant is that ONE constant feeds both the count and the
        href — asserted on the source, because the resolver returns None on an
        empty instance and a runtime check would pass vacuously forever.

        A hand-typed `?status=blocked_inline,blocked_llm,review_error` would
        look identical today and drift the first time the review set changes.
        """
        src = Path("app/web/admin_signals.py").read_text(encoding="utf-8")
        assert '",".join(_SUBMISSION_REVIEW_STATUSES)' in src, (
            "the submissions href no longer derives its filter from the tuple its count queried"
        )

    def test_the_two_uses_of_the_review_set_agree(self) -> None:
        """Runtime half of the pair, when the instance HAS submissions to
        count. Vacuous on an empty DB by design — the source assertion above
        is what holds then."""
        from app.web.admin_signals import _SUBMISSION_REVIEW_STATUSES

        spec = next(s for s in ADMIN_SIGNALS if s.key == "store_submissions")
        signal = _force_signal(spec)
        if signal is None:
            pytest.skip("no submissions in this instance — see the source guard above")
        assert signal.href.split("?", 1)[1] == "status=" + ",".join(_SUBMISSION_REVIEW_STATUSES)

    def test_blocked_inline_is_not_dropped_from_the_review_set(self) -> None:
        """The Rescan-flow state. Dropping it silently undercounts the queue —
        a real prior bug on the moderation hub."""
        from app.web.admin_signals import _SUBMISSION_REVIEW_STATUSES

        assert "blocked_inline" in _SUBMISSION_REVIEW_STATUSES


class TestResolverIsolation:
    def test_a_raising_resolver_degrades_to_one_failed_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom():
            raise RuntimeError("repo is on fire")

        monkeypatch.setattr(
            "app.web.admin_signals.ADMIN_SIGNALS",
            [
                SignalSpec("boom", "Boom", ZONE_NEEDS_FIXING, "error", boom),
                SignalSpec(
                    "fine",
                    "Fine",
                    ZONE_NEEDS_FIXING,
                    "error",
                    lambda: Signal(count=1, href="/admin/sync", blurb="x"),
                ),
            ],
        )
        rows = admin_dashboard.resolve_zone(ZONE_NEEDS_FIXING)
        by_key = {r.key: r for r in rows}
        assert by_key["boom"].failed is True
        assert by_key["boom"].count == 0
        # The healthy sibling still resolved — isolation, not a poisoned batch.
        assert by_key["fine"].failed is False
        assert by_key["fine"].count == 1

    def test_a_failed_row_never_reads_as_all_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A broken check must stay VISIBLE. Dropping it would leave an empty
        zone, which the page renders as "everything is running normally"."""

        def boom():
            raise RuntimeError("nope")

        monkeypatch.setattr(
            "app.web.admin_signals.ADMIN_SIGNALS",
            [SignalSpec("boom", "Boom", ZONE_NEEDS_FIXING, "error", boom)],
        )
        rows = admin_dashboard.resolve_zone(ZONE_NEEDS_FIXING)
        assert len(rows) == 1
        assert rows[0].as_dict()["blurb"] != ""
        assert rows[0].as_dict()["href"] is None


class TestZoneTwoCache:
    def test_second_call_is_served_from_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def counting():
            calls["n"] += 1
            return Signal(count=1, href="/admin/sync", blurb="x")

        monkeypatch.setattr(
            "app.web.admin_signals.ADMIN_SIGNALS",
            [SignalSpec("counted", "Counted", ZONE_NEEDS_FIXING, "error", counting)],
        )
        admin_dashboard.invalidate_cache()
        admin_dashboard.resolve_needs_fixing()
        admin_dashboard.resolve_needs_fixing()
        assert calls["n"] == 1, "zone-2 resolvers ran twice inside the TTL"
        admin_dashboard.invalidate_cache()

    def test_force_bypasses_the_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def counting():
            calls["n"] += 1
            return Signal(count=1, href="/admin/sync", blurb="x")

        monkeypatch.setattr(
            "app.web.admin_signals.ADMIN_SIGNALS",
            [SignalSpec("counted", "Counted", ZONE_NEEDS_FIXING, "error", counting)],
        )
        admin_dashboard.invalidate_cache()
        admin_dashboard.resolve_needs_fixing()
        admin_dashboard.resolve_needs_fixing(force=True)
        assert calls["n"] == 2
        admin_dashboard.invalidate_cache()


def _force_signal(spec: SignalSpec):
    """Resolve *spec* against whatever the test DB holds, tolerating an empty
    instance. Returns None when there is nothing to report OR when the repo
    isn't reachable in this test context — the destination/zero-count
    assertions above are then vacuous for that spec, which is correct: they
    describe the shape of a signal that HAS something to say."""
    try:
        return spec.resolve()
    except Exception:
        return None
