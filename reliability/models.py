"""Typed, JSON-stable reliability evidence for market-data decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProviderOutcome = Literal[
    "success",
    "transient_error",
    "permanent_error",
    "circuit_open",
    "cache_hit",
    "stale_fallback",
]
FailureClass = Literal[
    "none",
    "timeout",
    "connection",
    "rate_limited",
    "server_error",
    "client_error",
    "invalid_response",
    "unknown",
    "circuit_open",
    "bulkhead_full",
]
HealthGrade = Literal[
    "insufficient_data", "healthy", "degraded", "unreliable"
]
FreshnessReference = Literal[
    "wall_clock", "session_watermark", "observation_only", "none"
]
TimestampConfidence = Literal["provider_event", "observed_only", "none"]


class ReliabilityModel(BaseModel):
    """Immutable model whose JSON representation can be persisted verbatim."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class FreshnessStatus(str, Enum):
    FRESH = "FRESH"
    AGING = "AGING"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    FUTURE = "FUTURE"


class OverallStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    BLIND = "BLIND"


class TimeBasis(str, Enum):
    SOURCE_EVENT = "source_event"
    OBSERVED_ONLY = "observed_only"
    NONE = "none"


class CacheState(str, Enum):
    NONE = "none"
    MISS = "miss"
    FRESH = "fresh"
    STALE_IF_ERROR = "stale_if_error"


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class FreshnessContext(ReliabilityModel):
    """Clock and exchange-session context injected by the scheduling boundary."""

    evaluated_at: datetime
    market_phase: Literal[
        "open", "closed", "pre_open", "post_close", "unknown"
    ] = "unknown"
    expected_source_after: datetime | None = None

    @field_validator("evaluated_at", "expected_source_after", mode="after")
    @classmethod
    def require_aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("freshness datetimes must include a timezone")
        return value.astimezone(UTC)


class FieldFreshnessPolicy(ReliabilityModel):
    """Independent source-event and observation-age budgets for one field."""

    max_source_age_seconds: float | None = Field(
        default=None, gt=0, le=31_536_000, allow_inf_nan=False
    )
    max_observation_age_seconds: float = Field(
        default=86_400, gt=0, le=31_536_000, allow_inf_nan=False
    )
    aging_ratio: float = Field(default=0.8, gt=0, lt=1, allow_inf_nan=False)
    allow_observed_only: bool = False
    session_aware: bool = False

    @model_validator(mode="after")
    def source_budget_is_defined_when_required(self) -> Self:
        if not self.allow_observed_only and self.max_source_age_seconds is None:
            raise ValueError(
                "max_source_age_seconds is required when observed-only data is denied"
            )
        return self


class FieldReliability(ReliabilityModel):
    field: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    status: FreshnessStatus
    source_as_of: datetime | None = None
    observed_at: datetime | None = None
    source_age_seconds: float | None = Field(default=None, allow_inf_nan=False)
    observation_age_seconds: float | None = Field(default=None, allow_inf_nan=False)
    budget_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    observation_budget_seconds: float = Field(gt=0, allow_inf_nan=False)
    time_basis: TimeBasis
    cache_state: CacheState = CacheState.NONE
    freshness_reference: FreshnessReference
    expected_source_after: datetime | None = None
    timestamp_confidence: TimestampConfidence
    provider: str | None = Field(default=None, max_length=80)
    usable_for_signal: bool
    reason: str = Field(min_length=1, max_length=240)

    @field_validator(
        "source_as_of", "observed_at", "expected_source_after", mode="after"
    )
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("field timestamps must include a timezone")
        return value.astimezone(UTC)


class ProviderKey(ReliabilityModel):
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    operation: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    market: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9_.-]+$")

    @property
    def storage_key(self) -> str:
        return f"{self.provider}:{self.operation}:{self.market}"


class ProviderAttempt(ReliabilityModel):
    provider: str = Field(min_length=1, max_length=64)
    operation: str = Field(min_length=1, max_length=64)
    market: str = Field(min_length=1, max_length=16)
    outcome: ProviderOutcome
    failure_class: FailureClass = "none"
    error_type: FailureClass = "none"
    latency_ms: float = Field(ge=0, allow_inf_nan=False)
    attempt_index: int = Field(ge=0, le=20)
    cache_state: CacheState = CacheState.NONE
    circuit_state: CircuitState = CircuitState.CLOSED
    observed_at: datetime

    @field_validator("observed_at", mode="after")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider attempt timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def outcome_evidence_is_consistent(self) -> Self:
        if self.error_type != self.failure_class:
            raise ValueError("error_type must match failure_class")
        if self.outcome == "success":
            valid = (
                self.failure_class == "none"
                and self.cache_state is CacheState.MISS
                and self.attempt_index >= 1
                and self.circuit_state is CircuitState.CLOSED
            )
        elif self.outcome == "cache_hit":
            valid = (
                self.failure_class == "none"
                and self.cache_state is CacheState.FRESH
                and self.attempt_index == 0
            )
        elif self.outcome == "stale_fallback":
            valid = (
                self.failure_class == "none"
                and self.cache_state is CacheState.STALE_IF_ERROR
                and self.attempt_index == 0
            )
        elif self.outcome == "circuit_open":
            valid = (
                self.failure_class == "circuit_open"
                and self.cache_state is CacheState.MISS
                and self.attempt_index == 0
                and self.circuit_state
                in {CircuitState.OPEN, CircuitState.HALF_OPEN}
            )
        else:
            valid = (
                self.failure_class not in {"none", "circuit_open"}
                and self.cache_state is CacheState.MISS
                and self.attempt_index >= 1
            )
        if not valid:
            raise ValueError("provider attempt outcome evidence is inconsistent")
        return self


class ReliabilityReport(ReliabilityModel):
    ticker: str = Field(min_length=1, max_length=64)
    market: str = Field(min_length=1, max_length=16)
    overall: OverallStatus
    # True only when every enabled-rule field is eligible.  Partial protective
    # signals may still be valid; callers must inspect their triggered fields.
    usable_for_signal: bool
    full_coverage: bool
    usable_for_trusted_silence: bool
    evaluated_at: datetime
    fields: dict[str, FieldReliability]
    provider_attempts: tuple[ProviderAttempt, ...] = ()
    reasons: tuple[str, ...] = ()
    cache_state: CacheState = CacheState.NONE

    @field_validator("evaluated_at", mode="after")
    @classmethod
    def normalize_evaluated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("report timestamp must include a timezone")
        return value.astimezone(UTC)


class CoverageSummary(ReliabilityModel):
    enabled_instruments: int = Field(ge=0)
    usable_instruments: int = Field(ge=0)
    fresh_coverage: float | None = Field(default=None, ge=0, le=1)
    unusable_tickers: tuple[str, ...] = ()


class CircuitSnapshot(ReliabilityModel):
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = Field(default=0, ge=0)
    opened_at: datetime | None = None
    half_open_in_flight: int = Field(default=0, ge=0)

    @field_validator("opened_at", mode="after")
    @classmethod
    def normalize_opened_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("circuit timestamp must include a timezone")
        return value.astimezone(UTC)


class ProviderRuntimeConfig(ReliabilityModel):
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    bulkhead_max_calls: int = Field(default=4, ge=1, le=64)
    max_attempts: int = Field(default=3, ge=1, le=5)
    base_backoff_seconds: float = Field(default=0.25, ge=0, le=10)
    max_backoff_seconds: float = Field(default=4.0, gt=0, le=60)
    max_retry_after_seconds: float = Field(default=60.0, gt=0, le=300)
    failure_threshold: int = Field(default=3, ge=1, le=20)
    open_seconds: float = Field(default=300.0, gt=0, le=86_400)
    half_open_max_calls: int = Field(default=1, ge=1, le=10)
    fresh_cache_seconds: float = Field(default=300.0, ge=0, le=86_400)
    stale_if_error_seconds: float = Field(default=86_400.0, ge=0, le=604_800)
    observation_limit: int = Field(default=100, ge=10, le=10_000)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= base_backoff_seconds")
        if self.stale_if_error_seconds < self.fresh_cache_seconds:
            raise ValueError(
                "stale_if_error_seconds must be >= fresh_cache_seconds"
            )
        return self


class ProviderHealth(ReliabilityModel):
    key: ProviderKey
    sample_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    success_rate: float | None = Field(default=None, ge=0, le=1)
    wilson_lower_bound: float | None = Field(default=None, ge=0, le=1)
    grade: HealthGrade


class RuntimeState(ReliabilityModel):
    circuits: dict[str, CircuitSnapshot] = Field(default_factory=dict)
    caches: dict[str, dict[str, Any]] = Field(default_factory=dict)
    observations: dict[str, tuple[ProviderAttempt, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def persistence_bounds_are_enforced(self) -> Self:
        provider_keys = set(self.circuits) | set(self.observations)
        provider_keys.update(
            cache_key.rsplit(":", 1)[0]
            for cache_key in self.caches
            if ":" in cache_key
        )
        if len(provider_keys) > 256:
            raise ValueError("provider runtime cannot exceed 256 provider keys")
        if len(self.caches) > 10_000:
            raise ValueError("provider runtime cannot exceed 10000 cache records")
        if any(len(items) > 10_000 for items in self.observations.values()):
            raise ValueError("provider runtime cannot exceed 10000 samples per key")
        if sum(len(items) for items in self.observations.values()) > 50_000:
            raise ValueError("provider runtime cannot exceed 50000 total samples")
        return self
