from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from state.blindness import (
    BlindnessObservation,
    ProtectionSnapshot,
    ProtectionState,
    transition_protection,
)

T0 = datetime(2026, 8, 10, 7, 30, tzinfo=UTC)


def observe(
    *,
    at: datetime = T0,
    enabled: int = 2,
    usable: int = 2,
    full_scan: bool = False,
    deadline_missed: bool = False,
    provider_degraded: bool = False,
    paused: bool = False,
    reasons: tuple[str, ...] = (),
) -> BlindnessObservation:
    return BlindnessObservation(
        observed_at=at,
        enabled_instruments=enabled,
        usable_instruments=usable,
        full_coverage_scan=full_scan,
        deadline_missed=deadline_missed,
        provider_degraded=provider_degraded,
        paused=paused,
        reason_codes=reasons,
        unusable_tickers=("00700",) if usable < enabled else (),
    )


def test_unconfigured_and_initial_full_scan_never_invent_health() -> None:
    empty = transition_protection(None, observe(enabled=0, usable=0))
    assert empty.snapshot.state is ProtectionState.UNCONFIGURED
    assert empty.snapshot.color == "GRAY"
    assert empty.snapshot.coverage.ratio is None

    configured = transition_protection(None, observe(full_scan=True))
    assert configured.snapshot.state is ProtectionState.HEALTHY
    assert configured.snapshot.color == "GREEN"
    assert configured.snapshot.last_success_at == T0


def test_partial_coverage_is_degraded_and_repeated_observation_has_no_edge() -> None:
    healthy = transition_protection(None, observe(full_scan=True)).snapshot
    partial = transition_protection(
        healthy,
        observe(at=T0 + timedelta(minutes=5), usable=1, reasons=("partial_coverage",)),
    )
    assert partial.snapshot.state is ProtectionState.DEGRADED
    assert partial.snapshot.color == "AMBER"
    assert partial.edge is True
    assert partial.event_type == "degraded"
    assert partial.snapshot.incident_id is not None

    repeated = transition_protection(
        partial.snapshot,
        observe(at=T0 + timedelta(minutes=10), usable=1, reasons=("partial_coverage",)),
    )
    assert repeated.snapshot.state is ProtectionState.DEGRADED
    assert repeated.snapshot.state_since == partial.snapshot.state_since
    assert repeated.edge is False
    assert repeated.event_type is None


def test_zero_coverage_or_missed_deadline_is_immediately_blind() -> None:
    no_data = transition_protection(
        None, observe(usable=0, reasons=("no_data",))
    )
    assert no_data.snapshot.state is ProtectionState.BLIND
    assert no_data.snapshot.blind_started_at == T0

    healthy = transition_protection(None, observe(full_scan=True)).snapshot
    missed = transition_protection(
        healthy,
        observe(
            at=T0 + timedelta(minutes=16),
            deadline_missed=True,
            reasons=("expected_window_missing",),
        ),
    )
    assert missed.snapshot.state is ProtectionState.BLIND
    assert "deadline_missed" in missed.snapshot.reason_codes


def test_incident_recovery_requires_real_scan_then_consecutive_confirmation() -> None:
    blind = transition_protection(
        None, observe(usable=0, reasons=("no_data",))
    ).snapshot
    watchdog_only = transition_protection(
        blind, observe(at=T0 + timedelta(minutes=5))
    )
    assert watchdog_only.snapshot.state is ProtectionState.BLIND
    assert "awaiting_full_scan" in watchdog_only.snapshot.reason_codes

    recovering = transition_protection(
        watchdog_only.snapshot,
        observe(at=T0 + timedelta(minutes=6), full_scan=True),
    )
    assert recovering.snapshot.state is ProtectionState.RECOVERING
    assert recovering.snapshot.color == "BLUE"
    assert recovering.snapshot.healthy_confirmations == 1
    assert recovering.snapshot.recovery_has_full_scan is True

    watchdog_confirmation = transition_protection(
        recovering.snapshot,
        observe(at=T0 + timedelta(minutes=11)),
    )
    assert watchdog_confirmation.snapshot.state is ProtectionState.RECOVERING
    assert watchdog_confirmation.snapshot.healthy_confirmations == 1

    recovered = transition_protection(
        watchdog_confirmation.snapshot,
        observe(at=T0 + timedelta(minutes=12), full_scan=True),
    )
    assert recovered.snapshot.state is ProtectionState.HEALTHY
    assert recovered.event_type == "recovered"
    assert recovered.snapshot.recovered_at == T0 + timedelta(minutes=12)
    assert recovered.snapshot.blind_started_at == T0


def test_recovery_regresses_to_degraded_or_blind_on_new_bad_evidence() -> None:
    degraded = transition_protection(None, observe(usable=1)).snapshot
    recovering = transition_protection(
        degraded, observe(at=T0 + timedelta(minutes=1), full_scan=True)
    ).snapshot

    partial = transition_protection(
        recovering, observe(at=T0 + timedelta(minutes=2), usable=1)
    )
    assert partial.snapshot.state is ProtectionState.DEGRADED
    assert partial.snapshot.healthy_confirmations == 0

    recovering_again = transition_protection(
        partial.snapshot,
        observe(at=T0 + timedelta(minutes=3), full_scan=True),
    ).snapshot
    blind = transition_protection(
        recovering_again,
        observe(at=T0 + timedelta(minutes=4), usable=0, reasons=("all_stale",)),
    )
    assert blind.snapshot.state is ProtectionState.BLIND


def test_snapshot_round_trips_as_stable_json() -> None:
    snapshot = transition_protection(
        None,
        observe(usable=1, provider_degraded=True, reasons=("provider_degraded",)),
    ).snapshot
    payload = snapshot.model_dump(mode="json")
    assert ProtectionSnapshot.model_validate(payload) == snapshot


def test_same_observation_id_is_idempotent_and_cannot_confirm_recovery() -> None:
    blind = transition_protection(None, observe(usable=0)).snapshot
    first_scan = observe(
        at=T0 + timedelta(minutes=1),
        full_scan=True,
    ).model_copy(update={"observation_id": "scan-run-1"})
    recovering = transition_protection(blind, first_scan).snapshot

    replay = transition_protection(recovering, first_scan)

    assert replay.snapshot == recovering
    assert replay.snapshot.healthy_confirmations == 1
    assert replay.edge is False


def test_distinct_completed_scans_are_required_to_reach_healthy() -> None:
    blind = transition_protection(None, observe(usable=0)).snapshot
    first = transition_protection(
        blind,
        observe(at=T0 + timedelta(minutes=1), full_scan=True).model_copy(
            update={"observation_id": "scan-run-1"}
        ),
    ).snapshot
    second = transition_protection(
        first,
        observe(at=T0 + timedelta(minutes=2), full_scan=True).model_copy(
            update={"observation_id": "scan-run-2"}
        ),
    )
    assert second.snapshot.state is ProtectionState.HEALTHY
    assert second.snapshot.healthy_confirmations == 2


def test_scope_mismatch_and_out_of_order_observations_are_rejected() -> None:
    current = transition_protection(None, observe(full_scan=True)).snapshot
    mismatched = observe(at=T0 + timedelta(minutes=1), full_scan=True).model_copy(
        update={"scope": "market:US"}
    )
    out_of_order = observe(at=T0 - timedelta(seconds=1), full_scan=True)

    with pytest.raises(ValueError, match="scope"):
        transition_protection(current, mismatched)
    with pytest.raises(ValueError, match="out-of-order"):
        transition_protection(current, out_of_order)
