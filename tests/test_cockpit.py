from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import Settings
from notifier.heartbeat import delivery_config_fingerprints, heartbeat_eligible
from reliability import CircuitSnapshot, ProviderAttempt, ProviderKey, RuntimeState
from scheduler import expected_market_scans_between, latest_expected_market_scan
from state.blindness import BlindnessObservation
from state.cockpit import build_reliability_cockpit as _build_reliability_cockpit
from state.store import StateStore as _StateStore


_MARKET_CONTRACTS = {"US": "a" * 64, "HK": "b" * 64}
_DELIVERY_FINGERPRINT = "d" * 64


class StateStore(_StateStore):
    """Test fixture store whose normal scopes carry proven contracts."""

    def set_protection_scope(self, enabled_markets, **kwargs):
        kwargs.setdefault(
            "market_contract_hashes",
            {market: _MARKET_CONTRACTS[market] for market in enabled_markets},
        )
        return super().set_protection_scope(enabled_markets, **kwargs)


def build_reliability_cockpit(**kwargs):
    configured = kwargs["enabled_instruments"]
    kwargs.setdefault(
        "market_contract_hashes",
        {
            market: _MARKET_CONTRACTS[market]
            for market in sorted(set(configured.values()))
        },
    )
    kwargs.setdefault(
        "current_delivery_fingerprints",
        {
            "telegram": _DELIVERY_FINGERPRINT,
            "whatsapp": _DELIVERY_FINGERPRINT,
            "heartbeat": _DELIVERY_FINGERPRINT,
        },
    )
    return _build_reliability_cockpit(**kwargs)


def _healthy_observation(at: datetime) -> BlindnessObservation:
    return BlindnessObservation(
        scope="market:US",
        observation_id=f"scan:{at.isoformat()}",
        observed_at=at,
        enabled_instruments=1,
        usable_instruments=1,
        full_coverage_scan=True,
    )


def _observation(
    scope: str,
    observation_id: str,
    at: datetime,
    *,
    enabled: int = 1,
    usable: int = 1,
    full_scan: bool = False,
    paused: bool = False,
    deadline_missed: bool = False,
) -> BlindnessObservation:
    return BlindnessObservation(
        scope=scope,
        observation_id=observation_id,
        observed_at=at,
        enabled_instruments=enabled,
        usable_instruments=usable,
        unusable_tickers=("AAPL",) if usable < enabled else (),
        reason_codes=("deadline_missed",) if deadline_missed else (),
        full_coverage_scan=full_scan,
        paused=paused,
        deadline_missed=deadline_missed,
    )


def _record_good_latest_window(store: StateStore, market: str, now: datetime) -> None:
    expected = latest_expected_market_scan(market, now)
    actual = expected.expected_at + timedelta(minutes=1)
    store.record_protection_window(
        expected.key,
        market,
        expected.expected_at,
        expected.deadline_at,
        "good",
        actual_at=actual,
        last_success_at=actual,
        enabled_instruments=1,
        usable_instruments=1,
        now=actual,
    )


def _record_empty_provider_runtime(store: StateStore, at: datetime) -> None:
    store.save_provider_runtime_state(
        RuntimeState().model_dump(mode="json"),
        now=at,
    )


def _stock_run_detail(
    *,
    selected: int,
    evaluated: int,
    fresh_usable: int,
    trusted_usable: int,
    fresh_affected: list[str],
    trusted_affected: list[str],
) -> dict:
    failed = evaluated < selected
    return {
        "selected": selected,
        "evaluated": evaluated,
        "notified": 0,
        "error_tickers": list(trusted_affected) if failed else [],
        "notification_error_tickers": [],
        "incident_attempted": False,
        "incident_notified": False,
        "incident_notification_error": None,
        "integrity_incident_attempted": 0,
        "integrity_incident_notified": 0,
        "integrity_notification_errors": {},
        "integrity_ledger_error": None,
        "telegram_probe_attempted": False,
        "telegram_probe_success": None,
        "telegram_probe_error": None,
        "protection_event_id": None,
        "protection_event_ids": [],
        "reliability": {
            "fresh_data_coverage": {
                "enabled_instruments": selected,
                "usable_instruments": fresh_usable,
                "fresh_coverage": fresh_usable / selected if selected else None,
                "unusable_tickers": fresh_affected,
            },
            "trusted_decision_coverage": {
                "enabled_instruments": selected,
                "usable_instruments": trusted_usable,
                "fresh_coverage": trusted_usable / selected if selected else None,
                "unusable_tickers": trusted_affected,
            },
        },
    }


def _market_run_slice(
    *,
    selected: int,
    evaluated: int,
    fresh_usable: int,
    trusted_usable: int,
    fresh_affected: list[str],
    trusted_affected: list[str],
) -> dict:
    return {
        "selected": selected,
        "evaluated": evaluated,
        "fresh_data_coverage": {
            "enabled_instruments": selected,
            "usable_instruments": fresh_usable,
            "fresh_coverage": fresh_usable / selected if selected else None,
            "unusable_tickers": fresh_affected,
        },
        "trusted_decision_coverage": {
            "enabled_instruments": selected,
            "usable_instruments": trusted_usable,
            "fresh_coverage": trusted_usable / selected if selected else None,
            "unusable_tickers": trusted_affected,
        },
    }


def _full_all_run_detail(markets: tuple[str, ...]) -> dict:
    detail = _stock_run_detail(
        selected=len(markets),
        evaluated=len(markets),
        fresh_usable=len(markets),
        trusted_usable=len(markets),
        fresh_affected=[],
        trusted_affected=[],
    )
    detail["reliability"]["by_market"] = {
        market: _market_run_slice(
            selected=1,
            evaluated=1,
            fresh_usable=1,
            trusted_usable=1,
            fresh_affected=[],
            trusted_affected=[],
        )
        for market in markets
    }
    return detail


def test_cockpit_zero_enabled_is_unconfigured_gray() -> None:
    receipt = build_reliability_cockpit(
        store=None,
        enabled_instruments={},
        generated_at=datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
    )

    assert receipt["state"] == "UNCONFIGURED"
    assert receipt["overall_color"] == "GRAY"
    assert receipt["reason_codes"] == ["unconfigured"]


@pytest.mark.parametrize("store_kind", ["absent", "empty"])
def test_cockpit_configured_without_protection_baseline_is_blue(
    tmp_path: Path,
    store_kind: str,
) -> None:
    store = None if store_kind == "absent" else StateStore(tmp_path / "state.db")
    try:
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
        )
    finally:
        if store is not None:
            store.close()

    assert (receipt["state"], receipt["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert receipt["reason_codes"] == ["protection_not_activated"]


def test_cockpit_valid_current_protection_is_healthy_green(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)  # 09:30 New York
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
                generated_at=now,
        )

    assert receipt["state"] == "HEALTHY"
    assert receipt["overall_color"] == "GREEN"
    assert receipt["reason_codes"] == []
    assert receipt["schedule"]["markets"][0]["deadline_state"] == "within_grace"


def test_cockpit_projects_active_and_recovered_watchdog_without_raw_evidence(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    window_key = "US:2026-08-10"
    with StateStore(tmp_path / "state.db") as store:
        scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        scope_generation = scope["watchdog_generation"]
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.observe_watchdog_incident(
            scope_generation=scope_generation,
            enabled_instruments=1,
            affected_tickers=("AAPL",),
            markets=("US",),
            window_keys=(window_key,),
            first_seen_at=expected.expected_at,
            now=now,
        )

        active = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )
        store.resolve_watchdog_incident(now=now + timedelta(seconds=1))
        recovered = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now + timedelta(seconds=1),
        )

    assert (active["state"], active["overall_color"]) == ("BLIND", "RED")
    assert active["silence"]["state"] == "HEALTHY"
    assert "watchdog_incident_active" in active["reason_codes"]
    assert active["watchdog"] == {
        "state": "BLIND",
        "generation": 1,
        "active": True,
        "affected": ["AAPL"],
        "markets": ["US"],
        "window_count": 1,
        "first_seen_at": expected.expected_at.astimezone(UTC).isoformat(
            timespec="microseconds"
        ),
        "resolved_at": None,
        "delivery_status": "suppressed",
    }
    assert (recovered["state"], recovered["overall_color"]) == (
        "HEALTHY",
        "GREEN",
    )
    assert recovered["watchdog"]["state"] == "RECOVERED"
    assert recovered["watchdog"]["active"] is False
    assert recovered["watchdog"]["resolved_at"] is not None
    rendered = json.dumps((active, recovered), sort_keys=True)
    assert scope_generation not in rendered
    assert window_key not in rendered
    assert "evidence_sha256" not in rendered
    assert "payload_json" not in rendered


@pytest.mark.parametrize("delivery_status", ["pending", "sent", "suppressed"])
def test_cockpit_active_watchdog_disposition_is_always_red(
    tmp_path: Path,
    delivery_status: str,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        incident = store.observe_watchdog_incident(
            scope_generation=scope["watchdog_generation"],
            enabled_instruments=1,
            affected_tickers=("AAPL",),
            markets=("US",),
            window_keys=("US:2026-08-10",),
            first_seen_at=expected.expected_at,
            delivery_status=(
                "pending" if delivery_status == "sent" else delivery_status
            ),
            now=now,
        )
        if delivery_status == "sent":
            claim = store.claim_watchdog_incident_notification(
                incident["id"], now=now
            )
            assert claim is not None
            store.mark_watchdog_incident_notified(
                incident["id"], claim, now=now
            )

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["silence"]["state"] == "HEALTHY"
    assert receipt["watchdog"]["delivery_status"] == delivery_status
    assert heartbeat_eligible(receipt) is False


@pytest.mark.parametrize("early_return", ["zero", "paused", "absent"])
def test_cockpit_future_resolved_watchdog_is_red_before_early_return(
    tmp_path: Path,
    early_return: str,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    with StateStore(tmp_path / "state.db") as store:
        if early_return == "absent":
            scope_generation = "f" * 64
        else:
            scope = store.set_protection_scope(
                ["US"],
                enabled_instruments_by_market={"US": ("AAPL",)},
                paused=early_return == "paused",
                now=now - timedelta(minutes=5),
            )
            scope_generation = scope["watchdog_generation"]
        store.observe_watchdog_incident(
            scope_generation=scope_generation,
            enabled_instruments=1,
            affected_tickers=("AAPL",),
            markets=("US",),
            window_keys=("US:2026-08-10",),
            first_seen_at=now - timedelta(minutes=2),
            now=now - timedelta(minutes=2),
        )
        store.resolve_watchdog_incident(now=now - timedelta(minutes=1))
        store.connection.execute(
            "UPDATE watchdog_incidents SET resolved_at = ?",
            ((now + timedelta(minutes=1)).isoformat(),),
        )

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments=(
                {} if early_return == "zero" else {"AAPL": "US"}
            ),
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]


def test_cockpit_legacy_watchdog_generation_is_blue_until_real_scope_set(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.connection.execute(
            "UPDATE protection_scope SET watchdog_generation = NULL"
        )
        store.connection.execute("DELETE FROM protection_scope_generations")

        legacy = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=now,
        )
        proven = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert (legacy["state"], legacy["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert "watchdog_generation_unproven" in legacy["reason_codes"]
    assert heartbeat_eligible(legacy) is False
    assert (proven["state"], proven["overall_color"]) == ("HEALTHY", "GREEN")


@pytest.mark.parametrize("resolved", [False, True])
def test_cockpit_watchdog_corruption_is_red_before_early_return_without_leaking(
    tmp_path: Path,
    resolved: bool,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    secret = "https://watchdog.invalid/private-ledger-token"
    with StateStore(tmp_path / "state.db") as store:
        store.observe_watchdog_incident(
            scope_generation="f" * 64,
            enabled_instruments=1,
            affected_tickers=("AAPL",),
            markets=("US",),
            window_keys=("US:2026-08-10",),
            first_seen_at=now - timedelta(minutes=1),
            now=now,
        )
        if resolved:
            store.resolve_watchdog_incident(now=now + timedelta(seconds=1))
        store.connection.execute(
            "UPDATE watchdog_incidents SET payload_json = ?",
            (secret,),
        )

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={},
            generated_at=now + timedelta(seconds=1),
        )

    rendered = json.dumps(receipt, sort_keys=True)
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]
    assert receipt["recent_runs"] == []
    assert secret not in rendered
    assert "payload_json" not in rendered


def test_cockpit_due_missing_overrides_old_healthy_state(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)  # after the 09:40 deadline
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert receipt["state"] == "BLIND"
    assert receipt["overall_color"] == "RED"
    assert receipt["reason_codes"] == ["deadline_missed"]
    assert receipt["schedule"]["markets"][0]["deadline_state"] == "missing"


def test_cockpit_typed_corruption_is_red_without_raw_payload(tmp_path: Path) -> None:
    secret = "https://heartbeat.example/2f619ea6-secret-token"
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(["US"], now=now - timedelta(minutes=10))
        store.connection.execute(
            "UPDATE protection_scope SET enabled_markets_json = ?",
            (json.dumps([secret]),),
        )

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    rendered = json.dumps(receipt, sort_keys=True)
    assert receipt["state"] == "BLIND"
    assert receipt["overall_color"] == "RED"
    assert receipt["reason_codes"] == ["state_corrupt"]
    assert secret not in rendered


def test_cockpit_missing_market_baseline_is_recovering_blue(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 1, 30, tzinfo=UTC)
    us_expected = latest_expected_market_scan("US", now)
    hk_expected = latest_expected_market_scan("HK", now)
    activated = min(us_expected.expected_at, hk_expected.expected_at) - timedelta(
        minutes=5
    )
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            now=activated,
        )
        _record_good_latest_window(store, "US", now)
        store.observe_protection(
            _observation(
                "market:US",
                "us-healthy",
                us_expected.expected_at,
                full_scan=True,
            )
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "00700": "HK"},
            generated_at=now,
        )

    assert receipt["state"] == "RECOVERING"
    assert receipt["overall_color"] == "BLUE"
    assert "protection_baseline_missing" in receipt["reason_codes"]
    assert receipt["silence"]["affected"] == ["00700"]


def test_cockpit_snapshot_count_mismatch_cannot_be_green(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "MSFT": "US"},
            generated_at=now,
        )

    assert receipt["overall_color"] != "GREEN"
    assert "scope_coverage_mismatch" in receipt["reason_codes"]


def test_cockpit_scope_and_current_config_market_drift_cannot_be_green(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"], now=expected.expected_at - timedelta(minutes=5)
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"00700": "HK"},
            generated_at=now,
        )

    assert receipt["overall_color"] != "GREEN"
    assert "scope_config_mismatch" in receipt["reason_codes"]


def test_cockpit_paused_scope_is_gray(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(["US"], paused=True, now=now)
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert receipt["state"] == "PAUSED"
    assert receipt["overall_color"] == "GRAY"


def test_cockpit_blind_market_is_not_hidden_by_paused_market(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 1, 30, tzinfo=UTC)
    us_expected = latest_expected_market_scan("US", now)
    hk_expected = latest_expected_market_scan("HK", now)
    activated = min(us_expected.expected_at, hk_expected.expected_at) - timedelta(
        minutes=5
    )
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(["US", "HK"], now=activated)
        _record_good_latest_window(store, "US", now)
        store.observe_protection(
            _observation(
                "market:US",
                "us-blind",
                us_expected.expected_at,
                usable=0,
            )
        )
        store.observe_protection(
            _observation(
                "market:HK",
                "hk-paused",
                hk_expected.expected_at,
                paused=True,
            )
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "00700": "HK"},
            generated_at=now,
        )

    assert receipt["state"] == "BLIND"
    assert receipt["overall_color"] == "RED"


def test_cockpit_bad_window_requires_later_recovery_evidence(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    blind_at = expected.deadline_at + timedelta(minutes=1)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.record_protection_window(
            expected.key,
            "US",
            expected.expected_at,
            expected.deadline_at,
            "bad",
            actual_at=blind_at,
            enabled_instruments=1,
            usable_instruments=0,
            affected_tickers=("AAPL",),
            reason_codes=("deadline_missed",),
            now=blind_at,
        )
        stale = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=blind_at,
        )
        store.observe_protection(
            _observation(
                "market:US",
                "deadline-edge",
                blind_at,
                usable=0,
                deadline_missed=True,
            )
        )
        store.observe_protection(
            _observation(
                "market:US",
                "recovery-1",
                blind_at + timedelta(minutes=1),
                full_scan=True,
            )
        )
        blue = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=blind_at + timedelta(minutes=1),
        )
        store.observe_protection(
            _observation(
                "market:US",
                "recovery-2",
                blind_at + timedelta(minutes=2),
                full_scan=True,
            )
        )
        green = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=blind_at + timedelta(minutes=2),
        )

    assert stale["state"] == "BLIND"
    assert "deadline_evidence_unresolved" in stale["reason_codes"]
    assert (blue["state"], blue["overall_color"]) == ("RECOVERING", "BLUE")
    assert (green["state"], green["overall_color"]) == ("HEALTHY", "GREEN")
    assert green["schedule"]["slo_30d"] == {
        "target": 0.99,
        "good": 0,
        "bad": 1,
        "missing": 0,
        "pending": 0,
        "expected": 1,
        "violations": 1,
        "ratio": 0.0,
        "error_rate": 1.0,
        "burn_rate": 100.0,
        "error_budget_consumed": 100.0,
    }


def test_cockpit_slo_counts_immutable_window_outcomes_and_error_budget(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    due = [
        window
        for window in expected_market_scans_between(
            "US", now - timedelta(days=10), now
        )
        if window.deadline_at.astimezone(UTC) <= now
    ][-4:]
    assert len(due) == 4
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=due[0].expected_at - timedelta(minutes=1),
        )
        store.observe_protection(
            _healthy_observation(due[-1].deadline_at.astimezone(UTC))
        )
        store.record_protection_window(
            due[0].key,
            "US",
            due[0].expected_at,
            due[0].deadline_at,
            "good",
            actual_at=due[0].expected_at + timedelta(minutes=1),
            enabled_instruments=1,
            usable_instruments=1,
            now=due[0].expected_at + timedelta(minutes=1),
        )
        store.record_protection_window(
            due[1].key,
            "US",
            due[1].expected_at,
            due[1].deadline_at,
            "bad",
            actual_at=due[1].deadline_at + timedelta(minutes=1),
            enabled_instruments=1,
            usable_instruments=0,
            affected_tickers=("AAPL",),
            reason_codes=("deadline_missed",),
            now=due[1].deadline_at + timedelta(minutes=1),
        )
        store.record_protection_window(
            due[2].key,
            "US",
            due[2].expected_at,
            due[2].deadline_at,
            "pending",
            enabled_instruments=1,
            usable_instruments=0,
            affected_tickers=("AAPL",),
            reason_codes=("partial_coverage",),
            now=due[2].expected_at + timedelta(minutes=1),
        )

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    expected_slo = {
        "good": 1,
        "bad": 1,
        "missing": 1,
        "pending": 1,
        "expected": 4,
        "violations": 3,
        "ratio": 0.25,
        "error_rate": 0.75,
        "burn_rate": 75.0,
        "error_budget_consumed": 75.0,
    }
    assert receipt["schedule"]["markets"][0]["slo_30d"] == expected_slo
    assert receipt["schedule"]["slo_30d"] == {
        "target": 0.99,
        **expected_slo,
    }


def test_cockpit_zero_enabled_ignores_old_scope_and_due_gap(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"], now=expected.expected_at - timedelta(minutes=5)
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={},
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == (
        "UNCONFIGURED",
        "GRAY",
    )


def test_cockpit_active_integrity_overrides_zero_enabled(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    with StateStore(tmp_path / "state.db") as store:
        store.observe_integrity_incident(
            "global",
            "protection_scope",
            "state_corrupt",
            now=now,
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={},
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]


def test_cockpit_activation_after_expected_run_excludes_promise(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(["US"], now=expected.expected_at + timedelta(minutes=5))
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert receipt["schedule"]["slo_30d"]["expected"] == 0
    assert receipt["schedule"]["slo_30d"]["ratio"] is None


@pytest.mark.parametrize("activation_offset", [timedelta(minutes=-5), timedelta(0)])
def test_cockpit_activation_by_expected_run_includes_due_promise(
    tmp_path: Path,
    activation_offset: timedelta,
) -> None:
    now = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(["US"], now=expected.expected_at + activation_offset)
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert receipt["schedule"]["slo_30d"]["expected"] == 1
    assert receipt["schedule"]["slo_30d"]["good"] == 0


def test_cockpit_new_market_after_run_does_not_backfill_same_day(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 21, 0, tzinfo=UTC)  # 17:00 New York
    expected = latest_expected_market_scan("US", now)
    activated = expected.expected_at + timedelta(hours=6, minutes=35)  # 16:00
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(["US"], now=activated)
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert receipt["schedule"]["slo_30d"]["expected"] == 0


@pytest.mark.parametrize("unresolved_market", ["HK", "US"])
def test_cockpit_one_recovered_market_cannot_wash_an_unresolved_bad_market(
    tmp_path: Path,
    unresolved_market: str,
) -> None:
    now = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    expected_by_market = {
        market: latest_expected_market_scan(market, now) for market in ("US", "HK")
    }
    activation = min(
        item.expected_at for item in expected_by_market.values()
    ) - timedelta(minutes=5)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            now=activation,
        )
        for market, expected in expected_by_market.items():
            store.observe_protection(
                _observation(
                    f"market:{market}",
                    f"{market}-initial",
                    expected.expected_at,
                    full_scan=True,
                )
            )
            late = expected.deadline_at + timedelta(minutes=1)
            store.record_protection_window(
                expected.key,
                market,
                expected.expected_at,
                expected.deadline_at,
                "bad",
                actual_at=late,
                enabled_instruments=1,
                usable_instruments=0,
                affected_tickers=("AAPL" if market == "US" else "00700",),
                reason_codes=("deadline_missed",),
                now=late,
            )
            if market != unresolved_market:
                store.observe_protection(
                    _observation(
                        f"market:{market}",
                        f"{market}-blind",
                        late,
                        usable=0,
                        deadline_missed=True,
                    )
                )
                store.observe_protection(
                    _observation(
                        f"market:{market}",
                        f"{market}-recovery-1",
                        late + timedelta(minutes=1),
                        full_scan=True,
                    )
                )
                store.observe_protection(
                    _observation(
                        f"market:{market}",
                        f"{market}-recovery-2",
                        late + timedelta(minutes=2),
                        full_scan=True,
                    )
                )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "00700": "HK"},
            generated_at=now,
        )

    assert receipt["state"] == "BLIND"
    assert "deadline_evidence_unresolved" in receipt["reason_codes"]


def test_cockpit_degraded_outranks_recovering(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 1, 30, tzinfo=UTC)
    us_expected = latest_expected_market_scan("US", now)
    hk_expected = latest_expected_market_scan("HK", now)
    activation = min(us_expected.expected_at, hk_expected.expected_at) - timedelta(
        minutes=5
    )
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            now=activation,
        )
        _record_good_latest_window(store, "US", now)
        store.observe_protection(
            _observation(
                "market:US",
                "us-degraded",
                us_expected.expected_at,
                enabled=2,
                usable=1,
            )
        )
        store.observe_protection(
            _observation(
                "market:HK", "hk-blind", hk_expected.expected_at, usable=0
            )
        )
        store.observe_protection(
            _observation(
                "market:HK",
                "hk-recovery",
                hk_expected.expected_at + timedelta(minutes=1),
                full_scan=True,
            )
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "MSFT": "US", "00700": "HK"},
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == ("DEGRADED", "AMBER")


def test_cockpit_paused_market_prevents_healthy_green(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 1, 30, tzinfo=UTC)
    us_expected = latest_expected_market_scan("US", now)
    hk_expected = latest_expected_market_scan("HK", now)
    activation = min(us_expected.expected_at, hk_expected.expected_at) - timedelta(
        minutes=5
    )
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            now=activation,
        )
        _record_good_latest_window(store, "US", now)
        store.observe_protection(
            _observation(
                "market:US",
                "us-healthy",
                us_expected.expected_at,
                full_scan=True,
            )
        )
        store.observe_protection(
            _observation(
                "market:HK",
                "hk-paused",
                hk_expected.expected_at,
                paused=True,
            )
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "00700": "HK"},
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == ("PAUSED", "GRAY")


@pytest.mark.parametrize(
    ("configured", "attempted", "success", "error", "reason"),
    [
        (False, None, None, None, "delivery_unconfigured"),
        (True, None, None, None, "delivery_unproven"),
        (True, datetime(2026, 8, 10, 13, 29, tzinfo=UTC), False, "timeout", "delivery_unavailable"),
        (True, datetime(2026, 8, 9, 12, 29, tzinfo=UTC), True, None, "delivery_proof_expired"),
    ],
)
def test_cockpit_active_telegram_readiness_is_orthogonal_to_healthy_silence(
    tmp_path: Path,
    configured: bool,
    attempted: datetime | None,
    success: bool | None,
    error: str | None,
    reason: str,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.record_run(
            "stock-scan:US",
            "success",
            _stock_run_detail(
                selected=1,
                evaluated=1,
                fresh_usable=1,
                trusted_usable=1,
                fresh_affected=[],
                trusted_affected=[],
            ),
            started_at=expected.expected_at,
            finished_at=expected.expected_at + timedelta(minutes=1),
        )
        store.record_delivery_state(
            "telegram",
            config_fingerprint=_DELIVERY_FINGERPRINT,
            configured=configured,
            mode="active",
            attempted_at=attempted,
            success=success,
            error_code=error,
            now=now,
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert receipt["state"] == "BLIND"
    assert receipt["overall_color"] == "RED"
    assert receipt["silence"]["state"] == "HEALTHY"
    assert reason in receipt["reason_codes"]


def test_cockpit_delivery_tamper_is_red_and_never_leaks_secret(tmp_path: Path) -> None:
    secret = "https://heartbeat.example/private-token"
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.record_delivery_state(
            "heartbeat",
            config_fingerprint=_DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            now=now,
        )
        store.connection.execute(
            "UPDATE delivery_state SET error_code = ? WHERE channel = 'heartbeat'",
            (secret,),
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    rendered = json.dumps(receipt, sort_keys=True)
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]
    assert secret not in rendered


def test_cockpit_active_external_watcher_without_success_is_unproven(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        for channel in ("telegram", "heartbeat"):
            store.record_delivery_state(
                channel,
                config_fingerprint=_DELIVERY_FINGERPRINT,
                configured=True,
                mode="active",
                now=now,
            )
        # Telegram readiness is proven independently; watcher remains unproven.
        store.record_delivery_state(
            "telegram",
            config_fingerprint=_DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            attempted_at=now,
            success=True,
            now=now,
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert receipt["silence"]["state"] == "HEALTHY"
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert "watcher_unproven" in receipt["reason_codes"]


def test_cockpit_delivery_generation_change_invalidates_old_proof_without_digest(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    old_fingerprint = "d" * 64
    current_fingerprint = "e" * 64
    current = {
        "telegram": current_fingerprint,
        "whatsapp": current_fingerprint,
        "heartbeat": current_fingerprint,
    }
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.record_delivery_state(
            "telegram",
            config_fingerprint=old_fingerprint,
            configured=True,
            mode="active",
            attempted_at=now,
            success=True,
            now=now,
        )
        unproven = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            current_delivery_fingerprints=current,
            generated_at=now,
        )
        store.record_delivery_state(
            "telegram",
            config_fingerprint=current_fingerprint,
            configured=True,
            mode="active",
            attempted_at=now + timedelta(minutes=1),
            success=True,
            now=now + timedelta(minutes=1),
        )
        proven = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            current_delivery_fingerprints=current,
            generated_at=now + timedelta(minutes=1),
        )

    assert (unproven["state"], unproven["overall_color"]) == ("BLIND", "RED")
    assert "delivery_unproven" in unproven["reason_codes"]
    assert unproven["delivery"]["telegram"]["last_success_at"] is None
    assert (proven["state"], proven["overall_color"]) == ("HEALTHY", "GREEN")
    rendered = json.dumps((unproven, proven), sort_keys=True)
    assert old_fingerprint not in rendered
    assert current_fingerprint not in rendered


def test_heartbeat_disable_reenable_same_url_requires_new_success(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    enabled = Settings(
        notifications_enabled=True,
        telegram_bot_token="telegram-secret",
        telegram_chat_id="chat-id",
        heartbeat_enabled=True,
        heartbeat_url="https://watcher.invalid/private-token",
    )
    disabled = enabled.model_copy(update={"heartbeat_enabled": False})
    enabled_fingerprints = delivery_config_fingerprints(enabled)
    disabled_fingerprints = delivery_config_fingerprints(disabled)
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        _record_good_latest_window(store, "US", now)
        finished = expected.expected_at + timedelta(minutes=1)
        store.record_run(
            "stock-scan:US",
            "success",
            _stock_run_detail(
                selected=1,
                evaluated=1,
                fresh_usable=1,
                trusted_usable=1,
                fresh_affected=[],
                trusted_affected=[],
            ),
            started_at=expected.expected_at,
            finished_at=finished,
        )
        for channel in ("telegram", "heartbeat"):
            store.record_delivery_state(
                channel,
                config_fingerprint=enabled_fingerprints[channel],
                configured=True,
                mode="active",
                attempted_at=finished,
                success=True,
                now=finished,
            )
        store.record_delivery_state(
            "heartbeat",
            config_fingerprint=disabled_fingerprints["heartbeat"],
            configured=False,
            mode="preview",
            now=finished + timedelta(minutes=1),
        )

    with StateStore(state_path) as reopened:
        reopened.record_delivery_state(
            "heartbeat",
            config_fingerprint=enabled_fingerprints["heartbeat"],
            configured=True,
            mode="active",
            now=finished + timedelta(minutes=2),
        )
        unproven = build_reliability_cockpit(
            store=reopened,
            enabled_instruments={"AAPL": "US"},
            current_delivery_fingerprints=enabled_fingerprints,
            generated_at=finished + timedelta(minutes=2),
        )
        reopened.record_delivery_state(
            "heartbeat",
            config_fingerprint=enabled_fingerprints["heartbeat"],
            configured=True,
            mode="active",
            attempted_at=finished + timedelta(minutes=3),
            success=True,
            now=finished + timedelta(minutes=3),
        )
        recovered = build_reliability_cockpit(
            store=reopened,
            enabled_instruments={"AAPL": "US"},
            current_delivery_fingerprints=enabled_fingerprints,
            generated_at=finished + timedelta(minutes=3),
        )

    assert "watcher_unproven" in unproven["reason_codes"]
    assert unproven["delivery"]["external_watcher"]["last_success_at"] is None
    # Product readiness is RED until the new generation succeeds, while the
    # transport retry predicate deliberately ignores its own missing proof so
    # the first/recovery ping cannot self-lock.
    assert heartbeat_eligible(unproven) is True
    assert (recovered["state"], recovered["overall_color"]) == (
        "HEALTHY",
        "GREEN",
    )
    assert heartbeat_eligible(recovered) is True


def test_cockpit_tampered_delivery_fingerprint_is_red_without_leaking(
    tmp_path: Path,
) -> None:
    secret = "https://heartbeat.example/private-fingerprint-token"
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.record_delivery_state(
            "telegram",
            config_fingerprint=_DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            now=now,
        )
        store.connection.execute(
            "UPDATE delivery_state SET config_fingerprint = ?",
            (secret,),
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    rendered = json.dumps(receipt, sort_keys=True)
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]
    assert secret not in rendered


def test_cockpit_mixed_delivery_modes_fail_closed(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"], now=expected.expected_at - timedelta(minutes=5)
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.record_delivery_state(
            "telegram",
            config_fingerprint=_DELIVERY_FINGERPRINT,
            configured=False,
            mode="preview",
            now=now,
        )
        store.record_delivery_state(
            "heartbeat",
            config_fingerprint=_DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            now=now,
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert receipt["delivery_mode"] == "ACTIVE"
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert "mode_mismatch" in receipt["reason_codes"]


def test_cockpit_whatsapp_only_is_healthy_and_heartbeat_eligible(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.record_run(
            "stock-scan:US",
            "success",
            _stock_run_detail(
                selected=1,
                evaluated=1,
                fresh_usable=1,
                trusted_usable=1,
                fresh_affected=[],
                trusted_affected=[],
            ),
            started_at=expected.expected_at,
            finished_at=expected.expected_at + timedelta(minutes=1),
        )
        store.record_delivery_state(
            "telegram",
            config_fingerprint=_DELIVERY_FINGERPRINT,
            configured=False,
            mode="preview",
            now=now,
        )
        store.record_delivery_state(
            "whatsapp",
            config_fingerprint=_DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            attempted_at=now,
            success=True,
            now=now,
        )
        store.record_delivery_state(
            "heartbeat",
            config_fingerprint=_DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            now=now,
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert "delivery_unconfigured" not in receipt["reason_codes"]
    assert "mode_mismatch" not in receipt["reason_codes"]
    assert (receipt["silence"]["state"], receipt["silence"]["color"]) == (
        "HEALTHY",
        "GREEN",
    )
    assert heartbeat_eligible(receipt, at=now) is True


@pytest.mark.parametrize("historical_status", ["missing", "pending", "bad"])
def test_cockpit_historical_unresolved_due_window_overrides_today_good(
    tmp_path: Path,
    historical_status: str,
) -> None:
    now = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    due = expected_market_scans_between("US", now - timedelta(days=4), now)
    historical, current = due[-2:]
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"], now=historical.expected_at - timedelta(minutes=5)
        )
        if historical_status != "missing":
            late = historical.deadline_at + timedelta(minutes=1)
            store.record_protection_window(
                historical.key,
                "US",
                historical.expected_at,
                historical.deadline_at,
                historical_status,
                actual_at=(
                    historical.expected_at + timedelta(minutes=1)
                    if historical_status == "pending"
                    else late
                ),
                enabled_instruments=1,
                usable_instruments=0,
                affected_tickers=("AAPL",),
                reason_codes=("no_data",),
                now=(
                    historical.expected_at + timedelta(minutes=1)
                    if historical_status == "pending"
                    else late
                ),
            )
        actual = current.expected_at + timedelta(minutes=1)
        store.record_protection_window(
            current.key,
            "US",
            current.expected_at,
            current.deadline_at,
            "good",
            actual_at=actual,
            last_success_at=actual,
            enabled_instruments=1,
            usable_instruments=1,
            now=actual,
        )
        store.observe_protection(_healthy_observation(actual))
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")


@pytest.mark.parametrize("projection", ["zero", "paused"])
def test_cockpit_validates_all_states_before_inactive_projection(
    tmp_path: Path,
    projection: str,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            paused=projection == "paused",
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.connection.execute(
            "UPDATE protection_state SET snapshot_json = '{broken'"
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments=(
                {} if projection == "zero" else {"AAPL": "US"}
            ),
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]


def test_cockpit_validates_resolved_integrity_rows_without_leaking(
    tmp_path: Path,
) -> None:
    secret = "https://heartbeat.example/resolved-secret"
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"], now=expected.expected_at - timedelta(minutes=5)
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.observe_integrity_incident(
            "global", "protection_scope", "state_corrupt", now=now
        )
        store.resolve_integrity_incident("global", "protection_scope", now=now)
        store.connection.execute(
            "UPDATE integrity_incidents SET reason_code = ?", (secret,)
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    rendered = json.dumps(receipt, sort_keys=True)
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert secret not in rendered


@pytest.mark.parametrize(
    "unusable_ticker",
    ["https://heartbeat.example/private-token", "00700"],
)
def test_cockpit_rejects_unsafe_or_out_of_scope_snapshot_tickers(
    tmp_path: Path,
    unusable_ticker: str,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"], now=expected.expected_at - timedelta(minutes=5)
        )
        persisted_ticker = (
            "AAPL" if unusable_ticker.startswith("https://") else unusable_ticker
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="unsafe-ticker",
                observed_at=expected.expected_at,
                enabled_instruments=2,
                usable_instruments=1,
                unusable_tickers=(persisted_ticker,),
                provider_degraded=True,
            )
        )
        if unusable_ticker.startswith("https://"):
            row = store.connection.execute(
                "SELECT snapshot_json FROM protection_state WHERE scope_key = 'market:US'"
            ).fetchone()
            payload = json.loads(row["snapshot_json"])
            payload["coverage"]["unusable_tickers"] = [unusable_ticker]
            store.connection.execute(
                "UPDATE protection_state SET snapshot_json = ? WHERE scope_key = 'market:US'",
                (json.dumps(payload),),
            )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "MSFT": "US"},
            generated_at=now,
        )

    rendered = json.dumps(receipt, sort_keys=True)
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert unusable_ticker not in rendered


def test_cockpit_same_count_ticker_swap_requires_new_configuration_baseline(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"MSFT": "US"},
            generated_at=now,
        )
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("MSFT",)},
            now=now,
        )
        reset_epoch = build_reliability_cockpit(
            store=store,
            enabled_instruments={"MSFT": "US"},
            generated_at=now,
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="msft-baseline",
                observed_at=now + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        restored = build_reliability_cockpit(
            store=store,
            enabled_instruments={"MSFT": "US"},
            generated_at=now + timedelta(minutes=1),
        )

    assert (receipt["state"], receipt["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert "configuration_baseline_missing" in receipt["reason_codes"]
    assert (reset_epoch["state"], reset_epoch["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert (restored["state"], restored["overall_color"]) == (
        "HEALTHY",
        "GREEN",
    )


def test_cockpit_legacy_scope_identity_is_unproven_blue(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"], now=expected.expected_at - timedelta(minutes=5)
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert "identity_unproven" in receipt["reason_codes"]


def test_cockpit_missing_protection_contract_is_unproven_blue(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes=None,
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes={"US": "a" * 64},
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert "contract_unproven" in receipt["reason_codes"]


def test_cockpit_contract_mismatch_stays_blue_until_new_full_scan(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    old_contract = {"US": "a" * 64}
    new_contract = {"US": "c" * 64}
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes=old_contract,
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        mismatch = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes=new_contract,
            generated_at=now,
        )
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes=new_contract,
            now=now,
        )
        before_scan = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes=new_contract,
            generated_at=now,
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="new-contract-full-scan",
                observed_at=now + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        restored = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes=new_contract,
            generated_at=now + timedelta(minutes=1),
        )

    assert (mismatch["state"], mismatch["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert "configuration_baseline_missing" in mismatch["reason_codes"]
    assert (before_scan["state"], before_scan["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert (restored["state"], restored["overall_color"]) == (
        "HEALTHY",
        "GREEN",
    )


def test_contract_epoch_requires_post_epoch_full_scan_not_newer_update(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    old_contract = {"US": "a" * 64}
    new_contract = {"US": "c" * 64}
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes=old_contract,
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes=new_contract,
            now=now,
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="non-full-new-contract-observation",
                observed_at=now + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=False,
            )
        )
        non_full = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes=new_contract,
            generated_at=now + timedelta(minutes=1),
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="full-new-contract-observation",
                observed_at=now + timedelta(minutes=2),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        full = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes=new_contract,
            generated_at=now + timedelta(minutes=2),
        )

    assert (non_full["state"], non_full["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert "configuration_baseline_missing" in non_full["reason_codes"]
    assert (full["state"], full["overall_color"]) == ("HEALTHY", "GREEN")


@pytest.mark.parametrize("future_evidence", ["scope", "snapshot"])
def test_cockpit_rejects_future_protection_evidence(
    tmp_path: Path,
    future_evidence: str,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    scope_at = (
        now + timedelta(days=2)
        if future_evidence == "scope"
        else expected.expected_at - timedelta(minutes=5)
    )
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=scope_at,
        )
        if future_evidence == "snapshot":
            store.observe_protection(_healthy_observation(now + timedelta(days=2)))

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]


def test_cockpit_rejects_future_delivery_update_without_attempt(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.record_delivery_state(
            "telegram",
            config_fingerprint=_DELIVERY_FINGERPRINT,
            configured=False,
            mode="preview",
            now=now + timedelta(minutes=1),
        )

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]


def test_cockpit_tampered_contract_hash_is_red_without_leaking(
    tmp_path: Path,
) -> None:
    secret = "https://heartbeat.example/contract-token"
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.connection.execute(
            "UPDATE protection_scope SET market_contract_hashes_json = ?",
            (json.dumps({"US": secret}),),
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    rendered = json.dumps(receipt, sort_keys=True)
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]
    assert secret not in rendered


def test_cockpit_projects_provider_wilson_health_without_cache_hits(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    key = ProviderKey(provider="probe", operation="quote", market="US")
    attempts = tuple(
        ProviderAttempt(
            provider=key.provider,
            operation=key.operation,
            market=key.market,
            outcome="success" if index < 20 else "cache_hit",
            latency_ms=1.0,
            attempt_index=index + 1 if index < 20 else 0,
            cache_state="miss" if index < 20 else "fresh",
            observed_at=expected.expected_at,
        )
        for index in range(21)
    )
    runtime = RuntimeState(
        circuits={key.storage_key: CircuitSnapshot()},
        observations={key.storage_key: attempts},
    )
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.save_provider_runtime_state(
            runtime.model_dump(mode="json"), now=expected.expected_at
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    capability = receipt["providers"]["capabilities"][0]
    assert capability["provider"] == "probe"
    assert capability["operation"] == "quote"
    assert capability["market"] == "US"
    assert capability["sample_count"] == 20
    assert capability["success_rate"] == 1.0
    # ProviderRuntime is the single Wilson implementation (95%, z=1.96).
    assert capability["wilson_lower_bound"] == pytest.approx(0.8388698745)
    assert capability["grade"] == "degraded"
    assert capability["circuit_state"] == "closed"


def test_cockpit_provider_health_requires_twenty_real_samples(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    key = ProviderKey(provider="probe", operation="quote", market="US")
    attempts = tuple(
        ProviderAttempt(
            provider=key.provider,
            operation=key.operation,
            market=key.market,
            outcome="success",
            latency_ms=1.0,
            attempt_index=1,
            cache_state="miss",
            observed_at=expected.expected_at,
        )
        for _index in range(19)
    )
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.save_provider_runtime_state(
            RuntimeState(observations={key.storage_key: attempts}).model_dump(
                mode="json"
            ),
            now=expected.expected_at,
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    capability = receipt["providers"]["capabilities"][0]
    assert capability["sample_count"] == 19
    assert capability["grade"] == "insufficient_data"
    assert capability["reasons"] == ["insufficient_samples"]


def test_cockpit_full_stock_run_without_provider_runtime_fails_closed(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    finished = expected.expected_at + timedelta(minutes=1)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        _record_good_latest_window(store, "US", now)
        store.record_run(
            "stock-scan:US",
            "success",
            _stock_run_detail(
                selected=1,
                evaluated=1,
                fresh_usable=1,
                trusted_usable=1,
                fresh_affected=[],
                trusted_affected=[],
            ),
            started_at=expected.expected_at,
            finished_at=finished,
        )

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=finished + timedelta(seconds=30),
        )

    assert receipt["state"] == "BLIND"
    assert receipt["overall_color"] == "RED"
    assert receipt["reason_codes"] == ["state_corrupt"]


@pytest.mark.parametrize(
    "tamper",
    [
        "future_row",
        "future_attempt",
        "future_cache",
        "cache_key",
        "open_without_opened_at",
        "attempt_identity",
        "success_with_timeout",
        "success_with_open_circuit",
        "circuit_open_with_closed_circuit",
    ],
)
def test_cockpit_rejects_provider_runtime_semantic_tamper_without_leaking(
    tmp_path: Path,
    tamper: str,
) -> None:
    secret = "https://provider.example/private-token"
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    key = ProviderKey(provider="probe", operation="quote", market="US")
    attempt = {
        "provider": key.provider,
        "operation": key.operation,
        "market": key.market,
        "outcome": "success",
        "failure_class": "none",
        "error_type": "none",
        "latency_ms": 1.0,
        "attempt_index": 1,
        "cache_state": "miss",
        "circuit_state": "closed",
        "observed_at": expected.expected_at.isoformat(),
    }
    cache_key = f"{key.storage_key}:{'a' * 24}"
    payload = {
        "circuits": {},
        "caches": {
            cache_key: {
                "stored_at": expected.expected_at.isoformat(),
                "value": secret,
            }
        },
        "observations": {key.storage_key: [attempt]},
    }
    saved_at = expected.expected_at
    if tamper == "future_row":
        saved_at = now + timedelta(minutes=1)
    elif tamper == "future_attempt":
        attempt["observed_at"] = (now + timedelta(minutes=1)).isoformat()
    elif tamper == "future_cache":
        payload["caches"][cache_key]["stored_at"] = (
            now + timedelta(minutes=1)
        ).isoformat()
    elif tamper == "cache_key":
        payload["caches"] = {
            secret: {
                "stored_at": expected.expected_at.isoformat(),
                "value": "redacted",
            }
        }
    elif tamper == "open_without_opened_at":
        payload["circuits"] = {
            key.storage_key: {
                "state": "open",
                "consecutive_failures": 3,
                "opened_at": None,
                "half_open_in_flight": 0,
            }
        }
    elif tamper == "attempt_identity":
        attempt["market"] = "HK"
    elif tamper == "success_with_open_circuit":
        attempt["circuit_state"] = "open"
    elif tamper == "circuit_open_with_closed_circuit":
        attempt.update(
            {
                "outcome": "circuit_open",
                "failure_class": "circuit_open",
                "error_type": "circuit_open",
                "attempt_index": 0,
            }
        )
    else:
        attempt["failure_class"] = "timeout"
        attempt["error_type"] = "timeout"

    with StateStore(tmp_path / f"{tamper}.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.save_provider_runtime_state(payload, now=saved_at)
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    rendered = json.dumps(receipt, sort_keys=True)
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]
    assert secret not in rendered


def test_cockpit_rejects_provider_storage_key_tamper_without_leaking(
    tmp_path: Path,
) -> None:
    secret = "https://provider.example/private-token"
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    payload = {
        "circuits": {secret: CircuitSnapshot().model_dump(mode="json")},
        "caches": {},
        "observations": {},
    }
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.save_provider_runtime_state(payload, now=expected.expected_at)
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    rendered = json.dumps(receipt, sort_keys=True)
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]
    assert secret not in rendered


def test_provider_runtime_repair_is_blue_until_post_repair_full_scan(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    secret = "https://provider.example/private-token"
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.save_provider_runtime_state(
            {"circuits": {secret: {}}, "caches": {}, "observations": {}},
            now=expected.expected_at,
        )
        store.observe_integrity_incident(
            "global",
            "provider_runtime",
            "state_corrupt",
            delivery_status="suppressed",
            now=expected.expected_at,
        )
        corrupt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

        digest = store.repair_corrupt_provider_runtime_state(
            ["US"],
            now=now,
        )
        calibrating = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="post-provider-repair-full-scan",
                observed_at=now + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        restored = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now + timedelta(minutes=1),
        )

    assert len(digest) == 64
    assert (corrupt["state"], corrupt["overall_color"]) == ("BLIND", "RED")
    assert (calibrating["state"], calibrating["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert "configuration_baseline_missing" in calibrating["reason_codes"]
    assert (restored["state"], restored["overall_color"]) == (
        "HEALTHY",
        "GREEN",
    )
    assert secret not in json.dumps((corrupt, calibrating, restored), sort_keys=True)


def test_cockpit_rejects_provider_observation_window_above_runtime_limit(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    key = ProviderKey(provider="probe", operation="quote", market="US")
    attempt = ProviderAttempt(
        provider=key.provider,
        operation=key.operation,
        market=key.market,
        outcome="success",
        latency_ms=1.0,
        attempt_index=1,
        cache_state="miss",
        observed_at=expected.expected_at,
    ).model_dump(mode="json")
    payload = {
        "circuits": {},
        "caches": {},
        "observations": {key.storage_key: [attempt] * 10_001},
    }
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.save_provider_runtime_state(payload, now=expected.expected_at)

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]


def test_cockpit_projects_latest_run_fresh_and_trusted_coverage_separately(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    completed = expected.expected_at + timedelta(minutes=1)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="partial-decision",
                observed_at=completed,
                enabled_instruments=2,
                usable_instruments=1,
                unusable_tickers=("MSFT",),
                reason_codes=("evaluation_failed",),
            )
        )
        store.record_run(
            "stock-scan:US",
            "partial",
                {
                    "selected": 2,
                    "evaluated": 1,
                    "notified": 0,
                    "error_tickers": ["MSFT"],
                    "notification_error_tickers": [],
                    "incident_attempted": False,
                    "incident_notified": False,
                    "incident_notification_error": None,
                    "integrity_incident_attempted": 0,
                    "integrity_incident_notified": 0,
                    "integrity_notification_errors": {},
                    "integrity_ledger_error": None,
                    "telegram_probe_attempted": False,
                    "telegram_probe_success": None,
                    "telegram_probe_error": None,
                    "protection_event_id": None,
                    "protection_event_ids": [],
                    "reliability": {
                    "fresh_data_coverage": {
                        "enabled_instruments": 2,
                        "usable_instruments": 2,
                        "fresh_coverage": 1.0,
                        "unusable_tickers": [],
                    },
                    "trusted_decision_coverage": {
                        "enabled_instruments": 2,
                        "usable_instruments": 1,
                        "fresh_coverage": 0.5,
                        "unusable_tickers": ["MSFT"],
                    },
                },
            },
            started_at=expected.expected_at,
            finished_at=completed,
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "MSFT": "US"},
            generated_at=now,
        )

    assert receipt["silence"]["fresh_data"] == {
        "enabled": 2,
        "usable": 2,
        "ratio": 1.0,
        "affected": [],
        "known": True,
    }
    assert receipt["silence"]["trusted_decision"] == {
        "enabled": 2,
        "usable": 1,
        "ratio": 0.5,
        "affected": ["MSFT"],
        "known": True,
    }
    run = receipt["recent_runs"][0]
    assert run == {
        "job": "stock-scan:US",
        "market": "US",
        "status": "partial",
        "started_at": expected.expected_at.astimezone(UTC).isoformat(
            timespec="microseconds"
        ),
        "finished_at": completed.astimezone(UTC).isoformat(
            timespec="microseconds"
        ),
        "selected": 2,
        "evaluated": 1,
        "notified": 0,
    }
    assert "detail" not in run


def test_cockpit_projects_complete_all_market_run_without_copying_by_market(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    completed = now - timedelta(minutes=1)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["HK", "US"],
            enabled_instruments_by_market={"HK": ("00700",), "US": ("AAPL",)},
            now=completed - timedelta(minutes=5),
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="all-us",
                observed_at=completed,
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:HK",
                observation_id="all-hk",
                observed_at=completed,
                enabled_instruments=1,
                usable_instruments=0,
                unusable_tickers=("00700",),
                reason_codes=("evaluation_failed",),
            )
        )
        store.observe_protection(
            BlindnessObservation(
                scope="global",
                observation_id="all-global",
                observed_at=completed,
                enabled_instruments=2,
                usable_instruments=1,
                unusable_tickers=("00700",),
                reason_codes=("evaluation_failed",),
            )
        )
        store.record_run(
            "stock-scan:ALL",
            "partial",
            _stock_run_detail(
                selected=2,
                evaluated=1,
                fresh_usable=2,
                trusted_usable=1,
                fresh_affected=[],
                trusted_affected=["00700"],
            ),
            started_at=completed - timedelta(minutes=1),
            finished_at=completed,
        )

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "00700": "HK"},
            generated_at=now,
        )

    assert receipt["silence"]["fresh_data"] == {
        "enabled": 2,
        "usable": 2,
        "ratio": 1.0,
        "affected": [],
        "known": True,
    }
    assert receipt["silence"]["trusted_decision"] == {
        "enabled": 2,
        "usable": 1,
        "ratio": 0.5,
        "affected": ["00700"],
        "known": True,
    }
    assert receipt["recent_runs"][0]["market"] == "ALL"
    assert receipt["recent_runs"][0]["job"] == "stock-scan:ALL"


def test_cockpit_selects_latest_explicit_or_all_slice_per_market(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    all_completed = now - timedelta(minutes=3)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["HK", "US"],
            enabled_instruments_by_market={"HK": ("00700",), "US": ("AAPL",)},
            now=all_completed - timedelta(minutes=5),
        )
        for scope, ticker in (("market:US", None), ("market:HK", "00700")):
            store.observe_protection(
                BlindnessObservation(
                    scope=scope,
                    observation_id=f"snapshot:{scope}",
                    observed_at=all_completed,
                    enabled_instruments=1,
                    usable_instruments=0 if ticker else 1,
                    unusable_tickers=(ticker,) if ticker else (),
                    reason_codes=("evaluation_failed",) if ticker else (),
                    full_coverage_scan=ticker is None,
                )
            )
        store.observe_protection(
            BlindnessObservation(
                scope="global",
                observation_id="snapshot:global",
                observed_at=all_completed,
                enabled_instruments=2,
                usable_instruments=1,
                unusable_tickers=("00700",),
                reason_codes=("evaluation_failed",),
            )
        )
        all_detail = _stock_run_detail(
            selected=2,
            evaluated=1,
            fresh_usable=2,
            trusted_usable=1,
            fresh_affected=[],
            trusted_affected=["00700"],
        )
        all_detail["reliability"]["by_market"] = {
            "US": _market_run_slice(
                selected=1,
                evaluated=1,
                fresh_usable=1,
                trusted_usable=1,
                fresh_affected=[],
                trusted_affected=[],
            ),
            "HK": _market_run_slice(
                selected=1,
                evaluated=0,
                fresh_usable=1,
                trusted_usable=0,
                fresh_affected=[],
                trusted_affected=["00700"],
            ),
        }
        store.record_run(
            "stock-scan:ALL",
            "partial",
            all_detail,
            finished_at=all_completed,
            started_at=all_completed - timedelta(minutes=1),
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="new-us-failure",
                observed_at=all_completed + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=0,
                unusable_tickers=("AAPL",),
                reason_codes=("evaluation_failed",),
            )
        )
        store.record_run(
            "stock-scan:US",
            "error",
            _stock_run_detail(
                selected=1,
                evaluated=0,
                fresh_usable=0,
                trusted_usable=0,
                fresh_affected=["AAPL"],
                trusted_affected=["AAPL"],
            ),
            finished_at=all_completed + timedelta(minutes=1),
            started_at=all_completed,
        )

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "00700": "HK"},
            generated_at=now,
        )

    assert {run["market"] for run in receipt["recent_runs"]} == {"ALL", "US"}
    assert receipt["silence"]["fresh_data"] == {
        "enabled": 2,
        "usable": 1,
        "ratio": 0.5,
        "affected": ["AAPL"],
        "known": True,
    }
    assert receipt["silence"]["trusted_decision"] == {
        "enabled": 2,
        "usable": 0,
        "ratio": 0.0,
        "affected": ["00700", "AAPL"],
        "known": True,
    }


def test_cockpit_breaks_equal_run_timestamps_by_run_id(tmp_path: Path) -> None:
    completed = datetime(2026, 8, 10, 13, 29, tzinfo=UTC)
    now = completed + timedelta(minutes=1)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=completed - timedelta(minutes=5),
        )
        store.observe_protection(
            BlindnessObservation(
                scope="global",
                observation_id="older-all-global",
                observed_at=completed,
                enabled_instruments=1,
                usable_instruments=0,
                unusable_tickers=("AAPL",),
                reason_codes=("evaluation_failed",),
            )
        )
        all_detail = _stock_run_detail(
            selected=1,
            evaluated=0,
            fresh_usable=0,
            trusted_usable=0,
            fresh_affected=["AAPL"],
            trusted_affected=["AAPL"],
        )
        all_detail["reliability"]["by_market"] = {
            "US": _market_run_slice(
                selected=1,
                evaluated=0,
                fresh_usable=0,
                trusted_usable=0,
                fresh_affected=["AAPL"],
                trusted_affected=["AAPL"],
            )
        }
        store.record_run(
            "stock-scan:ALL",
            "error",
            all_detail,
            started_at=completed,
            finished_at=completed,
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="newer-explicit-us",
                observed_at=completed,
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        store.record_run(
            "stock-scan:US",
            "success",
            _stock_run_detail(
                selected=1,
                evaluated=1,
                fresh_usable=1,
                trusted_usable=1,
                fresh_affected=[],
                trusted_affected=[],
            ),
            started_at=completed,
            finished_at=completed,
        )

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    assert receipt["silence"]["trusted_decision"]["usable"] == 1
    assert receipt["silence"]["trusted_decision"]["affected"] == []


def test_cockpit_reuses_only_all_slices_after_each_market_epoch(
    tmp_path: Path,
) -> None:
    activated = datetime(2026, 8, 10, 13, 20, tzinfo=UTC)
    all_completed = activated + timedelta(minutes=1)
    us_reset = activated + timedelta(minutes=2)
    us_completed = activated + timedelta(minutes=3)
    now = activated + timedelta(minutes=10)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["HK", "US"],
            enabled_instruments_by_market={"HK": ("00700",), "US": ("AAPL",)},
            market_contract_hashes={"HK": "b" * 64, "US": "a" * 64},
            now=activated,
        )
        for market in ("US", "HK"):
            store.observe_protection(
                BlindnessObservation(
                    scope=f"market:{market}",
                    observation_id=f"all:{market}",
                    observed_at=all_completed,
                    enabled_instruments=1,
                    usable_instruments=1,
                    full_coverage_scan=True,
                )
            )
        store.record_run(
            "stock-scan:ALL",
            "success",
            _full_all_run_detail(("US", "HK")),
            started_at=all_completed,
            finished_at=all_completed,
        )
        store.set_protection_scope(
            ["HK", "US"],
            enabled_instruments_by_market={"HK": ("00700",), "US": ("AAPL",)},
            market_contract_hashes={"HK": "b" * 64, "US": "c" * 64},
            now=us_reset,
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="post-reset-us",
                observed_at=us_completed,
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        store.record_run(
            "stock-scan:US",
            "success",
            _stock_run_detail(
                selected=1,
                evaluated=1,
                fresh_usable=1,
                trusted_usable=1,
                fresh_affected=[],
                trusted_affected=[],
            ),
            started_at=us_completed,
            finished_at=us_completed,
        )

        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "00700": "HK"},
            market_contract_hashes={"US": "c" * 64, "HK": "b" * 64},
            generated_at=now,
        )

    assert receipt["silence"]["fresh_data"]["known"] is True
    assert receipt["silence"]["fresh_data"]["enabled"] == 2
    assert receipt["silence"]["fresh_data"]["usable"] == 2


def test_cockpit_all_slices_survive_market_add_and_delete(tmp_path: Path) -> None:
    activated = datetime(2026, 8, 10, 13, 20, tzinfo=UTC)
    all_completed = activated + timedelta(minutes=1)
    hk_added = activated + timedelta(minutes=2)
    hk_completed = activated + timedelta(minutes=3)
    removed = activated + timedelta(minutes=4)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={"US": "a" * 64},
            now=activated,
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="initial-us",
                observed_at=all_completed,
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        store.record_run(
            "stock-scan:ALL",
            "success",
            _full_all_run_detail(("US",)),
            started_at=all_completed,
            finished_at=all_completed,
        )
        store.set_protection_scope(
            ["HK", "US"],
            enabled_instruments_by_market={"HK": ("00700",), "US": ("AAPL",)},
            market_contract_hashes={"HK": "b" * 64, "US": "a" * 64},
            now=hk_added,
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:HK",
                observation_id="new-hk",
                observed_at=hk_completed,
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        store.record_run(
            "stock-scan:HK",
            "success",
            _stock_run_detail(
                selected=1,
                evaluated=1,
                fresh_usable=1,
                trusted_usable=1,
                fresh_affected=[],
                trusted_affected=[],
            ),
            started_at=hk_completed,
            finished_at=hk_completed,
        )
        added = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "00700": "HK"},
            market_contract_hashes={"US": "a" * 64, "HK": "b" * 64},
            generated_at=hk_completed,
        )
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={"US": "a" * 64},
            now=removed,
        )
        deleted = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes={"US": "a" * 64},
            generated_at=removed,
        )

    assert added["silence"]["fresh_data"]["known"] is True
    assert added["silence"]["fresh_data"]["enabled"] == 2
    assert deleted["silence"]["fresh_data"]["known"] is True
    assert deleted["silence"]["fresh_data"]["enabled"] == 1


def test_cockpit_rejects_all_run_missing_a_market_already_in_scope(
    tmp_path: Path,
) -> None:
    activated = datetime(2026, 8, 10, 13, 20, tzinfo=UTC)
    completed = activated + timedelta(minutes=1)
    detail = _stock_run_detail(
        selected=1,
        evaluated=1,
        fresh_usable=1,
        trusted_usable=1,
        fresh_affected=[],
        trusted_affected=[],
    )
    detail["reliability"]["by_market"] = {
        "US": _market_run_slice(
            selected=1,
            evaluated=1,
            fresh_usable=1,
            trusted_usable=1,
            fresh_affected=[],
            trusted_affected=[],
        )
    }
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["HK", "US"],
            enabled_instruments_by_market={"HK": ("00700",), "US": ("AAPL",)},
            now=activated,
        )
        store.record_run(
            "stock-scan:ALL",
            "success",
            detail,
            started_at=completed,
            finished_at=completed,
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "00700": "HK"},
            generated_at=completed,
        )

    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]


def test_cockpit_rejects_malicious_run_detail_without_leaking(
    tmp_path: Path,
) -> None:
    secret = "https://heartbeat.example/run-private-token"
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.record_run(
            "stock-scan:US",
            "success",
            {"private_payload": secret},
            now=expected.expected_at,
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

    rendered = json.dumps(receipt, sort_keys=True)
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]
    assert secret not in rendered


def test_run_log_repair_is_blue_until_post_repair_full_scan(
    tmp_path: Path,
) -> None:
    secret = "https://heartbeat.example/run-private-token"
    now = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    expected = latest_expected_market_scan("US", now)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=expected.expected_at - timedelta(minutes=5),
        )
        store.observe_protection(_healthy_observation(expected.expected_at))
        store.record_run(
            "stock-scan:US",
            "success",
            {"private_payload": secret},
            now=expected.expected_at,
        )
        store.observe_integrity_incident(
            "global",
            "run_log",
            "state_corrupt",
            delivery_status="suppressed",
            now=expected.expected_at,
        )
        corrupt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )

        digests = store.repair_corrupt_run_log(
            affected_markets=["US"],
            now=now,
        )
        calibrating = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now,
        )
        store.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="post-run-repair-full-scan",
                observed_at=now + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        restored = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            generated_at=now + timedelta(minutes=1),
        )

    assert len(digests) == 1 and len(digests[0]) == 64
    assert (corrupt["state"], corrupt["overall_color"]) == ("BLIND", "RED")
    assert (calibrating["state"], calibrating["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert (restored["state"], restored["overall_color"]) == (
        "HEALTHY",
        "GREEN",
    )
    assert secret not in json.dumps((corrupt, calibrating, restored), sort_keys=True)
