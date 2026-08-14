from __future__ import annotations

import itertools
import threading
from datetime import UTC, datetime, timedelta

import pytest

from reliability import (
    CacheState,
    CircuitSnapshot,
    CircuitState,
    FieldFreshnessPolicy,
    FreshnessContext,
    FreshnessStatus,
    ProviderHTTPError,
    ProviderAttempt,
    ProviderKey,
    ProviderRuntime,
    ProviderRuntimeConfig,
    ProviderUnavailableError,
    RuntimeState,
    evaluate_field_freshness,
    evaluate_snapshot_reliability,
    gate_snapshot_for_decision,
    required_fields_by_rule,
    required_fields_for_rules,
    summarize_instrument_coverage,
    triggered_evidence_usable,
)
from signals.engine import evaluate


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
PRICE_POLICY = FieldFreshnessPolicy(
    max_source_age_seconds=1_000,
    max_observation_age_seconds=600,
    aging_ratio=0.8,
    allow_observed_only=False,
    session_aware=True,
)
FUNDAMENTAL_POLICY = FieldFreshnessPolicy(
    max_source_age_seconds=None,
    max_observation_age_seconds=86_400,
    aging_ratio=0.8,
    allow_observed_only=True,
)


def _context(**changes):
    values = {
        "evaluated_at": NOW,
        "market_phase": "open",
        "expected_source_after": None,
    }
    values.update(changes)
    return FreshnessContext(**values)


def _metadata(
    *, source_age=100, observation_age=50, basis="source_event", provider="test"
):
    return {
        "provider": provider,
        "source_as_of": (NOW - timedelta(seconds=source_age)).isoformat(),
        "observed_at": (NOW - timedelta(seconds=observation_age)).isoformat(),
        "time_basis": basis,
    }


def _snapshot(price_meta=None, pe_meta=None):
    return {
        "ticker": "TEST",
        "market": "US",
        "price": 100.0,
        "pe_ttm": 10.0,
        "field_metadata": {
            "price": price_meta or _metadata(),
            "pe_ttm": pe_meta
            or {
                "provider": "test",
                "source_as_of": None,
                "observed_at": NOW.isoformat(),
                "time_basis": "observed_only",
            },
        },
        "quality_issues": [],
    }


@pytest.mark.parametrize(
    ("source_age", "expected"),
    [(100, FreshnessStatus.FRESH), (850, FreshnessStatus.AGING)],
)
def test_source_event_fresh_and_aging_states(source_age, expected):
    result = evaluate_field_freshness(
        "price",
        100.0,
        _metadata(source_age=source_age),
        PRICE_POLICY,
        _context(),
    )

    assert result.status is expected
    assert result.usable_for_signal is True
    assert result.source_age_seconds == source_age
    assert result.observation_age_seconds == 50
    assert result.time_basis.value == "source_event"


def test_stale_unknown_future_and_observed_only_are_distinct():
    stale = evaluate_field_freshness(
        "price", 100, _metadata(source_age=1_001), PRICE_POLICY, _context()
    )
    unknown = evaluate_field_freshness(
        "price",
        100,
        {"observed_at": NOW.isoformat(), "time_basis": "observed_only"},
        PRICE_POLICY,
        _context(),
    )
    future = evaluate_field_freshness(
        "price",
        100,
        _metadata(source_age=-301),
        PRICE_POLICY,
        _context(),
        future_tolerance_seconds=300,
    )
    observed = evaluate_field_freshness(
        "pe_ttm",
        10,
        {
            "observed_at": (NOW - timedelta(hours=1)).isoformat(),
            "time_basis": "observed_only",
            "provider": "test",
        },
        FUNDAMENTAL_POLICY,
        _context(),
    )

    assert stale.status is FreshnessStatus.STALE
    assert unknown.status is FreshnessStatus.UNKNOWN
    assert future.status is FreshnessStatus.FUTURE
    assert observed.status is FreshnessStatus.FRESH
    assert observed.timestamp_confidence == "observed_only"
    assert not stale.usable_for_signal
    assert not unknown.usable_for_signal
    assert not future.usable_for_signal


def test_observation_age_is_an_independent_staleness_axis():
    result = evaluate_field_freshness(
        "price",
        100,
        _metadata(source_age=10, observation_age=601),
        PRICE_POLICY,
        _context(),
    )

    assert result.status is FreshnessStatus.STALE
    assert "observation age" in result.reason


def test_latest_completed_session_is_fresh_preopen_but_stale_during_open():
    friday_close = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)
    monday_preopen = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    metadata = {
        "provider": "test",
        "source_as_of": friday_close.isoformat(),
        "observed_at": monday_preopen.isoformat(),
        "time_basis": "source_event",
    }
    preopen = evaluate_field_freshness(
        "price",
        100,
        metadata,
        PRICE_POLICY,
        FreshnessContext(
            evaluated_at=monday_preopen,
            market_phase="pre_open",
            expected_source_after=friday_close,
        ),
    )
    during_open = evaluate_field_freshness(
        "price",
        100,
        metadata,
        PRICE_POLICY,
        FreshnessContext(
            evaluated_at=datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            market_phase="open",
            expected_source_after=friday_close,
        ),
    )

    assert preopen.status is FreshnessStatus.FRESH
    assert preopen.freshness_reference == "session_watermark"
    assert preopen.budget_seconds is None
    assert preopen.expected_source_after == friday_close
    assert "watermark" in preopen.reason
    assert during_open.status is FreshnessStatus.STALE


def test_invalid_or_timezone_naive_timestamp_fails_closed_without_echoing_value():
    result = evaluate_field_freshness(
        "price",
        100,
        {
            "provider": "test",
            "source_as_of": "not-a-date?token=secret",
            "observed_at": "2026-08-10T12:00:00",
            "time_basis": "source_event",
        },
        PRICE_POLICY,
        _context(),
    )

    assert result.status is FreshnessStatus.UNKNOWN
    assert "secret" not in result.reason


def test_required_fields_are_derived_per_rule_and_as_a_union():
    instrument = {
        "sell_rules": [
            {"id": "price-stop", "type": "price_below", "value": 90},
            {"id": "valuation", "type": "pe_above", "value": 30},
        ],
        "buy_rules": [{"id": "quality", "type": "roe_above", "value": 15}],
    }

    per_rule = required_fields_by_rule(instrument)

    assert per_rule["price-stop"] == {"price"}
    assert per_rule["valuation"] == {"price", "pe_ttm"}
    assert per_rule["quality"] == {"price", "roe"}
    assert required_fields_for_rules(instrument) == {"price", "pe_ttm", "roe"}


def test_report_is_json_stable_and_coverage_has_no_zero_sample_percentage():
    report = evaluate_snapshot_reliability(
        _snapshot(),
        {"price", "pe_ttm"},
        {"price": PRICE_POLICY, "pe_ttm": FUNDAMENTAL_POLICY},
        _context(),
    )
    restored = type(report).model_validate_json(report.model_dump_json())

    coverage = summarize_instrument_coverage({"TEST": report}, ["TEST", "MISS"])
    empty = summarize_instrument_coverage({}, [])

    assert restored == report
    assert report.overall.value == "DEGRADED"
    assert report.full_coverage is True
    assert report.usable_for_trusted_silence is False
    assert coverage.fresh_coverage == 0
    assert coverage.unusable_tickers == ("TEST", "MISS")
    assert empty.fresh_coverage is None


def test_empty_required_fields_are_blind_not_vacuously_healthy():
    report = evaluate_snapshot_reliability(
        _snapshot(), set(), {}, _context()
    )

    assert report.overall.value == "BLIND"
    assert report.full_coverage is False
    assert report.usable_for_trusted_silence is False


def test_stale_if_error_is_context_only_and_never_signal_eligible():
    report = evaluate_snapshot_reliability(
        _snapshot(),
        {"price", "pe_ttm"},
        {"price": PRICE_POLICY, "pe_ttm": FUNDAMENTAL_POLICY},
        _context(),
        cache_state=CacheState.STALE_IF_ERROR,
    )

    assert report.overall.value == "BLIND"
    assert report.usable_for_signal is False
    assert all(not field.usable_for_signal for field in report.fields.values())


def _partial_report():
    stale_pe = {
        "provider": "test",
        "observed_at": (NOW - timedelta(days=2)).isoformat(),
        "time_basis": "observed_only",
    }
    return evaluate_snapshot_reliability(
        _snapshot(pe_meta=stale_pe),
        {"price", "pe_ttm"},
        {"price": PRICE_POLICY, "pe_ttm": FUNDAMENTAL_POLICY},
        _context(),
    )


def test_rule_level_gate_preserves_fresh_price_protection_with_stale_pe():
    raw = _snapshot(pe_meta={
        "provider": "test",
        "observed_at": (NOW - timedelta(days=2)).isoformat(),
        "time_basis": "observed_only",
    })
    report = _partial_report()
    gated = gate_snapshot_for_decision(raw, report, {"price", "pe_ttm"})
    rules = {
        "watchlist": {
            "TEST": {
                "name": "Test",
                "sell_rules": [
                    {"id": "price-protection", "type": "price_above", "value": 90},
                    {"id": "stale-pe", "type": "pe_above", "value": 5},
                ],
                "buy_rules": [],
            }
        }
    }

    result = evaluate("TEST", gated, rules)

    assert raw["pe_ttm"] == 10
    assert gated["price"] == 100
    assert gated["pe_ttm"] is None
    assert gated["context_values"]["pe_ttm"] == 10
    assert result["decision"] == "SELL_REVIEW"
    assert result["evidence"]["sell"][1]["status"] == "UNKNOWN"
    assert triggered_evidence_usable(result["rule_results"], report) is True


def test_stale_triggering_field_never_triggers_and_partial_silence_is_unknown():
    report = _partial_report()
    gated = gate_snapshot_for_decision(_snapshot(), report)
    stale_only = {
        "watchlist": {
            "TEST": {
                "name": "Test",
                "sell_rules": [{"type": "pe_above", "value": 5}],
                "buy_rules": [],
            }
        }
    }
    no_trigger_plus_unknown = {
        "watchlist": {
            "TEST": {
                "name": "Test",
                "sell_rules": [
                    {"type": "price_above", "value": 200},
                    {"type": "pe_above", "value": 5},
                ],
                "buy_rules": [],
            }
        }
    }

    stale_result = evaluate("TEST", gated, stale_only)
    silence_result = evaluate("TEST", gated, no_trigger_plus_unknown)

    assert stale_result["decision"] == "UNKNOWN"
    assert stale_result["sell"] == []
    assert silence_result["decision"] == "UNKNOWN"
    assert not triggered_evidence_usable(stale_result["rule_results"], report)


class MutableClock:
    def __init__(self, current=NOW):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)


def _runtime(clock, sleeps, **changes):
    values = {
        "max_attempts": 3,
        "base_backoff_seconds": 1,
        "max_backoff_seconds": 10,
        "max_retry_after_seconds": 60,
        "failure_threshold": 2,
        "open_seconds": 10,
        "fresh_cache_seconds": 5,
        "stale_if_error_seconds": 100,
    }
    values.update(changes)
    ticks = itertools.count()
    return ProviderRuntime(
        ProviderRuntimeConfig(**values),
        clock=clock,
        monotonic=lambda: float(next(ticks)),
        sleeper=sleeps.append,
        random_value=lambda: 0.5,
    )


KEY = ProviderKey(provider="demo", operation="quote", market="US")


def test_timeout_retries_with_bounded_full_jitter_then_succeeds():
    clock = MutableClock()
    sleeps = []
    runtime = _runtime(clock, sleeps)
    calls = 0

    def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("https://secret.example/?token=do-not-record")
        return {"price": 100}

    result = runtime.execute(KEY, flaky, idempotent=True)

    assert calls == 3
    assert sleeps == [0.5, 1.0]
    assert [item.failure_class for item in result.attempts] == [
        "timeout",
        "timeout",
        "none",
    ]
    assert "secret" not in str(result.attempts)


def test_429_honors_retry_after_and_nontransient_error_does_not_retry():
    clock = MutableClock()
    sleeps = []
    runtime = _runtime(clock, sleeps)
    calls = 0

    def rate_limited():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderHTTPError(429, "3")
        return 42

    assert runtime.execute(KEY, rate_limited, idempotent=True).value == 42
    assert sleeps == [3.0]

    permanent_calls = 0

    def invalid():
        nonlocal permanent_calls
        permanent_calls += 1
        raise ValueError("schema mismatch at https://secret.example")

    with pytest.raises(ProviderUnavailableError) as exc_info:
        runtime.execute(
            ProviderKey(provider="demo", operation="fundamentals", market="US"),
            invalid,
            idempotent=True,
        )
    assert permanent_calls == 1
    assert exc_info.value.attempts[0].failure_class == "invalid_response"
    assert "secret" not in str(exc_info.value)


def test_non_idempotent_transient_operation_is_never_retried():
    clock = MutableClock()
    sleeps = []
    runtime = _runtime(clock, sleeps)
    calls = 0

    def timeout():
        nonlocal calls
        calls += 1
        raise TimeoutError

    with pytest.raises(ProviderUnavailableError):
        runtime.execute(KEY, timeout, idempotent=False)

    assert calls == 1
    assert sleeps == []


def test_circuit_closed_open_half_open_closed_and_open_fast_failure():
    clock = MutableClock()
    sleeps = []
    runtime = _runtime(clock, sleeps, max_attempts=1)
    calls = 0

    def timeout():
        nonlocal calls
        calls += 1
        raise TimeoutError

    for _ in range(2):
        with pytest.raises(ProviderUnavailableError):
            runtime.execute(KEY, timeout, idempotent=True)

    assert runtime.circuit_for(KEY).state is CircuitState.OPEN
    with pytest.raises(ProviderUnavailableError) as blocked:
        runtime.execute(KEY, lambda: 99, idempotent=True)
    assert calls == 2
    assert blocked.value.attempts[0].outcome == "circuit_open"

    clock.advance(11)
    recovered = runtime.execute(KEY, lambda: 99, idempotent=True)

    assert recovered.value == 99
    assert recovered.circuit.state is CircuitState.CLOSED


def test_fresh_cache_and_bounded_stale_if_error_have_different_eligibility():
    clock = MutableClock()
    sleeps = []
    runtime = _runtime(clock, sleeps, max_attempts=1, failure_threshold=5)
    calls = 0

    def success():
        nonlocal calls
        calls += 1
        return {"price": 100}

    first = runtime.execute(KEY, success, idempotent=True, cache_identity="AAPL")
    fresh = runtime.execute(
        KEY, lambda: (_ for _ in ()).throw(AssertionError), idempotent=True,
        cache_identity="AAPL"
    )
    clock.advance(6)
    stale = runtime.execute(
        KEY,
        lambda: (_ for _ in ()).throw(TimeoutError()),
        idempotent=True,
        cache_identity="AAPL",
    )

    assert calls == 1
    assert first.cache_state is CacheState.MISS
    assert fresh.cache_state is CacheState.FRESH
    assert fresh.usable_for_signal is True
    assert stale.cache_state is CacheState.STALE_IF_ERROR
    assert stale.usable_for_signal is False
    assert stale.attempts[-1].outcome == "stale_fallback"


def test_runtime_state_is_serializable_and_health_requires_real_samples():
    clock = MutableClock()
    runtime = _runtime(
        clock,
        [],
        max_attempts=1,
        fresh_cache_seconds=0,
        stale_if_error_seconds=0,
    )
    empty = runtime.provider_health(KEY)
    for index in range(20):
        runtime.execute(
            KEY,
            lambda index=index: {"price": index},
            idempotent=True,
            cache_identity=str(index),
        )

    health = runtime.provider_health(KEY)
    state = runtime.export_state()
    restored = _runtime(clock, [])
    restored.import_state(state.model_dump(mode="json"))

    assert empty.sample_count == 0
    assert empty.success_rate is None
    assert empty.grade == "insufficient_data"
    assert health.sample_count == 20
    assert health.success_rate == 1
    assert health.wilson_lower_bound is not None
    assert restored.circuit_for(KEY).state is CircuitState.CLOSED
    assert state.model_dump_json()


@pytest.mark.parametrize(
    "changes",
    [
        {"failure_class": "timeout", "error_type": "timeout"},
        {"error_type": "timeout"},
        {"cache_state": "none"},
        {"circuit_state": "open"},
        {"outcome": "cache_hit", "cache_state": "fresh", "attempt_index": 1},
        {"outcome": "stale_fallback", "attempt_index": 0},
        {
            "outcome": "circuit_open",
            "failure_class": "circuit_open",
            "error_type": "circuit_open",
            "attempt_index": 0,
        },
        {"outcome": "transient_error"},
    ],
)
def test_provider_attempt_rejects_semantically_inconsistent_evidence(changes):
    values = {
        "provider": "probe",
        "operation": "quote",
        "market": "US",
        "outcome": "success",
        "failure_class": "none",
        "error_type": "none",
        "latency_ms": 1.0,
        "attempt_index": 1,
        "cache_state": "miss",
        "circuit_state": "closed",
        "observed_at": NOW,
    }
    values.update(changes)

    with pytest.raises(ValueError, match="provider attempt|error_type"):
        ProviderAttempt.model_validate(values)


@pytest.mark.parametrize("limit", ["keys", "caches", "per_key", "total"])
def test_runtime_state_rejects_persistence_limits(limit):
    attempt = ProviderAttempt(
        provider="probe",
        operation="quote",
        market="US",
        outcome="success",
        latency_ms=1.0,
        attempt_index=1,
        cache_state="miss",
        observed_at=NOW,
    )
    if limit == "keys":
        values = {
            "circuits": {
                f"provider{index}:quote:US": CircuitSnapshot()
                for index in range(257)
            }
        }
    elif limit == "caches":
        values = {
            "caches": {
                f"probe:quote:US:{index:024x}": {
                    "stored_at": NOW.isoformat(),
                    "value": index,
                }
                for index in range(10_001)
            }
        }
    elif limit == "per_key":
        values = {"observations": {"probe:quote:US": (attempt,) * 10_001}}
    else:
        observations = {
            f"provider{index}:quote:US": (attempt,) * 10_000
            for index in range(5)
        }
        observations["overflow:quote:US"] = (attempt,)
        values = {"observations": observations}

    with pytest.raises(ValueError, match="provider runtime"):
        RuntimeState.model_validate(values)


def test_retry_delay_does_not_make_new_cache_artificially_old():
    clock = MutableClock()
    sleeps = []

    def advance_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    ticks = itertools.count()
    runtime = ProviderRuntime(
        ProviderRuntimeConfig(
            max_attempts=2,
            base_backoff_seconds=1,
            max_backoff_seconds=10,
            failure_threshold=5,
            fresh_cache_seconds=5,
            stale_if_error_seconds=100,
        ),
        clock=clock,
        monotonic=lambda: float(next(ticks)),
        sleeper=advance_sleep,
        random_value=lambda: 0,
    )
    calls = 0

    def delayed_success():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderHTTPError(429, "5")
        return 42

    runtime.execute(KEY, delayed_success, idempotent=True, cache_identity="clock")
    clock.advance(4)
    cached = runtime.execute(
        KEY,
        lambda: (_ for _ in ()).throw(AssertionError),
        idempotent=True,
        cache_identity="clock",
    )

    assert sleeps == [5]
    assert cached.cache_state is CacheState.FRESH


def test_active_timeout_returns_and_hanging_call_keeps_bulkhead_slot():
    clock = MutableClock()
    release = threading.Event()
    calls = 0
    runtime = ProviderRuntime(
        ProviderRuntimeConfig(
            request_timeout_seconds=0.02,
            bulkhead_max_calls=1,
            max_attempts=3,
            failure_threshold=10,
            fresh_cache_seconds=0,
            stale_if_error_seconds=0,
        ),
        clock=clock,
        sleeper=lambda _seconds: pytest.fail("in-flight timeout must not retry"),
    )

    def hangs():
        nonlocal calls
        calls += 1
        release.wait(1)
        return 1

    try:
        with pytest.raises(ProviderUnavailableError) as timed_out:
            runtime.execute(KEY, hangs, idempotent=True, cache_identity="hang-1")
        with pytest.raises(ProviderUnavailableError) as bulkhead:
            runtime.execute(KEY, hangs, idempotent=True, cache_identity="hang-2")
    finally:
        release.set()

    assert calls == 1
    assert len(timed_out.value.attempts) == 1
    assert timed_out.value.attempts[0].failure_class == "timeout"
    assert bulkhead.value.attempts[0].failure_class == "bulkhead_full"


def test_half_open_admission_allows_only_one_concurrent_probe():
    clock = MutableClock()
    runtime = _runtime(
        clock,
        [],
        max_attempts=1,
        failure_threshold=1,
        request_timeout_seconds=1,
        bulkhead_max_calls=2,
    )
    with pytest.raises(ProviderUnavailableError):
        runtime.execute(KEY, lambda: (_ for _ in ()).throw(TimeoutError()), idempotent=True)
    clock.advance(11)

    entered = threading.Event()
    release = threading.Event()
    results = []

    def probe():
        entered.set()
        release.wait(1)
        return 99

    def run_probe():
        results.append(runtime.execute(KEY, probe, idempotent=True))

    thread = threading.Thread(target=run_probe)
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(ProviderUnavailableError) as blocked:
            runtime.execute(KEY, lambda: 100, idempotent=True)
        assert blocked.value.attempts[0].outcome == "circuit_open"
    finally:
        release.set()
        thread.join(1)

    assert len(results) == 1
    assert results[0].circuit.state is CircuitState.CLOSED


def test_concurrent_terminal_failures_increment_circuit_without_lost_update():
    clock = MutableClock()
    runtime = _runtime(
        clock,
        [],
        max_attempts=1,
        failure_threshold=10,
        request_timeout_seconds=1,
        bulkhead_max_calls=2,
    )
    barrier = threading.Barrier(2)
    errors = []

    def fail_together():
        barrier.wait(1)
        raise TimeoutError

    def execute_failure():
        try:
            runtime.execute(KEY, fail_together, idempotent=True)
        except ProviderUnavailableError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=execute_failure) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)

    assert len(errors) == 2
    assert runtime.circuit_for(KEY).consecutive_failures == 2


def test_cache_returns_isolated_values_and_half_open_probe_is_not_restored():
    clock = MutableClock()
    runtime = _runtime(clock, [], max_attempts=1)
    original = {"nested": {"price": 100}}
    runtime.execute(KEY, lambda: original, idempotent=True, cache_identity="mutable")
    original["nested"]["price"] = 1
    first_hit = runtime.execute(
        KEY, lambda: pytest.fail("must use cache"), idempotent=True,
        cache_identity="mutable"
    )
    first_hit.value["nested"]["price"] = 2
    second_hit = runtime.execute(
        KEY, lambda: pytest.fail("must use cache"), idempotent=True,
        cache_identity="mutable"
    )

    state = runtime.export_state().model_dump(mode="json")
    state["circuits"][KEY.storage_key] = {
        "state": "half_open",
        "consecutive_failures": 2,
        "opened_at": NOW.isoformat(),
        "half_open_in_flight": 1,
    }
    restored = _runtime(clock, [])
    restored.import_state(state)

    assert second_hit.value["nested"]["price"] == 100
    assert restored.circuit_for(KEY).half_open_in_flight == 0


def test_export_state_is_consistent_while_provider_calls_complete():
    clock = MutableClock()
    runtime = _runtime(
        clock,
        [],
        max_attempts=1,
        bulkhead_max_calls=4,
        fresh_cache_seconds=0,
    )
    start = threading.Barrier(4)
    errors = []

    def execute(index):
        try:
            start.wait(1)
            for iteration in range(20):
                runtime.execute(
                    KEY,
                    lambda: {"ok": True},
                    idempotent=True,
                    cache_identity=f"{index}-{iteration}",
                )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=execute, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    start.wait(1)
    for _ in range(20):
        assert runtime.export_state().model_dump_json()
    for thread in threads:
        thread.join(2)

    assert errors == []
    assert runtime.provider_health(KEY).sample_count > 0
