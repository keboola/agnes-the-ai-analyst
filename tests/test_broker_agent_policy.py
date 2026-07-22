"""Unit tests for ``app/api/broker_agent_policy.py`` — the pure-logic half of
Task 8 (per-agent model policy, usage parsing, budget math, batched
usage-ledger accumulator). No HTTP/DB fixtures: these exercise the module
functions directly, monkeypatching the repo/coordination seams.
"""

from __future__ import annotations

import json


from app.api import broker_agent_policy as pol


# ---------------------------------------------------------------------------
# check_model
# ---------------------------------------------------------------------------


def test_check_model_agent_model_allowed():
    agent_row = {"model": "claude-opus-4-7"}
    body = json.dumps({"model": "claude-opus-4-7"}).encode()
    assert pol.check_model(body, agent_row, [], pol.INSTANCE_DEFAULT_MODEL) is None


def test_check_model_utility_model_allowed():
    agent_row = {"model": "claude-opus-4-7"}
    body = json.dumps({"model": "claude-haiku-4-5-20251001"}).encode()
    err = pol.check_model(body, agent_row, ["claude-haiku-4-5-20251001"], pol.INSTANCE_DEFAULT_MODEL)
    assert err is None


def test_check_model_instance_default_when_agent_model_null():
    agent_row = {"model": None}
    body = json.dumps({"model": pol.INSTANCE_DEFAULT_MODEL}).encode()
    assert pol.check_model(body, agent_row, [], pol.INSTANCE_DEFAULT_MODEL) is None


def test_check_model_foreign_model_rejected():
    agent_row = {"model": "claude-opus-4-7"}
    body = json.dumps({"model": "some-other-vendor-model"}).encode()
    err = pol.check_model(body, agent_row, ["claude-haiku-4-5-20251001"], pol.INSTANCE_DEFAULT_MODEL)
    assert err == "model_not_allowed"


def test_check_model_malformed_body_passes():
    agent_row = {"model": "claude-opus-4-7"}
    assert pol.check_model(b"not json at all", agent_row, [], pol.INSTANCE_DEFAULT_MODEL) is None
    assert pol.check_model(b"", agent_row, [], pol.INSTANCE_DEFAULT_MODEL) is None


def test_check_model_missing_model_key_passes():
    agent_row = {"model": "claude-opus-4-7"}
    body = json.dumps({"messages": []}).encode()
    assert pol.check_model(body, agent_row, [], pol.INSTANCE_DEFAULT_MODEL) is None


def test_check_model_non_dict_json_passes():
    agent_row = {"model": "claude-opus-4-7"}
    assert pol.check_model(b"[1, 2, 3]", agent_row, [], pol.INSTANCE_DEFAULT_MODEL) is None


# ---------------------------------------------------------------------------
# parse_usage
# ---------------------------------------------------------------------------


def test_parse_usage_plain_json():
    body = json.dumps(
        {
            "id": "msg_1",
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
            },
        }
    ).encode()
    usage = pol.parse_usage(body, "application/json")
    assert usage == {
        "model": "claude-sonnet-4-6",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 3,
        "cache_creation_tokens": 2,
    }


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def test_parse_usage_sse_message_start_plus_two_deltas():
    body = (
        _sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 50,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 5,
                        "cache_read_input_tokens": 2,
                    },
                },
            },
        )
        + _sse_event("content_block_start", {"type": "content_block_start", "index": 0})
        + _sse_event("message_delta", {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 10}})
        + _sse_event(
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 15}},
        )
        + _sse_event("message_stop", {"type": "message_stop"})
    ).encode()
    usage = pol.parse_usage(body, "text/event-stream; charset=utf-8")
    assert usage == {
        "model": "claude-sonnet-4-6",
        "input_tokens": 50,
        "output_tokens": 1 + 10 + 15,
        "cache_read_tokens": 2,
        "cache_creation_tokens": 5,
    }


def test_parse_usage_garbage_returns_none():
    assert pol.parse_usage(b"not json at all", "application/json") is None
    assert pol.parse_usage(b"", "application/json") is None
    assert pol.parse_usage(b"garbage\n\nnot sse either", "text/event-stream") is None


def test_parse_usage_json_without_usage_key_returns_none():
    body = json.dumps({"model": "claude-sonnet-4-6"}).encode()
    assert pol.parse_usage(body, "application/json") is None


# ---------------------------------------------------------------------------
# check_budget
# ---------------------------------------------------------------------------


def test_check_budget_under():
    assert pol.check_budget({"token_budget_monthly": 1000}, 500) is None


def test_check_budget_at_limit():
    assert pol.check_budget({"token_budget_monthly": 1000}, 1000) == "budget_exhausted"


def test_check_budget_over():
    assert pol.check_budget({"token_budget_monthly": 1000}, 1500) == "budget_exhausted"


def test_check_budget_no_budget_configured():
    assert pol.check_budget({"token_budget_monthly": None}, 999_999) is None
    assert pol.check_budget({}, 999_999) is None


# ---------------------------------------------------------------------------
# cached_month_total
# ---------------------------------------------------------------------------


class _FakeCoordination:
    def __init__(self):
        self.kv = {}
        self.incr_calls = []
        self.unavailable = False

    def kv_get(self, key):
        if self.unavailable:
            raise pol.CoordinationUnavailable("down")
        return self.kv.get(key)

    def kv_set(self, key, value, *, ttl_s):
        if self.unavailable:
            raise pol.CoordinationUnavailable("down")
        self.kv[key] = value

    def incr(self, key, *, amount=1, ttl_s):
        if self.unavailable:
            raise pol.CoordinationUnavailable("down")
        self.incr_calls.append((key, amount))
        new_val = int(self.kv.get(key, "0")) + amount
        self.kv[key] = str(new_val)
        return new_val


class _FakeLlmUsageRepo:
    def __init__(self):
        self.batches = []
        self.month_total = 42

    def insert_batch(self, rows):
        self.batches.append(list(rows))

    def month_total_tokens(self, agent_id, year_month):
        return self.month_total


def test_cached_month_total_cache_miss_reads_repo(monkeypatch):
    fake_coord = _FakeCoordination()
    fake_repo = _FakeLlmUsageRepo()
    monkeypatch.setattr(pol, "coordination", lambda: fake_coord)
    monkeypatch.setattr(pol, "llm_usage_repo", lambda: fake_repo)

    total = pol.cached_month_total("agent-1", ttl_s=60)
    assert total == 42
    # second call hits the cache, not the repo again
    fake_repo.month_total = 999
    total2 = pol.cached_month_total("agent-1", ttl_s=60)
    assert total2 == 42


def test_cached_month_total_coordination_unavailable_falls_back(monkeypatch):
    fake_coord = _FakeCoordination()
    fake_coord.unavailable = True
    fake_repo = _FakeLlmUsageRepo()
    monkeypatch.setattr(pol, "coordination", lambda: fake_coord)
    monkeypatch.setattr(pol, "llm_usage_repo", lambda: fake_repo)

    total = pol.cached_month_total("agent-1", ttl_s=60)
    assert total == 42


# ---------------------------------------------------------------------------
# UsageAccumulator
# ---------------------------------------------------------------------------


def _usage_row(i=1):
    return {
        "id": f"row-{i}",
        "agent_id": "agent-1",
        "user_id": "user-1",
        "session_id": "sess-1",
        "model": "claude-sonnet-4-6",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


def test_accumulator_flushes_at_size_threshold(monkeypatch):
    fake_repo = _FakeLlmUsageRepo()
    fake_coord = _FakeCoordination()
    monkeypatch.setattr(pol, "llm_usage_repo", lambda: fake_repo)
    monkeypatch.setattr(pol, "coordination", lambda: fake_coord)

    acc = pol.UsageAccumulator(flush_size=3, flush_interval_s=3600)
    for i in range(2):
        acc.add(_usage_row(i))
    assert fake_repo.batches == []  # not yet at threshold

    acc.add(_usage_row(2))
    assert len(fake_repo.batches) == 1
    assert len(fake_repo.batches[0]) == 3


def test_accumulator_flushes_at_age_threshold(monkeypatch):
    fake_repo = _FakeLlmUsageRepo()
    fake_coord = _FakeCoordination()
    monkeypatch.setattr(pol, "llm_usage_repo", lambda: fake_repo)
    monkeypatch.setattr(pol, "coordination", lambda: fake_coord)

    clock = {"t": 1000.0}
    acc = pol.UsageAccumulator(flush_size=20, flush_interval_s=30, clock=lambda: clock["t"])
    acc.add(_usage_row(1))
    assert fake_repo.batches == []

    clock["t"] += 31
    acc.add(_usage_row(2))
    assert len(fake_repo.batches) == 1
    assert len(fake_repo.batches[0]) == 2


def test_accumulator_add_increments_budget_counter(monkeypatch):
    fake_repo = _FakeLlmUsageRepo()
    fake_coord = _FakeCoordination()
    monkeypatch.setattr(pol, "llm_usage_repo", lambda: fake_repo)
    monkeypatch.setattr(pol, "coordination", lambda: fake_coord)

    acc = pol.UsageAccumulator(flush_size=20, flush_interval_s=3600)
    acc.add(_usage_row(1), budget_ttl_s=60)

    assert len(fake_coord.incr_calls) == 1
    key, amount = fake_coord.incr_calls[0]
    assert key == pol.budget_cache_key("agent-1", pol._current_year_month())
    assert amount == 15  # input + output + cache_creation


def test_accumulator_add_coordination_unavailable_is_best_effort(monkeypatch):
    fake_repo = _FakeLlmUsageRepo()
    fake_coord = _FakeCoordination()
    fake_coord.unavailable = True
    monkeypatch.setattr(pol, "llm_usage_repo", lambda: fake_repo)
    monkeypatch.setattr(pol, "coordination", lambda: fake_coord)

    acc = pol.UsageAccumulator(flush_size=20, flush_interval_s=3600)
    # must not raise even though the coordination backend is down
    acc.add(_usage_row(1), budget_ttl_s=60)


def test_accumulator_flush_is_noop_when_empty(monkeypatch):
    fake_repo = _FakeLlmUsageRepo()
    monkeypatch.setattr(pol, "llm_usage_repo", lambda: fake_repo)

    acc = pol.UsageAccumulator()
    acc.flush()
    assert fake_repo.batches == []


def test_module_singleton_exists_and_is_flushable():
    assert isinstance(pol.usage_accumulator, pol.UsageAccumulator)
    pol.usage_accumulator.flush()  # must not raise even with an empty buffer
