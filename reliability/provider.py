"""Bounded provider retry, cache and circuit-breaker runtime.

Retries are permitted only for explicitly idempotent operations and classified
transient failures.  Full-jitter backoff, Retry-After, clocks and sleeping are all
injectable so tests never wait or depend on wall time.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random as random_module
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Generic, TypeVar

from .models import (
    CacheState,
    CircuitSnapshot,
    CircuitState,
    FailureClass,
    HealthGrade,
    ProviderAttempt,
    ProviderHealth,
    ProviderKey,
    ProviderOutcome,
    ProviderRuntimeConfig,
    ReliabilityReport,
    RuntimeState,
)


T = TypeVar("T")


class ProviderHTTPError(Exception):
    """Sanitized HTTP failure with only retry-relevant attributes."""

    def __init__(self, status_code: int, retry_after: str | float | None = None):
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"provider HTTP status {status_code}")


class ProviderUnavailableError(RuntimeError):
    """Fail-closed terminal provider error without URL, token or raw body."""

    def __init__(
        self,
        key: ProviderKey,
        attempts: tuple[ProviderAttempt, ...],
        circuit: CircuitSnapshot,
        *,
        report: ReliabilityReport | None = None,
    ) -> None:
        self.key = key
        self.attempts = attempts
        self.circuit = circuit
        self.report = report
        super().__init__(
            f"provider {key.provider} operation {key.operation} is unavailable"
        )


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    value: T
    attempts: tuple[ProviderAttempt, ...]
    cache_state: CacheState
    circuit: CircuitSnapshot
    usable_for_signal: bool


@dataclass
class _CacheRecord(Generic[T]):
    value: T
    stored_at: datetime


@dataclass(frozen=True)
class _Failure:
    failure_class: FailureClass
    transient: bool
    retry_after_seconds: float | None
    retry_safe: bool = True


class _OperationTimedOut(TimeoutError):
    """The caller returned, but the non-cancellable daemon call may still run."""


class _BulkheadFull(RuntimeError):
    """No execution slot is available for this provider capability."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("provider clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def parse_retry_after(value: Any, now: datetime) -> float | None:
    """Parse either Retry-After delta-seconds or an RFC HTTP date."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            return None
        seconds = (retry_at.astimezone(UTC) - _aware_utc(now)).total_seconds()
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _status_code(exc: Exception) -> int | None:
    raw = getattr(exc, "status_code", None)
    if raw is None:
        response = getattr(exc, "response", None)
        raw = getattr(response, "status_code", None)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        status = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return status if 100 <= status <= 599 else None


def _retry_after(exc: Exception, now: datetime) -> float | None:
    direct = getattr(exc, "retry_after", None)
    if direct is not None:
        return parse_retry_after(direct, now)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        for name, value in headers.items():
            if str(name).casefold() == "retry-after":
                return parse_retry_after(value, now)
    return None


def classify_provider_error(exc: Exception, now: datetime) -> _Failure:
    """Map arbitrary exceptions into a fixed low-cardinality taxonomy."""

    class_name = type(exc).__name__.casefold()
    module = type(exc).__module__.casefold()
    if isinstance(exc, TimeoutError) or (
        module.startswith("requests.") and "timeout" in class_name
    ):
        return _Failure(
            "timeout", True, None, retry_safe=not isinstance(exc, _OperationTimedOut)
        )
    if isinstance(exc, _BulkheadFull):
        return _Failure("bulkhead_full", True, None, retry_safe=False)
    if isinstance(exc, ConnectionError) or (
        module.startswith("requests.") and "connection" in class_name
    ):
        return _Failure("connection", True, None)

    status = _status_code(exc)
    if status == 429:
        return _Failure("rate_limited", True, _retry_after(exc, now))
    if status in {408, 425} or 500 <= (status or 0) <= 599:
        return _Failure("server_error", True, _retry_after(exc, now))
    if status is not None and 400 <= status <= 499:
        return _Failure("client_error", False, None)
    if isinstance(exc, (ValueError, TypeError)):
        return _Failure("invalid_response", False, None)
    return _Failure("unknown", False, None)


def _hashed_cache_identity(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()[:24]


class ProviderRuntime:
    """In-process runtime whose state can be exported to a persistence boundary."""

    def __init__(
        self,
        config: ProviderRuntimeConfig | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random_module.random,
        cache_serializer: Callable[[Any], Any] | None = None,
        cache_deserializer: Callable[[Any], Any] | None = None,
    ) -> None:
        self.config = config or ProviderRuntimeConfig()
        self._clock = clock
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._random_value = random_value
        self._cache_serializer = cache_serializer
        self._cache_deserializer = cache_deserializer
        self._lock = threading.RLock()
        self._circuits: dict[str, CircuitSnapshot] = {}
        self._caches: dict[str, _CacheRecord[Any]] = {}
        self._observations: dict[str, deque[ProviderAttempt]] = {}
        self._bulkheads: dict[str, threading.BoundedSemaphore] = {}

    def _now(self) -> datetime:
        return _aware_utc(self._clock())

    def circuit_for(self, key: ProviderKey) -> CircuitSnapshot:
        with self._lock:
            return self._circuits.get(key.storage_key, CircuitSnapshot())

    def observations_for(self, key: ProviderKey) -> tuple[ProviderAttempt, ...]:
        with self._lock:
            return tuple(self._observations.get(key.storage_key, ()))

    def _observe(self, key: ProviderKey, attempt: ProviderAttempt) -> None:
        with self._lock:
            history = self._observations.setdefault(
                key.storage_key, deque(maxlen=self.config.observation_limit)
            )
            history.append(attempt)

    def _attempt(
        self,
        key: ProviderKey,
        *,
        outcome: ProviderOutcome,
        failure_class: FailureClass,
        latency_ms: float,
        attempt_index: int,
        cache_state: CacheState,
        circuit_state: CircuitState,
        observed_at: datetime,
    ) -> ProviderAttempt:
        attempt = ProviderAttempt(
            provider=key.provider,
            operation=key.operation,
            market=key.market,
            outcome=outcome,
            failure_class=failure_class,
            error_type=failure_class,
            latency_ms=max(0.0, latency_ms),
            attempt_index=attempt_index,
            cache_state=cache_state,
            circuit_state=circuit_state,
            observed_at=observed_at,
        )
        self._observe(key, attempt)
        return attempt

    def _cache_key(self, key: ProviderKey, cache_identity: str | None) -> str:
        identity = cache_identity if cache_identity is not None else key.storage_key
        return f"{key.storage_key}:{_hashed_cache_identity(identity)}"

    def _cache_age(self, record: _CacheRecord[Any], now: datetime) -> float:
        return max(0.0, (now - record.stored_at).total_seconds())

    def _clone_cache_value(self, value: T) -> T:
        """Isolate caller-owned values from the cache's last-known-good copy."""

        if self._cache_serializer is not None and self._cache_deserializer is not None:
            encoded = self._cache_serializer(value)
            payload = json.dumps(encoded, allow_nan=False)
            return self._cache_deserializer(json.loads(payload))
        return copy.deepcopy(value)

    def _bulkhead_for(self, key: ProviderKey) -> threading.BoundedSemaphore:
        with self._lock:
            return self._bulkheads.setdefault(
                key.storage_key,
                threading.BoundedSemaphore(self.config.bulkhead_max_calls),
            )

    def _run_bounded(self, key: ProviderKey, call: Callable[[], T]) -> T:
        """Return by the deadline without spawning unbounded stuck calls.

        Python cannot safely kill a blocked third-party thread.  A timed-out call
        therefore keeps its bulkhead slot until it really exits, and is never
        retried while still in flight.  The daemon worker cannot hold process exit.
        """

        slot = self._bulkhead_for(key)
        if not slot.acquire(blocking=False):
            raise _BulkheadFull
        finished = threading.Event()
        result: list[T] = []
        failure: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(call())
            except BaseException as exc:  # propagate control exceptions to caller
                failure.append(exc)
            finally:
                slot.release()
                finished.set()

        worker = threading.Thread(
            target=invoke,
            name=f"alpha-guard-{key.provider}-{key.operation}",
            daemon=True,
        )
        worker.start()
        if not finished.wait(self.config.request_timeout_seconds):
            raise _OperationTimedOut
        if failure:
            raise failure[0]
        return result[0]

    def _admit(
        self, key: ProviderKey, now: datetime
    ) -> tuple[CircuitSnapshot, bool]:
        with self._lock:
            circuit = self._circuits.get(key.storage_key, CircuitSnapshot())
            if circuit.state is CircuitState.OPEN:
                opened_at = circuit.opened_at or now
                if (now - opened_at).total_seconds() >= self.config.open_seconds:
                    circuit = CircuitSnapshot(
                        state=CircuitState.HALF_OPEN,
                        consecutive_failures=circuit.consecutive_failures,
                        opened_at=circuit.opened_at,
                        half_open_in_flight=0,
                    )
                    self._circuits[key.storage_key] = circuit
            if circuit.state is CircuitState.HALF_OPEN:
                if circuit.half_open_in_flight >= self.config.half_open_max_calls:
                    return circuit, False
                circuit = CircuitSnapshot(
                    state=circuit.state,
                    consecutive_failures=circuit.consecutive_failures,
                    opened_at=circuit.opened_at,
                    half_open_in_flight=circuit.half_open_in_flight + 1,
                )
                self._circuits[key.storage_key] = circuit
            return circuit, circuit.state is not CircuitState.OPEN

    def _record_success(self, key: ProviderKey) -> CircuitSnapshot:
        circuit = CircuitSnapshot()
        with self._lock:
            self._circuits[key.storage_key] = circuit
        return circuit

    def _record_failure(
        self, key: ProviderKey, now: datetime, *, transient: bool
    ) -> CircuitSnapshot:
        with self._lock:
            circuit = self._circuits.get(key.storage_key, CircuitSnapshot())
            if not transient:
                if circuit.state is CircuitState.HALF_OPEN:
                    circuit = CircuitSnapshot()
                    self._circuits[key.storage_key] = circuit
                return circuit
            failures = circuit.consecutive_failures + 1
            should_open = (
                circuit.state is CircuitState.HALF_OPEN
                or failures >= self.config.failure_threshold
            )
            if should_open:
                circuit = CircuitSnapshot(
                    state=CircuitState.OPEN,
                    consecutive_failures=failures,
                    opened_at=now,
                    half_open_in_flight=0,
                )
            else:
                circuit = CircuitSnapshot(
                    state=CircuitState.CLOSED,
                    consecutive_failures=failures,
                    opened_at=None,
                    half_open_in_flight=0,
                )
            self._circuits[key.storage_key] = circuit
            return circuit

    def _backoff(self, attempt_index: int, retry_after: float | None) -> float:
        cap = min(
            self.config.max_backoff_seconds,
            self.config.base_backoff_seconds * (2 ** max(0, attempt_index - 1)),
        )
        random_fraction = self._random_value()
        if not math.isfinite(random_fraction):
            random_fraction = 0.0
        jitter = cap * min(1.0, max(0.0, random_fraction))
        if retry_after is None:
            return jitter
        bounded_retry_after = min(
            retry_after, self.config.max_retry_after_seconds
        )
        return max(jitter, bounded_retry_after)

    def _stale_or_raise(
        self,
        key: ProviderKey,
        record: _CacheRecord[T] | None,
        now: datetime,
        attempts: list[ProviderAttempt],
        circuit: CircuitSnapshot,
    ) -> ProviderResult[T]:
        if record is not None:
            age = self._cache_age(record, now)
            if age <= self.config.stale_if_error_seconds:
                fallback = self._attempt(
                    key,
                    outcome="stale_fallback",
                    failure_class="none",
                    latency_ms=0.0,
                    attempt_index=0,
                    cache_state=CacheState.STALE_IF_ERROR,
                    circuit_state=circuit.state,
                    observed_at=now,
                )
                attempts.append(fallback)
                try:
                    value = self._clone_cache_value(record.value)
                except Exception as exc:
                    raise ProviderUnavailableError(
                        key, tuple(attempts), circuit
                    ) from exc
                return ProviderResult(
                    value=value,
                    attempts=tuple(attempts),
                    cache_state=CacheState.STALE_IF_ERROR,
                    circuit=circuit,
                    usable_for_signal=False,
                )
        raise ProviderUnavailableError(key, tuple(attempts), circuit)

    def execute(
        self,
        key: ProviderKey,
        call: Callable[[], T],
        *,
        idempotent: bool,
        cache_identity: str | None = None,
    ) -> ProviderResult[T]:
        """Execute one provider operation under retry/cache/circuit controls."""

        now = self._now()
        cache_key = self._cache_key(key, cache_identity)
        with self._lock:
            record = self._caches.get(cache_key)
        cache_is_fresh = (
            record is not None
            and self._cache_age(record, now) <= self.config.fresh_cache_seconds
        )
        if cache_is_fresh and record is not None:
            try:
                cached_value = self._clone_cache_value(record.value)
            except Exception:
                with self._lock:
                    self._caches.pop(cache_key, None)
                record = None
        else:
            cached_value = None
        if cache_is_fresh and record is not None:
            circuit = self.circuit_for(key)
            hit = self._attempt(
                key,
                outcome="cache_hit",
                failure_class="none",
                latency_ms=0.0,
                attempt_index=0,
                cache_state=CacheState.FRESH,
                circuit_state=circuit.state,
                observed_at=now,
            )
            return ProviderResult(
                value=cached_value,
                attempts=(hit,),
                cache_state=CacheState.FRESH,
                circuit=circuit,
                usable_for_signal=True,
            )

        circuit, admitted = self._admit(key, now)
        if not admitted:
            blocked_attempt = self._attempt(
                key,
                outcome="circuit_open",
                failure_class="circuit_open",
                latency_ms=0.0,
                attempt_index=0,
                cache_state=CacheState.MISS,
                circuit_state=circuit.state,
                observed_at=now,
            )
            return self._stale_or_raise(
                key, record, now, [blocked_attempt], circuit
            )

        attempts: list[ProviderAttempt] = []
        max_attempts = self.config.max_attempts if idempotent else 1
        terminal_failure: _Failure | None = None
        for attempt_index in range(1, max_attempts + 1):
            started = self._monotonic()
            try:
                value = self._run_bounded(key, call)
            except Exception as exc:  # provider boundary; retry remains allowlisted
                latency_ms = max(0.0, (self._monotonic() - started) * 1_000)
                completed_at = self._now()
                failure = classify_provider_error(exc, completed_at)
                terminal_failure = failure
                outcome: ProviderOutcome = (
                    "transient_error" if failure.transient else "permanent_error"
                )
                attempts.append(
                    self._attempt(
                        key,
                        outcome=outcome,
                        failure_class=failure.failure_class,
                        latency_ms=latency_ms,
                        attempt_index=attempt_index,
                        cache_state=CacheState.MISS,
                        circuit_state=circuit.state,
                        observed_at=completed_at,
                    )
                )
                should_retry = (
                    idempotent
                    and failure.transient
                    and failure.retry_safe
                    and attempt_index < max_attempts
                )
                if should_retry:
                    self._sleeper(
                        self._backoff(attempt_index, failure.retry_after_seconds)
                    )
                    continue
                break
            else:
                latency_ms = max(0.0, (self._monotonic() - started) * 1_000)
                completed_at = self._now()
                circuit = self._record_success(key)
                success = self._attempt(
                    key,
                    outcome="success",
                    failure_class="none",
                    latency_ms=latency_ms,
                    attempt_index=attempt_index,
                    cache_state=CacheState.MISS,
                    circuit_state=circuit.state,
                    observed_at=completed_at,
                )
                attempts.append(success)
                try:
                    cached_copy = self._clone_cache_value(value)
                except Exception:
                    cached_copy = None
                if cached_copy is not None:
                    with self._lock:
                        self._caches[cache_key] = _CacheRecord(
                            value=cached_copy, stored_at=completed_at
                        )
                return ProviderResult(
                    value=value,
                    attempts=tuple(attempts),
                    cache_state=CacheState.MISS,
                    circuit=circuit,
                    usable_for_signal=True,
                )

        failure = terminal_failure or _Failure("unknown", False, None)
        terminal_at = self._now()
        circuit = self._record_failure(
            key, terminal_at, transient=failure.transient
        )
        return self._stale_or_raise(
            key, record, terminal_at, attempts, circuit
        )

    def provider_health(
        self, key: ProviderKey, *, minimum_samples: int = 20
    ) -> ProviderHealth:
        """Score real provider attempts; cache hits never inflate availability."""

        if minimum_samples < 1:
            raise ValueError("minimum_samples must be positive")
        samples = [
            item
            for item in self.observations_for(key)
            if item.outcome
            in {"success", "transient_error", "permanent_error"}
        ]
        count = len(samples)
        successes = sum(item.outcome == "success" for item in samples)
        if count == 0:
            return ProviderHealth(
                key=key,
                sample_count=0,
                success_count=0,
                success_rate=None,
                wilson_lower_bound=None,
                grade="insufficient_data",
            )
        rate = successes / count
        z = 1.96
        denominator = 1 + z * z / count
        center = rate + z * z / (2 * count)
        margin = z * math.sqrt(
            (rate * (1 - rate) + z * z / (4 * count)) / count
        )
        lower = max(0.0, (center - margin) / denominator)
        if count < minimum_samples:
            grade: HealthGrade = "insufficient_data"
        elif lower >= 0.95:
            grade = "healthy"
        elif lower >= 0.80:
            grade = "degraded"
        else:
            grade = "unreliable"
        return ProviderHealth(
            key=key,
            sample_count=count,
            success_count=successes,
            success_rate=rate,
            wilson_lower_bound=lower,
            grade=grade,
        )

    def export_state(self) -> RuntimeState:
        """Return JSON-stable state; unserializable cache values are omitted."""

        with self._lock:
            cache_items = tuple(self._caches.items())
            circuits = dict(self._circuits)
            observations = {
                key: tuple(items) for key, items in self._observations.items()
            }
        caches: dict[str, dict[str, Any]] = {}
        for key, record in cache_items:
            try:
                isolated = copy.deepcopy(record.value)
                value = (
                    self._cache_serializer(isolated)
                    if self._cache_serializer is not None
                    else isolated
                )
                json.dumps(value, allow_nan=False)
            except Exception:
                continue
            caches[key] = {"stored_at": record.stored_at.isoformat(), "value": value}
        return RuntimeState(
            circuits=circuits,
            caches=caches,
            observations=observations,
        )

    def import_state(self, state: RuntimeState | Mapping[str, Any]) -> None:
        """Replace runtime state from a validated persistence snapshot."""

        resolved = (
            state if isinstance(state, RuntimeState) else RuntimeState.model_validate(state)
        )
        caches: dict[str, _CacheRecord[Any]] = {}
        for key, raw in resolved.caches.items():
            stored_at_raw = raw.get("stored_at")
            value = raw.get("value")
            try:
                stored_at = datetime.fromisoformat(str(stored_at_raw))
                stored_at = _aware_utc(stored_at)
            except (TypeError, ValueError):
                continue
            if self._cache_deserializer is not None:
                value = self._cache_deserializer(value)
            try:
                value = self._clone_cache_value(value)
            except Exception:
                continue
            caches[key] = _CacheRecord(value=value, stored_at=stored_at)
        circuits = {
            key: circuit.model_copy(update={"half_open_in_flight": 0})
            for key, circuit in resolved.circuits.items()
        }
        with self._lock:
            self._circuits = circuits
            self._caches = caches
            self._observations = {
                key: deque(items, maxlen=self.config.observation_limit)
                for key, items in resolved.observations.items()
            }
