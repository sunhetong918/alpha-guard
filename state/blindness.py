"""Pure trusted-silence state machine.

The state machine knows nothing about SQLite, Telegram or wall clocks.  Every
transition is driven by explicit evidence so incident history can be replayed
and audited deterministically.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProtectionState(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    PAUSED = "PAUSED"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    BLIND = "BLIND"
    RECOVERING = "RECOVERING"


STATE_COLORS: dict[ProtectionState, str] = {
    ProtectionState.UNCONFIGURED: "GRAY",
    ProtectionState.PAUSED: "GRAY",
    ProtectionState.HEALTHY: "GREEN",
    ProtectionState.DEGRADED: "AMBER",
    ProtectionState.BLIND: "RED",
    ProtectionState.RECOVERING: "BLUE",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CoverageEvidence(_Model):
    enabled_instruments: int = Field(ge=0)
    usable_instruments: int = Field(ge=0)
    ratio: float | None = Field(default=None, ge=0, le=1)
    unusable_tickers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def coverage_is_consistent(self) -> Self:
        if self.usable_instruments > self.enabled_instruments:
            raise ValueError("usable instruments cannot exceed enabled instruments")
        expected = (
            self.usable_instruments / self.enabled_instruments
            if self.enabled_instruments
            else None
        )
        if self.ratio != expected:
            raise ValueError("coverage ratio must match enabled and usable counts")
        if self.usable_instruments == self.enabled_instruments and self.unusable_tickers:
            raise ValueError("full coverage cannot list unusable tickers")
        if len(set(self.unusable_tickers)) != len(self.unusable_tickers):
            raise ValueError("unusable tickers must be unique")
        if len(self.unusable_tickers) > self.enabled_instruments:
            raise ValueError("unusable tickers cannot exceed enabled instruments")
        for ticker in self.unusable_tickers:
            if (
                not ticker
                or ticker != ticker.strip()
                or len(ticker) > 64
                or not all(
                    character.isalnum() or character in "._-"
                    for character in ticker
                )
            ):
                raise ValueError("unusable tickers must be canonical identifiers")
        return self


class BlindnessObservation(_Model):
    scope: str = Field(default="global", min_length=1, max_length=80)
    observation_id: str | None = Field(default=None, min_length=1, max_length=96)
    observed_at: datetime
    enabled_instruments: int = Field(ge=0)
    usable_instruments: int = Field(ge=0)
    unusable_tickers: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    paused: bool = False
    provider_degraded: bool = False
    deadline_missed: bool = False
    full_coverage_scan: bool = False
    last_success_at: datetime | None = None

    @field_validator("observed_at", "last_success_at", mode="after")
    @classmethod
    def aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observation timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("reason_codes", mode="after")
    @classmethod
    def bounded_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip().lower() for item in value))
        if len(normalized) > 32:
            raise ValueError("reason_codes cannot exceed 32 items")
        for item in normalized:
            if not item or len(item) > 64 or not all(
                character.isalnum() or character in "_.:-" for character in item
            ):
                raise ValueError("reason codes must be low-cardinality identifiers")
        return normalized

    @model_validator(mode="after")
    def counts_are_consistent(self) -> Self:
        if self.usable_instruments > self.enabled_instruments:
            raise ValueError("usable instruments cannot exceed enabled instruments")
        if self.full_coverage_scan and (
            not self.enabled_instruments
            or self.usable_instruments != self.enabled_instruments
        ):
            raise ValueError("a full-coverage scan requires all enabled instruments")
        return self


class ProtectionSnapshot(_Model):
    scope: str = Field(min_length=1, max_length=80)
    state: ProtectionState
    color: str
    state_since: datetime
    updated_at: datetime
    coverage: CoverageEvidence
    reason_codes: tuple[str, ...] = ()
    incident_id: str | None = None
    incident_started_at: datetime | None = None
    blind_started_at: datetime | None = None
    recovered_at: datetime | None = None
    last_success_at: datetime | None = None
    healthy_confirmations: int = Field(default=0, ge=0)
    recovery_has_full_scan: bool = False
    last_observation_id: str = Field(min_length=1, max_length=96)

    @field_validator(
        "state_since",
        "updated_at",
        "incident_started_at",
        "blind_started_at",
        "recovered_at",
        "last_success_at",
        mode="after",
    )
    @classmethod
    def aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("protection timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def color_matches_state(self) -> Self:
        if self.color != STATE_COLORS[self.state]:
            raise ValueError("protection color must match state")
        if self.state_since > self.updated_at:
            raise ValueError("state_since cannot follow updated_at")
        for value in (
            self.incident_started_at,
            self.blind_started_at,
            self.recovered_at,
            self.last_success_at,
        ):
            if value is not None and value > self.updated_at:
                raise ValueError("protection evidence cannot be future-dated")

        enabled = self.coverage.enabled_instruments
        usable = self.coverage.usable_instruments
        if self.state is ProtectionState.UNCONFIGURED:
            if enabled != 0 or usable != 0 or self.coverage.ratio is not None:
                raise ValueError("UNCONFIGURED requires zero enabled instruments")
            if self.incident_id is not None or self.incident_started_at is not None:
                raise ValueError("UNCONFIGURED cannot carry an incident")
        elif enabled == 0:
            raise ValueError("configured protection states require instruments")

        if self.state in {ProtectionState.HEALTHY, ProtectionState.RECOVERING}:
            if usable != enabled or self.coverage.unusable_tickers:
                raise ValueError(f"{self.state.value} requires full coverage")
            if self.last_success_at is None:
                raise ValueError(f"{self.state.value} requires last_success_at")
        if self.state is ProtectionState.DEGRADED and usable == 0:
            raise ValueError("DEGRADED requires some usable coverage")
        if self.state is ProtectionState.PAUSED and (
            self.incident_id is not None or self.incident_started_at is not None
        ):
            raise ValueError("PAUSED cannot carry an incident")

        incident_state = self.state in {
            ProtectionState.DEGRADED,
            ProtectionState.BLIND,
            ProtectionState.RECOVERING,
        }
        if incident_state and (
            self.incident_id is None or self.incident_started_at is None
        ):
            raise ValueError("incident states require incident identity and start")
        if self.state is ProtectionState.BLIND and self.blind_started_at is None:
            raise ValueError("BLIND requires blind_started_at")
        if self.state is ProtectionState.RECOVERING:
            if self.healthy_confirmations != 1 or not self.recovery_has_full_scan:
                raise ValueError("RECOVERING requires exactly one full-scan confirmation")
        elif self.state is ProtectionState.HEALTHY:
            if self.reason_codes:
                raise ValueError("HEALTHY cannot retain degradation reason codes")
            valid_initial = (
                self.healthy_confirmations == 0
                and not self.recovery_has_full_scan
            )
            valid_recovered = (
                self.healthy_confirmations >= 2
                and self.recovery_has_full_scan
                and self.recovered_at is not None
            )
            if not (valid_initial or valid_recovered):
                raise ValueError("HEALTHY recovery confirmation evidence is inconsistent")
        elif self.healthy_confirmations or self.recovery_has_full_scan:
            raise ValueError("only RECOVERING/HEALTHY may retain confirmations")

        normalized_reasons = tuple(
            dict.fromkeys(item.strip().lower() for item in self.reason_codes)
        )
        if normalized_reasons != self.reason_codes:
            raise ValueError("snapshot reason codes must be normalized and unique")
        for item in normalized_reasons:
            if not item or len(item) > 64 or not all(
                character.isalnum() or character in "_.:-" for character in item
            ):
                raise ValueError("snapshot reason codes must be low-cardinality")
        return self


class ProtectionTransition(_Model):
    previous_state: ProtectionState | None
    snapshot: ProtectionSnapshot
    edge: bool
    event_type: str | None = None


def _incident_id(observation: BlindnessObservation) -> str:
    raw = (
        f"{observation.scope}|{observation.observed_at.isoformat()}|"
        f"{','.join(observation.reason_codes)}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _observation_id(observation: BlindnessObservation) -> str:
    if observation.observation_id is not None:
        return observation.observation_id
    raw = observation.model_dump_json(exclude={"observation_id"})
    return hashlib.sha256(raw.encode()).hexdigest()


def protection_observation_identity(observation: BlindnessObservation) -> str:
    """Return the stable replay identity used by the state machine."""

    return _observation_id(observation)


def _coverage(observation: BlindnessObservation) -> CoverageEvidence:
    ratio = (
        observation.usable_instruments / observation.enabled_instruments
        if observation.enabled_instruments
        else None
    )
    return CoverageEvidence(
        enabled_instruments=observation.enabled_instruments,
        usable_instruments=observation.usable_instruments,
        ratio=ratio,
        unusable_tickers=tuple(dict.fromkeys(observation.unusable_tickers)),
    )


def _evidence_state(observation: BlindnessObservation) -> ProtectionState:
    if observation.enabled_instruments == 0:
        return ProtectionState.UNCONFIGURED
    if observation.paused:
        return ProtectionState.PAUSED
    if observation.deadline_missed or observation.usable_instruments == 0:
        return ProtectionState.BLIND
    if (
        observation.provider_degraded
        or observation.usable_instruments < observation.enabled_instruments
    ):
        return ProtectionState.DEGRADED
    return ProtectionState.HEALTHY


def _event_type(
    previous: ProtectionState | None,
    current: ProtectionState,
) -> str:
    if current is ProtectionState.BLIND:
        return "blind"
    if current is ProtectionState.DEGRADED:
        return "degraded"
    if current is ProtectionState.RECOVERING:
        return "recovering"
    if current is ProtectionState.HEALTHY and previous in {
        ProtectionState.DEGRADED,
        ProtectionState.BLIND,
        ProtectionState.RECOVERING,
    }:
        return "recovered"
    if current is ProtectionState.HEALTHY:
        return "healthy"
    return current.value.lower()


def transition_protection(
    current: ProtectionSnapshot | None,
    observation: BlindnessObservation,
) -> ProtectionTransition:
    """Apply one observation, requiring a real scan plus a confirmation to recover."""

    now = observation.observed_at
    observation_id = _observation_id(observation)
    if current is not None:
        if current.scope != observation.scope:
            raise ValueError("observation scope does not match current state")
        if current.last_observation_id == observation_id:
            return ProtectionTransition(
                previous_state=current.state,
                snapshot=current,
                edge=False,
                event_type=None,
            )
        if observation.observed_at < current.updated_at:
            raise ValueError("out-of-order protection observation")
    evidence_state = _evidence_state(observation)
    previous = current.state if current is not None else None
    reasons = list(observation.reason_codes)
    if observation.deadline_missed and "deadline_missed" not in reasons:
        reasons.append("deadline_missed")

    target = evidence_state
    confirmations = 0
    recovery_has_full_scan = False
    if (
        current is None
        and evidence_state is ProtectionState.HEALTHY
        and not observation.full_coverage_scan
    ):
        target = ProtectionState.DEGRADED
        if "awaiting_full_scan" not in reasons:
            reasons.append("awaiting_full_scan")
    elif current is not None:
        confirmations = current.healthy_confirmations
        recovery_has_full_scan = current.recovery_has_full_scan

        if current.state in {ProtectionState.BLIND, ProtectionState.DEGRADED} and (
            evidence_state is ProtectionState.HEALTHY
        ):
            if observation.full_coverage_scan:
                target = ProtectionState.RECOVERING
                confirmations = 1
                recovery_has_full_scan = True
            else:
                target = current.state
                if "awaiting_full_scan" not in reasons:
                    reasons.append("awaiting_full_scan")
        elif current.state is ProtectionState.RECOVERING:
            if evidence_state is ProtectionState.HEALTHY:
                if observation.full_coverage_scan:
                    confirmations += 1
                    recovery_has_full_scan = True
                target = (
                    ProtectionState.HEALTHY
                    if confirmations >= 2 and recovery_has_full_scan
                    else ProtectionState.RECOVERING
                )
            else:
                target = evidence_state
                confirmations = 0
                recovery_has_full_scan = False
        elif evidence_state is not ProtectionState.HEALTHY:
            confirmations = 0
            recovery_has_full_scan = False

    edge = previous is not target
    state_since = now if edge or current is None else current.state_since

    incident_id = current.incident_id if current is not None else None
    incident_started_at = current.incident_started_at if current is not None else None
    blind_started_at = current.blind_started_at if current is not None else None
    recovered_at = current.recovered_at if current is not None else None
    entering_incident = target in {
        ProtectionState.DEGRADED,
        ProtectionState.BLIND,
        ProtectionState.RECOVERING,
    } and previous in {
        None,
        ProtectionState.UNCONFIGURED,
        ProtectionState.PAUSED,
        ProtectionState.HEALTHY,
    }
    if entering_incident:
        incident_id = _incident_id(observation)
        incident_started_at = now
        blind_started_at = None
        recovered_at = None
    if target is ProtectionState.BLIND and blind_started_at is None:
        blind_started_at = now
    if target is ProtectionState.HEALTHY and previous is ProtectionState.RECOVERING:
        recovered_at = now
    if target in {ProtectionState.UNCONFIGURED, ProtectionState.PAUSED}:
        incident_id = None
        incident_started_at = None
        blind_started_at = None
        recovered_at = None
        confirmations = 0
        recovery_has_full_scan = False

    last_success = current.last_success_at if current is not None else None
    if observation.last_success_at is not None:
        last_success = observation.last_success_at
    if observation.full_coverage_scan and evidence_state is ProtectionState.HEALTHY:
        last_success = now

    snapshot = ProtectionSnapshot(
        scope=observation.scope,
        state=target,
        color=STATE_COLORS[target],
        state_since=state_since,
        updated_at=now,
        coverage=_coverage(observation),
        reason_codes=tuple(dict.fromkeys(reasons)),
        incident_id=incident_id,
        incident_started_at=incident_started_at,
        blind_started_at=blind_started_at,
        recovered_at=recovered_at,
        last_success_at=last_success,
        healthy_confirmations=confirmations,
        recovery_has_full_scan=recovery_has_full_scan,
        last_observation_id=observation_id,
    )
    return ProtectionTransition(
        previous_state=previous,
        snapshot=snapshot,
        edge=edge,
        event_type=_event_type(previous, target) if edge else None,
    )


__all__ = [
    "BlindnessObservation",
    "CoverageEvidence",
    "ProtectionSnapshot",
    "ProtectionState",
    "ProtectionTransition",
    "STATE_COLORS",
    "protection_observation_identity",
    "transition_protection",
]
