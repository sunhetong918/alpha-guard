from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import sqlite3
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import main
from config import AIFilterConfig, Settings, load_news_config, load_rules_config
from reliability import (
    CircuitSnapshot,
    ProviderKey,
    ProviderRuntime,
    ProviderUnavailableError,
    evaluate_snapshot_reliability,
    gate_snapshot_for_decision,
)
from state import ProtectionState, StateStore, protection_contract_version
from state.cockpit import build_reliability_cockpit as _build_reliability_cockpit
from state.store import instrument_set_hash
from notifier.heartbeat import (
    delivery_config_fingerprints,
    heartbeat_eligible,
    ping_heartbeat,
)
from notifier.whatsapp import WhatsAppDeliveryResult


runner = CliRunner()


def build_reliability_cockpit(**kwargs):
    kwargs.setdefault(
        "current_delivery_fingerprints",
        delivery_config_fingerprints(main.get_settings()),
    )
    return _build_reliability_cockpit(**kwargs)


def _ready_settings() -> Settings:
    return Settings(
        notifications_enabled=True,
        telegram_bot_token="test-token",
        telegram_chat_id="test-chat",
    )


def _ready_heartbeat_settings() -> Settings:
    return Settings(
        notifications_enabled=True,
        telegram_bot_token="test-token",
        telegram_chat_id="test-chat",
        heartbeat_enabled=True,
        heartbeat_url="https://heartbeat.example/private-token",
    )


def _ready_whatsapp_only_settings() -> Settings:
    return Settings(
        whatsapp_enabled=True,
        whatsapp_access_token="whatsapp-test-token",
        whatsapp_phone_number_id="123456789",
        whatsapp_default_to="8613800000000",
        whatsapp_signal_template_name="alpha_guard_signal",
        whatsapp_incident_template_name="alpha_guard_incident",
        whatsapp_trust_template_name="alpha_guard_trust",
    )


def test_notify_readiness_accepts_either_mobile_channel() -> None:
    assert main._notification_error(_ready_settings()) is None
    assert main._notification_error(_ready_whatsapp_only_settings()) is None
    assert main._notification_error(Settings()) is not None


def test_whatsapp_only_records_disabled_telegram_as_preview(tmp_path) -> None:
    settings = _ready_whatsapp_only_settings()
    now = main.datetime(2026, 8, 10, 13, 25, tzinfo=main.UTC)

    with StateStore(tmp_path / "state.db") as store:
        main._record_mobile_delivery_modes(
            store,
            settings,
            mode="active",
            now=now,
        )
        states = store.delivery_states(not_after=now)

    assert states["telegram"]["configured"] is False
    assert states["telegram"]["mode"] == "preview"
    assert states["whatsapp"]["configured"] is True
    assert states["whatsapp"]["mode"] == "active"


def test_signal_marks_business_event_only_after_all_channels_accept(
    monkeypatch, tmp_path
) -> None:
    settings = _ready_settings().model_copy(
        update={
            "whatsapp_enabled": True,
            "whatsapp_access_token": "whatsapp-test-token",
            "whatsapp_phone_number_id": "123456789",
            "whatsapp_default_to": "8613800000000",
            "whatsapp_signal_template_name": "alpha_guard_signal",
            "whatsapp_incident_template_name": "alpha_guard_incident",
            "whatsapp_trust_template_name": "alpha_guard_trust",
        }
    )
    telegram_calls = 0
    whatsapp_calls = 0

    async def telegram(_payload, settings=None):
        nonlocal telegram_calls
        del settings
        telegram_calls += 1

    class StubWhatsApp:
        def __init__(self, _settings):
            del _settings

        def send_template(self, **_kwargs):
            nonlocal whatsapp_calls
            whatsapp_calls += 1
            return WhatsAppDeliveryResult(
                whatsapp_calls > 1,
                "accepted" if whatsapp_calls > 1 else "timeout",
            )

    monkeypatch.setattr(main, "send_signal", telegram)
    monkeypatch.setattr("notifier.mobile.WhatsAppNotifier", StubWhatsApp)
    instrument = _enabled_aapl(cooldown_hours=0)
    buy = _decision_result(
        "BUY_REVIEW",
        buy_status="TRIGGERED",
        buy_evidence=[_evidence("buy-price", "price_below")],
    )
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(tmp_path / "state.db") as store:
        with pytest.raises(main._MobileDeliveryRejected):
            asyncio.run(
                main._apply_notification_state(
                    store,
                    ticker="AAPL",
                    instrument=instrument,
                    result=buy,
                    snapshot=_snapshot(),
                    settings=settings,
                    now=at,
                )
            )
        assert asyncio.run(
            main._apply_notification_state(
                store,
                ticker="AAPL",
                instrument=instrument,
                result=buy,
                snapshot=_snapshot(),
                settings=settings,
                now=at + timedelta(seconds=1),
            )
        )

    assert telegram_calls == 1
    assert whatsapp_calls == 2


def _enabled_aapl(*, cooldown_hours: float = 24.0):
    return (
        load_rules_config()
        .watchlist["AAPL"]
        .model_copy(update={"enabled": True, "alert_cooldown_hours": cooldown_hours})
    )


def _enabled_price_aapl(*, cooldown_hours: float = 24.0):
    instrument = _enabled_aapl(cooldown_hours=cooldown_hours)
    return instrument.model_copy(
        update={
            "sell_rules": [instrument.sell_rules[0]],
            "buy_rules": [instrument.buy_rules[0]],
        }
    )


def _snapshot(price: float = 160.0) -> dict:
    return {
        "ticker": "AAPL",
        "market": "US",
        "name": "Apple probe",
        "price": price,
        "pe_ttm": 20.0,
        "pb": 8.0,
        "roe": 25.0,
        "provider": "test-provider",
        "source": {
            "price": "test-provider",
            "pe_ttm": "test-provider",
            "roe": "test-provider",
        },
        "retrieved_at": "2026-08-10T00:00:00+00:00",
        "as_of": "2026-08-10T00:00:00+00:00",
        "currency": "USD",
        "quality_issues": [],
        "field_metadata": {
            "price": {
                "provider": "test-provider",
                "source_as_of": "2026-08-10T00:00:00+00:00",
                "observed_at": "2026-08-10T07:30:00+00:00",
                "time_basis": "source_event",
            },
            "pe_ttm": {
                "provider": "test-provider",
                "source_as_of": None,
                "observed_at": "2026-08-10T07:30:00+00:00",
                "time_basis": "observed_only",
            },
            "roe": {
                "provider": "test-provider",
                "source_as_of": None,
                "observed_at": "2026-08-10T07:30:00+00:00",
                "time_basis": "observed_only",
            },
        },
    }


def _reliable_fake(snapshot_factory):
    def fake_stock(_ticker, _market, **kwargs):
        snapshot = snapshot_factory()
        report = evaluate_snapshot_reliability(
            snapshot,
            kwargs["required_fields"],
            kwargs["freshness_policies"],
            kwargs["freshness_context"],
            future_tolerance_seconds=kwargs["future_tolerance_seconds"],
        )
        return gate_snapshot_for_decision(
            snapshot, report, kwargs["required_fields"]
        )

    return fake_stock


def _snapshot_at(at, price: float = 160.0) -> dict:
    snapshot = _snapshot(price)
    stamp = at.isoformat()
    snapshot["retrieved_at"] = stamp
    snapshot["as_of"] = stamp
    for metadata in snapshot["field_metadata"].values():
        metadata["observed_at"] = stamp
    snapshot["field_metadata"]["price"]["source_as_of"] = stamp
    return snapshot


def _evidence(
    rule_id: str,
    rule_type: str,
    *,
    status: str = "TRIGGERED",
    actual: float | None = 160.0,
    operator: str = "<=",
    threshold: float = 165.0,
    unit: str = "USD",
) -> dict:
    return {
        "rule_id": rule_id,
        "rule_type": rule_type,
        "status": status,
        "actual_value": actual,
        "operator": operator,
        "threshold": threshold,
        "unit": unit,
        "reason": "probe evidence",
    }


def _decision_result(
    decision: str,
    *,
    sell_status: str = "NOT_TRIGGERED",
    buy_status: str = "NOT_TRIGGERED",
    sell_evidence: list[dict] | None = None,
    buy_evidence: list[dict] | None = None,
) -> dict:
    return {
        "ticker": "AAPL",
        "name": "Apple probe",
        "price": 160.0,
        "decision": decision,
        "sell_status": sell_status,
        "buy_status": buy_status,
        "evidence": {
            "sell": sell_evidence or [],
            "buy": buy_evidence or [],
        },
    }


def test_validate_is_local_and_succeeds(monkeypatch) -> None:
    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("validate must not access a market provider")

    monkeypatch.setattr(main, "get_stock", network_forbidden)

    result = runner.invoke(main.app, ["validate"])

    assert result.exit_code == 0, result.output
    assert "配置校验通过" in result.output
    assert "外发通知：禁用" in result.output


def test_dry_run_uses_fixture_without_network_or_state(monkeypatch, tmp_path) -> None:
    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run must not access a market provider")

    def state_forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run must not create persistent state")

    monkeypatch.setattr(main, "get_stock", network_forbidden)
    monkeypatch.setattr(main, "StateStore", state_forbidden)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    result = runner.invoke(main.app, ["dry-run", "--json"])

    assert result.exit_code == 0, result.output
    assert "BUY_REVIEW" in result.output
    assert "SELL_REVIEW" in result.output
    assert "SYNTHETIC_FIXTURE" in result.output
    assert '"freshness_claimed": false' in result.output
    assert not (tmp_path / "state.db").exists()


def test_real_scan_passes_complete_reliability_contract_and_shared_runtime(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_aapl()}}
    )
    runtime = ProviderRuntime()
    captured: dict = {}
    delegate = _reliable_fake(_snapshot)

    def capture(ticker, market, **kwargs):
        captured.update({"ticker": ticker, "market": market, **kwargs})
        return delegate(ticker, market, **kwargs)

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", capture)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")
    replay_now = main.datetime(2026, 8, 10, 7, 30, tzinfo=main.UTC)

    outcome = asyncio.run(
        main.run_stock_scan(
            record_run=False,
            provider_runtime=runtime,
            now=replay_now,
        )
    )

    assert outcome["evaluated"] == 1
    assert captured["ticker"] == "AAPL"
    assert captured["market"] == "US"
    assert captured["required_fields"] == {"price", "pe_ttm", "roe"}
    assert captured["freshness_policies"] is rules.reliability.freshness.fields
    assert captured["freshness_context"].evaluated_at == replay_now
    assert captured["freshness_context"].market_phase == "pre_open"
    assert captured["provider_runtime"] is runtime
    assert captured["timeout_seconds"] == 10


def test_real_run_log_is_safe_cockpit_evidence(monkeypatch, tmp_path) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    state_path = tmp_path / "state.db"
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    asyncio.run(main.run_stock_scan(market="US", now=at))

    with StateStore(state_path) as store:
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes={
                "US": protection_contract_version(rules, "US")
            },
            generated_at=at,
        )

    assert (receipt["state"], receipt["overall_color"]) == (
        "HEALTHY",
        "GREEN",
    )
    assert receipt["silence"]["fresh_data"]["known"] is True
    assert receipt["silence"]["trusted_decision"]["known"] is True
    assert receipt["recent_runs"][0]["job"] == "stock-scan:US"


def test_hard_provider_failure_is_sanitized_and_summarized(monkeypatch, tmp_path) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_aapl()}}
    )
    key = ProviderKey(provider="yfinance", operation="info", market="US")

    def fail(*_args, **_kwargs):
        raise ProviderUnavailableError(key, (), CircuitSnapshot())

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", fail)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")
    outcome = asyncio.run(
        main.run_stock_scan(
            record_run=False,
            now=main.datetime(2026, 8, 10, 7, 30, tzinfo=main.UTC),
        )
    )

    assert outcome["errors"] == {"AAPL": "ProviderUnavailableError"}
    assert outcome["reliability"]["provider_capability_failures"] == [
        {
            "provider": "yfinance",
            "operation": "info",
            "market": "US",
            "reason": "provider_unavailable",
            "circuit": "closed",
        }
    ]


def test_cold_activation_after_old_deadline_does_not_backfill_bad_window(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    at = main.datetime(2026, 8, 10, 16, 0, tzinfo=main.UTC)
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    state_path = tmp_path / "state.db"
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(record_run=False, now=at))

    assert outcome["protection"]["state"] == "HEALTHY"
    with StateStore(state_path) as store:
        assert store.protection_windows() == []


def test_new_market_gets_new_epoch_without_erasing_retained_market_deadline(
    monkeypatch, tmp_path
) -> None:
    us = _enabled_price_aapl()
    hk = _enabled_price_aapl().model_copy(
        update={"name": "HK probe", "market": "HK", "currency": "HKD"}
    )
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": us, "0700.HK": hk}}
    )
    state_path = tmp_path / "state.db"
    us_activated = main.datetime(2026, 8, 10, 12, 0, tzinfo=main.UTC)
    completed = main.datetime(2026, 8, 10, 16, 0, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={
                "US": protection_contract_version(rules, "US")
            },
            now=us_activated,
        )

    def market_snapshot(ticker, market, **kwargs):
        snapshot = _snapshot_at(completed)
        snapshot["ticker"] = ticker
        snapshot["market"] = market
        report = evaluate_snapshot_reliability(
            snapshot,
            kwargs["required_fields"],
            kwargs["freshness_policies"],
            kwargs["freshness_context"],
            future_tolerance_seconds=kwargs["future_tolerance_seconds"],
        )
        return gate_snapshot_for_decision(
            snapshot, report, kwargs["required_fields"]
        )

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", market_snapshot)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(record_run=False, now=completed))

    assert outcome["protection"]["state"] == "RECOVERING"
    with StateStore(state_path) as store:
        scope = store.get_protection_scope()
        windows = store.protection_windows()
    assert scope is not None
    assert scope["market_epochs"]["US"] == us_activated.isoformat(
        timespec="microseconds"
    )
    assert scope["market_epochs"]["HK"] == completed.isoformat(
        timespec="microseconds"
    )
    assert scope["market_instrument_hashes"] == {
        "US": instrument_set_hash(("AAPL",)),
        "HK": instrument_set_hash(("0700.HK",)),
    }
    assert [(window["market"], window["status"]) for window in windows] == [
        ("US", "bad")
    ]


def test_production_scan_passes_exact_instrument_identity_to_scope(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 30, tzinfo=main.UTC)
    captured: dict[str, object] = {}
    original = StateStore.set_protection_scope

    def capture_scope(store, markets, **kwargs):
        captured["markets"] = markets
        captured.update(kwargs)
        return original(store, markets, **kwargs)

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(
        main, "get_stock", _reliable_fake(lambda: _snapshot_at(at))
    )
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(StateStore, "set_protection_scope", capture_scope)

    asyncio.run(main.run_stock_scan(record_run=False, now=at))

    assert captured["markets"] == ["US"]
    assert captured["enabled_instruments_by_market"] == {"US": ("AAPL",)}
    assert captured["market_contract_hashes"] == {
        "US": protection_contract_version(rules, "US")
    }


@pytest.mark.parametrize("change", ["ttl", "rule"])
def test_contract_change_requires_new_full_scan_before_cockpit_green(
    monkeypatch,
    tmp_path,
    change: str,
) -> None:
    base = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    if change == "ttl":
        freshness = base.reliability.freshness
        fields = dict(freshness.fields)
        fields["price"] = fields["price"].model_copy(
            update={
                "max_observation_age_seconds": (
                    fields["price"].max_observation_age_seconds + 1
                )
            }
        )
        changed = base.model_copy(
            update={
                "reliability": base.reliability.model_copy(
                    update={
                        "freshness": freshness.model_copy(update={"fields": fields})
                    }
                )
            }
        )
    else:
        instrument = base.watchlist["AAPL"]
        buy_rules = list(instrument.buy_rules)
        buy_rules[0] = buy_rules[0].model_copy(
            update={"value": buy_rules[0].value + 1}
        )
        changed = base.model_copy(
            update={
                "watchlist": {
                    "AAPL": instrument.model_copy(update={"buy_rules": buy_rules})
                }
            }
        )
    state_path = tmp_path / f"{change}.db"
    scan_at = main.datetime(2026, 8, 10, 16, 0, tzinfo=main.UTC)
    current = {"at": scan_at}
    monkeypatch.setattr(main, "load_rules_config", lambda: base)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(lambda: _snapshot_at(current["at"])),
    )
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    asyncio.run(main.run_stock_scan(record_run=False, now=scan_at))
    changed_contract = {"US": protection_contract_version(changed, "US")}
    with StateStore(state_path) as store:
        before_rescan = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes=changed_contract,
            generated_at=scan_at,
        )
    monkeypatch.setattr(main, "load_rules_config", lambda: changed)
    current["at"] = scan_at + timedelta(minutes=1)
    asyncio.run(main.run_stock_scan(record_run=False, now=current["at"]))
    with StateStore(state_path) as store:
        restored = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes=changed_contract,
            generated_at=current["at"],
        )

    assert (before_rescan["state"], before_rescan["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert "configuration_baseline_missing" in before_rescan["reason_codes"]
    assert (restored["state"], restored["overall_color"]) == (
        "HEALTHY",
        "GREEN",
    )


def test_cosmetic_and_cooldown_changes_keep_existing_contract_green(
    monkeypatch,
    tmp_path,
) -> None:
    base = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    instrument = base.watchlist["AAPL"]
    buy_rules = list(instrument.buy_rules)
    buy_rules[0] = buy_rules[0].model_copy(update={"note": "new display note"})
    changed = base.model_copy(
        update={
            "watchlist": {
                "AAPL": instrument.model_copy(
                    update={
                        "name": "Renamed Apple",
                        "alert_cooldown_hours": 72.0,
                        "buy_rules": buy_rules,
                    }
                )
            }
        }
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 16, 0, tzinfo=main.UTC)
    monkeypatch.setattr(main, "load_rules_config", lambda: base)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    asyncio.run(main.run_stock_scan(record_run=False, now=at))
    current_contract = {"US": protection_contract_version(changed, "US")}
    with StateStore(state_path) as store:
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes=current_contract,
            generated_at=at,
        )

    assert protection_contract_version(base, "US") == current_contract["US"]
    assert (receipt["state"], receipt["overall_color"]) == (
        "HEALTHY",
        "GREEN",
    )


def test_all_market_scan_persists_distinct_market_baselines_for_cockpit(
    monkeypatch,
    tmp_path,
) -> None:
    us = _enabled_price_aapl()
    hk = us.model_copy(
        update={"name": "HK probe", "market": "HK", "currency": "HKD"}
    )
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": us, "0700.HK": hk}}
    )
    at = main.datetime(2026, 8, 10, 16, 0, tzinfo=main.UTC)
    state_path = tmp_path / "state.db"

    def market_snapshot(ticker, market, **kwargs):
        snapshot = _snapshot_at(at)
        snapshot.update({"ticker": ticker, "market": market})
        report = evaluate_snapshot_reliability(
            snapshot,
            kwargs["required_fields"],
            kwargs["freshness_policies"],
            kwargs["freshness_context"],
            future_tolerance_seconds=kwargs["future_tolerance_seconds"],
        )
        return gate_snapshot_for_decision(
            snapshot, report, kwargs["required_fields"]
        )

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", market_snapshot)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    asyncio.run(main.run_stock_scan(record_run=True, now=at))

    with StateStore(state_path) as store:
        us_state = store.load_protection_state("market:US")
        hk_state = store.load_protection_state("market:HK")
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "0700.HK": "HK"},
            market_contract_hashes={
                market: protection_contract_version(rules, market)
                for market in ("US", "HK")
            },
            generated_at=at,
        )
    assert us_state is not None and us_state.state is ProtectionState.HEALTHY
    assert hk_state is not None and hk_state.state is ProtectionState.HEALTHY
    assert us_state.coverage.enabled_instruments == 1
    assert hk_state.coverage.enabled_instruments == 1
    assert (receipt["state"], receipt["overall_color"]) == ("HEALTHY", "GREEN")
    assert receipt["silence"]["fresh_data"]["known"] is True
    assert receipt["silence"]["trusted_decision"]["known"] is True
    assert receipt["recent_runs"][0]["market"] == "ALL"


@pytest.mark.parametrize("failed_market", ["US", "HK"])
def test_all_market_scan_failure_is_scoped_to_its_market(
    monkeypatch,
    tmp_path,
    failed_market: str,
) -> None:
    us = _enabled_price_aapl()
    hk = us.model_copy(
        update={"name": "HK probe", "market": "HK", "currency": "HKD"}
    )
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": us, "0700.HK": hk}}
    )
    at = main.datetime(2026, 8, 10, 16, 0, tzinfo=main.UTC)
    state_path = tmp_path / "state.db"
    delegate = _reliable_fake(lambda: _snapshot_at(at))

    def one_market_fails(ticker, market, **kwargs):
        if market == failed_market:
            raise RuntimeError("private provider failure")
        snapshot = delegate(ticker, market, **kwargs)
        snapshot["ticker"] = ticker
        snapshot["market"] = market
        return snapshot

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", one_market_fails)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    asyncio.run(main.run_stock_scan(record_run=True, now=at))

    with StateStore(state_path) as store:
        states = {
            market: store.load_protection_state(f"market:{market}")
            for market in ("US", "HK")
        }
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US", "0700.HK": "HK"},
            market_contract_hashes={
                market: protection_contract_version(rules, market)
                for market in ("US", "HK")
            },
            generated_at=at,
        )
    healthy_market = "HK" if failed_market == "US" else "US"
    assert states[failed_market] is not None
    assert states[failed_market].state is ProtectionState.BLIND
    assert states[failed_market].coverage.usable_instruments == 0
    assert states[healthy_market] is not None
    assert states[healthy_market].state is ProtectionState.HEALTHY
    assert states[healthy_market].coverage.usable_instruments == 1
    failed_ticker = "AAPL" if failed_market == "US" else "0700.HK"
    assert receipt["silence"]["fresh_data"] == {
        "enabled": 2,
        "usable": 1,
        "ratio": 0.5,
        "affected": [failed_ticker],
        "known": True,
    }
    assert receipt["silence"]["trusted_decision"] == {
        "enabled": 2,
        "usable": 1,
        "ratio": 0.5,
        "affected": [failed_ticker],
        "known": True,
    }


@pytest.mark.parametrize(
    ("failed_ticker", "expected_status", "expected_evaluated"),
    [("MSFT", "success", 1), ("AAPL", "error", 0)],
)
def test_include_disabled_does_not_pollute_protected_run_or_signal_state(
    monkeypatch,
    tmp_path,
    failed_ticker: str,
    expected_status: str,
    expected_evaluated: int,
) -> None:
    enabled = _enabled_price_aapl()
    disabled = enabled.model_copy(update={"name": "Disabled preview", "enabled": False})
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": enabled, "MSFT": disabled}}
    )
    at = main.datetime(2026, 8, 10, 16, 0, tzinfo=main.UTC)
    state_path = tmp_path / "state.db"

    def mixed_fetch(ticker, market, **kwargs):
        if ticker == failed_ticker:
            raise RuntimeError("private preview failure")
        snapshot = _snapshot_at(at)
        snapshot.update({"ticker": ticker, "market": market})
        report = evaluate_snapshot_reliability(
            snapshot,
            kwargs["required_fields"],
            kwargs["freshness_policies"],
            kwargs["freshness_context"],
            future_tolerance_seconds=kwargs["future_tolerance_seconds"],
        )
        return gate_snapshot_for_decision(
            snapshot, report, kwargs["required_fields"]
        )

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", mixed_fetch)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(
        main.run_stock_scan(include_disabled=True, record_run=True, now=at)
    )

    with StateStore(state_path) as store:
        run = store.run_records(not_after=at, job="stock-scan:ALL")[0]
        disabled_signal_rows = store.connection.execute(
            "SELECT COUNT(*) FROM signal_state WHERE signal_key LIKE 'MSFT:%'"
        ).fetchone()[0]

    assert outcome["selected"] == 2
    assert outcome["status"] == expected_status
    assert run["status"] == expected_status
    assert run["detail"]["selected"] == 1
    assert run["detail"]["evaluated"] == expected_evaluated
    assert disabled_signal_rows == 0


def test_only_disabled_scan_records_zero_protection_and_cockpit_gray(
    monkeypatch,
    tmp_path,
) -> None:
    disabled = _enabled_price_aapl().model_copy(update={"enabled": False})
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": disabled}}
    )
    at = main.datetime(2026, 8, 10, 16, 0, tzinfo=main.UTC)
    state_path = tmp_path / "state.db"
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(
        main, "get_stock", _reliable_fake(lambda: _snapshot_at(at))
    )
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    asyncio.run(main.run_stock_scan(include_disabled=True, record_run=True, now=at))

    with StateStore(state_path) as store:
        run = store.run_records(not_after=at, job="stock-scan:ALL")[0]
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={},
            market_contract_hashes={},
            generated_at=at,
        )
        signal_rows = store.connection.execute(
            "SELECT COUNT(*) FROM signal_state"
        ).fetchone()[0]

    assert run["status"] == "success"
    assert run["detail"]["selected"] == 0
    assert run["detail"]["evaluated"] == 0
    assert (receipt["state"], receipt["overall_color"]) == (
        "UNCONFIGURED",
        "GRAY",
    )
    assert signal_rows == 0


def test_all_market_scan_delivers_only_global_incident_and_keeps_market_audit(
    monkeypatch,
    tmp_path,
) -> None:
    us = _enabled_price_aapl()
    hk = us.model_copy(
        update={"name": "HK probe", "market": "HK", "currency": "HKD"}
    )
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": us, "0700.HK": hk}}
    )
    at = main.datetime(2026, 8, 10, 16, 0, tzinfo=main.UTC)
    state_path = tmp_path / "state.db"
    delegate = _reliable_fake(lambda: _snapshot_at(at))
    delivered_scopes: list[str] = []

    def hk_fails(ticker, market, **kwargs):
        if market == "HK":
            raise RuntimeError("private provider failure")
        snapshot = delegate(ticker, market, **kwargs)
        snapshot.update({"ticker": ticker, "market": market})
        return snapshot

    async def ignore_signal(_payload, settings=None):
        del settings

    async def capture_incident(payload, settings=None):
        del settings
        delivered_scopes.append(payload["scope"])

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", hk_fails)
    monkeypatch.setattr(main, "send_signal", ignore_signal)
    monkeypatch.setattr(main, "send_incident", capture_incident)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(
        main.run_stock_scan(notify=True, record_run=False, now=at)
    )

    with StateStore(state_path) as store:
        events = store.protection_events()
        global_events = [event for event in events if event["scope_key"] == "global"]
        market_events = [
            event for event in events if event["scope_key"].startswith("market:")
        ]
        assert store.pending_current_incident_event("market:US") is None
        assert store.pending_current_incident_event("market:HK") is None
    assert delivered_scopes == ["global"]
    assert len(global_events) == 1
    assert global_events[0]["delivery_status"] == "sent"
    assert market_events
    assert all(event["delivery_status"] == "suppressed" for event in market_events)
    assert outcome["protection_event_id"] == global_events[0]["id"]
    assert set(outcome["protection_event_ids"]) == {
        event["id"] for event in events
    }


def test_late_all_market_scan_audits_deadline_and_recovery_edges_per_scope(
    monkeypatch,
    tmp_path,
) -> None:
    us = _enabled_price_aapl()
    hk = us.model_copy(
        update={"name": "HK probe", "market": "HK", "currency": "HKD"}
    )
    base_rules = load_rules_config()
    freshness = base_rules.reliability.freshness
    freshness_fields = dict(freshness.fields)
    freshness_fields["price"] = freshness_fields["price"].model_copy(
        update={"max_observation_age_seconds": 3_600.0}
    )
    rules = base_rules.model_copy(
        update={
            "watchlist": {"AAPL": us, "0700.HK": hk},
            "reliability": base_rules.reliability.model_copy(
                update={
                    "freshness": freshness.model_copy(
                        update={"fields": freshness_fields}
                    )
                }
            ),
        }
    )
    activated = main.datetime(2026, 8, 9, 12, 0, tzinfo=main.UTC)
    started = main.datetime(2026, 8, 10, 13, 25, tzinfo=main.UTC)
    completed = main.datetime(2026, 8, 10, 13, 41, tzinfo=main.UTC)
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        store.set_protection_scope(
            ["US", "HK"],
                enabled_instruments_by_market={
                    "US": ("AAPL",),
                    "HK": ("0700.HK",),
                },
                market_contract_hashes={
                    market: protection_contract_version(rules, market)
                    for market in ("US", "HK")
                },
                now=activated,
        )

    def market_snapshot(ticker, market, **kwargs):
        snapshot = _snapshot_at(started)
        snapshot.update({"ticker": ticker, "market": market})
        report = evaluate_snapshot_reliability(
            snapshot,
            kwargs["required_fields"],
            kwargs["freshness_policies"],
            kwargs["freshness_context"],
            future_tolerance_seconds=kwargs["future_tolerance_seconds"],
        )
        return gate_snapshot_for_decision(
            snapshot, report, kwargs["required_fields"]
        )

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", market_snapshot)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(
        main.run_stock_scan(
            record_run=False,
            now=started,
            clock=lambda: completed,
        )
    )

    with StateStore(state_path) as store:
        events = store.protection_events(limit=20)
    events_by_scope = {
        scope: [event for event in events if event["scope_key"] == scope]
        for scope in ("global", "market:US", "market:HK")
    }
    assert all(
        len(scope_events) == 2 for scope_events in events_by_scope.values()
    ), events_by_scope
    assert all(
        {event["current_state"] for event in scope_events}
        == {"BLIND", "RECOVERING"}
        for scope_events in events_by_scope.values()
    )
    assert set(outcome["protection_event_ids"]) == {
        event["id"] for event in events
    }
    global_current = next(
        event
        for event in events_by_scope["global"]
        if event["current_state"] == "RECOVERING"
    )
    assert outcome["protection_event_id"] == global_current["id"]
    assert all(
        event["delivery_status"] == "suppressed"
        for scope in ("market:US", "market:HK")
        for event in events_by_scope[scope]
    )


def test_late_restart_records_red_edge_then_recovering_and_bad_window(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    activated = main.datetime(2026, 8, 10, 12, 0, tzinfo=main.UTC)
    completed = main.datetime(2026, 8, 10, 16, 0, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={
                "US": protection_contract_version(rules, "US")
            },
            now=activated,
        )
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(
        main, "get_stock", _reliable_fake(lambda: _snapshot_at(completed))
    )
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(record_run=False, now=completed))

    assert outcome["protection"]["state"] == "RECOVERING"
    with StateStore(state_path) as store:
        window = store.protection_windows()[0]
        events = store.protection_events(scope="global")
    assert window["status"] == "bad"
    assert [item["current_state"] for item in reversed(events)] == [
        "BLIND",
        "RECOVERING",
    ]


def test_existing_on_time_good_window_is_not_reversed_by_later_manual_scan(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    activated = main.datetime(2026, 8, 10, 12, 0, tzinfo=main.UTC)
    on_time = main.datetime(2026, 8, 10, 13, 25, tzinfo=main.UTC)
    later = main.datetime(2026, 8, 10, 16, 0, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(["US"], now=activated)
    current = {"at": on_time}
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(lambda: _snapshot_at(current["at"])),
    )
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    first = asyncio.run(main.run_stock_scan(record_run=False, now=on_time))
    current["at"] = later
    second = asyncio.run(main.run_stock_scan(record_run=False, now=later))

    assert first["protection"]["state"] == "HEALTHY"
    assert second["protection"]["state"] == "HEALTHY"
    with StateStore(state_path) as store:
        assert store.protection_windows()[0]["status"] == "good"


def test_scan_completion_after_deadline_records_blind_then_blue(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    activated = main.datetime(2026, 8, 10, 12, 0, tzinfo=main.UTC)
    started = main.datetime(2026, 8, 10, 13, 25, tzinfo=main.UTC)
    completed = main.datetime(2026, 8, 10, 13, 41, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(["US"], now=activated)
    moments = iter((started, started, completed, completed))
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(started)))
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(
        main.run_stock_scan(record_run=False, clock=lambda: next(moments))
    )

    assert outcome["protection"]["state"] == "RECOVERING"
    with StateStore(state_path) as store:
        assert store.protection_windows()[0]["status"] == "bad"
        assert [
            item["current_state"]
            for item in reversed(store.protection_events(scope="global"))
        ] == ["BLIND", "RECOVERING"]


def test_market_window_denominator_includes_enabled_ticker_without_report(
    monkeypatch, tmp_path
) -> None:
    second = _enabled_price_aapl().model_copy(update={"name": "Second probe"})
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl(), "MSFT": second}}
    )
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    delegate = _reliable_fake(lambda: _snapshot_at(at))

    def one_failure(ticker, market, **kwargs):
        if ticker == "MSFT":
            raise RuntimeError("provider body must stay private")
        return delegate(ticker, market, **kwargs)

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", one_failure)
    state_path = tmp_path / "state.db"
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    with StateStore(state_path) as store:
        store.set_protection_scope(
            ["US"],
            now=at - timedelta(hours=1),
            enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
            market_contract_hashes={
                "US": protection_contract_version(rules, "US")
            },
        )

    outcome = asyncio.run(main.run_stock_scan(record_run=False, now=at))

    assert outcome["reliability"]["enabled_instruments"] == 2
    assert outcome["reliability"]["usable_instruments"] == 1
    assert outcome["protection"]["state"] == "DEGRADED"
    with StateStore(state_path) as store:
        window = store.protection_windows()[0]
    assert window["status"] == "pending"
    assert window["enabled_instruments"] == 2
    assert window["usable_instruments"] == 1


def test_slow_fetch_revalidates_age_at_decision_completion(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    observed_at = main.datetime(2026, 8, 10, 13, 30, tzinfo=main.UTC)
    started = observed_at + timedelta(minutes=14)
    decision_at = observed_at + timedelta(minutes=16)
    moments = iter((decision_at, decision_at, decision_at))
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(lambda: _snapshot_at(observed_at)),
    )
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    outcome = asyncio.run(
        main.run_stock_scan(
            record_run=False,
            now=started,
            clock=lambda: next(moments),
        )
    )

    assert outcome["reliability"]["fresh_data_coverage"]["usable_instruments"] == 0
    assert outcome["reliability"]["trusted_decision_coverage"][
        "usable_instruments"
    ] == 0
    assert outcome["protection"]["state"] == "BLIND"
    assert outcome["results"][0]["decision"] == "UNKNOWN"


def test_preopen_quote_is_regated_after_exchange_opens_and_cannot_notify(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    started = main.datetime(2026, 8, 10, 13, 29, 59, tzinfo=main.UTC)
    decision_at = main.datetime(2026, 8, 10, 13, 30, 1, tzinfo=main.UTC)
    prior_close = main.market_freshness_context(
        "US", started
    ).expected_source_after
    assert prior_close is not None
    initial_reports = []

    def snapshot_factory():
        snapshot = _snapshot_at(started)
        snapshot["as_of"] = prior_close.isoformat()
        snapshot["field_metadata"]["price"][
            "source_as_of"
        ] = prior_close.isoformat()
        return snapshot

    delegate = _reliable_fake(snapshot_factory)

    def capture_initial(*args, **kwargs):
        snapshot = delegate(*args, **kwargs)
        initial_reports.append(snapshot["reliability"])
        return snapshot

    sent = 0
    incidents = 0

    async def capture_send(_payload, settings=None):
        nonlocal sent
        del settings
        sent += 1

    async def capture_incident(_payload, settings=None):
        nonlocal incidents
        del settings
        incidents += 1

    moments = iter(
        (
            decision_at,
            decision_at,
            decision_at,
            decision_at,
            decision_at,
            decision_at,
        )
    )
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", capture_initial)
    monkeypatch.setattr(main, "send_signal", capture_send)
    monkeypatch.setattr(main, "send_incident", capture_incident)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    outcome = asyncio.run(
        main.run_stock_scan(
            notify=True,
            record_run=False,
            now=started,
            clock=lambda: next(moments),
        )
    )

    assert initial_reports[0]["usable_for_trusted_silence"] is True
    assert outcome["reliability"]["fresh_data_coverage"]["usable_instruments"] == 0
    assert outcome["results"][0]["decision"] == "UNKNOWN"
    assert outcome["protection"]["state"] == "BLIND"
    assert sent == 0
    assert incidents == 1


def test_evaluation_failure_excludes_fresh_report_from_trusted_coverage(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    at = main.datetime(2026, 8, 10, 13, 25, tzinfo=main.UTC)

    def fail_evaluate(*_args, **_kwargs):
        raise RuntimeError("engine-internal-detail")

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "evaluate", fail_evaluate)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    outcome = asyncio.run(main.run_stock_scan(record_run=False, now=at))

    assert outcome["reliability"]["fresh_data_coverage"]["usable_instruments"] == 1
    assert outcome["reliability"]["trusted_decision_coverage"][
        "usable_instruments"
    ] == 0
    assert outcome["reliability"]["usable_instruments"] == 0
    assert outcome["evaluated"] == 0
    assert outcome["protection"]["state"] == "BLIND"
    assert "evaluation_failed" in outcome["protection"]["reason_codes"]


def test_one_evaluation_failure_makes_two_instrument_silence_degraded(
    monkeypatch, tmp_path
) -> None:
    second = _enabled_price_aapl().model_copy(update={"name": "Second probe"})
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl(), "MSFT": second}}
    )
    at = main.datetime(2026, 8, 10, 13, 25, tzinfo=main.UTC)
    real_evaluate = main.evaluate

    def fail_one(ticker, snapshot, snapshot_rules):
        if ticker == "MSFT":
            raise RuntimeError("engine-internal-detail")
        return real_evaluate(ticker, snapshot, snapshot_rules)

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "evaluate", fail_one)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    outcome = asyncio.run(main.run_stock_scan(record_run=False, now=at))

    assert outcome["reliability"]["fresh_data_coverage"]["usable_instruments"] == 2
    assert outcome["reliability"]["trusted_decision_coverage"][
        "usable_instruments"
    ] == 1
    assert outcome["protection"]["state"] == "DEGRADED"
    assert outcome["evaluated"] == 1


def test_fetch_orchestration_failure_closes_store_without_masking_root_cause(
    monkeypatch, tmp_path
) -> None:
    async def fail(*_args, **_kwargs):
        raise RuntimeError("root-cause-secret")

    state_path = tmp_path / "state.db"
    monkeypatch.setattr(main, "_fetch_snapshots", fail)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    with pytest.raises(RuntimeError, match="root-cause-secret"):
        asyncio.run(main.run_stock_scan(record_run=False))

    with StateStore(state_path) as reopened:
        assert reopened.connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_notification_exception_text_never_leaks_to_outcome_or_run_log(
    monkeypatch, tmp_path
) -> None:
    secret = "https://heartbeat.example/private-token"
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    current = {"at": at, "price": 160.0}

    async def fail(_payload, settings=None):
        del settings
        raise RuntimeError(secret)

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(
            lambda: _snapshot_at(current["at"], price=current["price"])
        ),
    )
    monkeypatch.setattr(main, "send_signal", fail)
    monkeypatch.setattr(main, "send_message", fail)
    state_path = tmp_path / "state.db"
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(notify=True, now=at))

    assert outcome["notification_errors"] == {"AAPL": "runtimeerror"}
    assert outcome["reliability"]["fresh_data_coverage"]["usable_instruments"] == 1
    assert outcome["reliability"]["trusted_decision_coverage"][
        "usable_instruments"
    ] == 1
    assert outcome["protection"]["state"] == "HEALTHY"
    assert secret not in str(outcome)
    with StateStore(state_path) as store:
        assert secret not in str(store.recent_status())
        assert store.delivery_states()["telegram"]["error_code"] == "runtimeerror"

    current.update({"at": at + timedelta(minutes=1), "price": 180.0})
    no_delivery = asyncio.run(
        main.run_stock_scan(notify=True, now=current["at"])
    )
    assert no_delivery["notification_errors"] == {}
    assert no_delivery["notified"] == 0
    with StateStore(state_path) as store:
        assert store.delivery_states()["telegram"]["error_code"] == "runtimeerror"

    async def succeed(_payload, settings=None):
        del settings

    monkeypatch.setattr(main, "send_signal", succeed)
    current.update({"at": at + timedelta(minutes=2), "price": 160.0})
    recovered = asyncio.run(main.run_stock_scan(notify=True, now=current["at"]))
    assert recovered["notified"] == 1
    with StateStore(state_path) as store:
        delivery = store.delivery_states()["telegram"]
        assert delivery["error_code"] is None
        assert delivery["last_success_at"] is not None


def test_slow_signal_delivery_cannot_turn_on_time_data_window_bad(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={
                "US": protection_contract_version(rules, "US")
            },
            now=main.datetime(2026, 8, 10, 12, 0, tzinfo=main.UTC),
        )
    started = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    decision_at = main.datetime(2026, 8, 10, 13, 39, tzinfo=main.UTC)
    workflow_finished = main.datetime(2026, 8, 10, 13, 41, tzinfo=main.UTC)

    async def timeout(_payload, settings=None):
        del settings
        raise TimeoutError("private delivery detail")

    moments = iter((decision_at, decision_at, decision_at, workflow_finished))
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(
        main, "get_stock", _reliable_fake(lambda: _snapshot_at(started))
    )
    monkeypatch.setattr(main, "send_signal", timeout)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(
        main.run_stock_scan(
            notify=True,
            now=started,
            clock=lambda: next(moments),
        )
    )

    assert outcome["notification_errors"] == {"AAPL": "timeout"}
    assert outcome["reliability"]["usable_instruments"] == 1
    assert outcome["protection"]["state"] == "HEALTHY"
    with StateStore(state_path) as store:
        window = store.protection_windows()[0]
        run = store.recent_status(limit=1)[0]
    assert window["status"] == "good"
    assert window["actual_at"] == decision_at.isoformat(timespec="microseconds")
    assert run["finished_at"] == workflow_finished.isoformat(timespec="microseconds")


def test_corrupt_protection_state_seals_silence_red_but_fresh_signal_still_sends(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(
            ["US"], now=main.datetime(2026, 8, 10, 12, 0, tzinfo=main.UTC)
        )
        store.observe_protection(
            main.BlindnessObservation(
                scope="global",
                observation_id="baseline",
                observed_at=main.datetime(
                    2026, 8, 10, 13, 30, tzinfo=main.UTC
                ),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        store.connection.execute(
            "UPDATE protection_state SET snapshot_json = '{broken'"
        )
    sent = 0
    incidents = 0

    async def capture(_payload, settings=None):
        nonlocal sent
        del settings
        sent += 1

    async def capture_incident(_payload, settings=None):
        nonlocal incidents
        del settings
        incidents += 1

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "send_signal", capture)
    monkeypatch.setattr(main, "send_incident", capture_incident)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(notify=True, record_run=False, now=at))

    assert sent == 1
    assert outcome["reliability"]["usable_instruments"] == 1
    assert outcome["protection"]["state"] == "BLIND"
    assert outcome["protection"]["color"] == "RED"
    assert outcome["protection"]["reason_codes"] == ["state_corrupt"]
    assert outcome["protection"]["persisted"] is False
    assert incidents == 1
    with StateStore(state_path) as store:
        assert (
            store.connection.execute(
                "SELECT snapshot_json FROM protection_state"
            ).fetchone()[0]
            == "{broken"
        )


def test_corrupt_scope_is_not_auto_overwritten_and_does_not_kill_signal_plane(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(
            ["US"], now=main.datetime(2026, 8, 10, 12, 0, tzinfo=main.UTC)
        )
        store.connection.execute(
            "UPDATE protection_scope SET market_epochs_json = '{}'"
        )
    sent = 0
    incidents = 0

    async def capture(_payload, settings=None):
        nonlocal sent
        del settings
        sent += 1

    async def capture_incident(_payload, settings=None):
        nonlocal incidents
        del settings
        incidents += 1

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "send_signal", capture)
    monkeypatch.setattr(main, "send_incident", capture_incident)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(notify=True, record_run=False, now=at))

    assert sent == 1
    assert outcome["protection"]["state"] == "BLIND"
    assert outcome["protection"]["reason_codes"] == ["state_corrupt"]
    assert incidents == 1
    with StateStore(state_path) as store:
        active_integrity = store.integrity_incidents(active_only=True)
        assert (
            store.connection.execute(
                "SELECT market_epochs_json FROM protection_scope"
            ).fetchone()[0]
            == "{}"
        )
    assert [(item["scope"], item["component"]) for item in active_integrity] == [
        ("global", "protection_scope")
    ]


def test_corrupt_provider_runtime_is_not_overwritten_and_signal_still_sends(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    secret = "https://provider.example/private-token"
    with StateStore(state_path) as store:
        store.save_provider_runtime_state(
            {"circuits": {secret: {}}, "caches": {}, "observations": {}},
            now=at - timedelta(minutes=1),
        )
        persisted_before = store.connection.execute(
            "SELECT payload_json FROM provider_runtime_state"
        ).fetchone()[0]

    signal_sends = 0
    incident_sends = 0
    incident_actions: list[str] = []

    async def capture_signal(_payload, settings=None):
        nonlocal signal_sends
        del settings
        signal_sends += 1

    async def capture_incident(payload, settings=None):
        nonlocal incident_sends
        del settings
        incident_sends += 1
        incident_actions.append(payload["recommended_action"])

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "send_signal", capture_signal)
    monkeypatch.setattr(main, "send_incident", capture_incident)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(notify=True, record_run=False, now=at))

    assert signal_sends == 1
    assert incident_sends == 1
    assert "--scope provider-runtime --confirm" in incident_actions[0]
    assert outcome["reliability"]["usable_instruments"] == 1
    with StateStore(state_path) as store:
        persisted_after = store.connection.execute(
            "SELECT payload_json FROM provider_runtime_state"
        ).fetchone()[0]
        active = store.integrity_incidents(active_only=True)
    assert persisted_after == persisted_before
    assert [(item["scope"], item["component"]) for item in active] == [
        ("global", "provider_runtime")
    ]


def test_corrupt_run_log_is_durable_red_while_fresh_signal_and_ops_send(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    secret = "https://run.example/private-token"
    with StateStore(state_path) as store:
        bad_id = store.record_run(
            "stock-scan:US",
            "success",
            {"private_payload": secret},
            now=at - timedelta(minutes=1),
        )
        bad_detail = store.connection.execute(
            "SELECT detail FROM run_log WHERE id = ?", (bad_id,)
        ).fetchone()[0]
    signal_sends = 0
    incident_sends = 0
    incident_actions: list[str] = []

    async def capture_signal(_payload, settings=None):
        nonlocal signal_sends
        del settings
        signal_sends += 1

    async def capture_incident(payload, settings=None):
        nonlocal incident_sends
        del settings
        incident_sends += 1
        incident_actions.append(payload["recommended_action"])

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "send_signal", capture_signal)
    monkeypatch.setattr(main, "send_incident", capture_incident)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(notify=True, record_run=True, now=at))

    assert signal_sends == 1
    assert incident_sends == 1
    assert "--scope run-log --confirm" in incident_actions[0]
    assert outcome["protection"]["state"] == "BLIND"
    assert outcome["integrity_ledger_error"] == "state_corrupt"
    assert secret not in str(outcome)
    with StateStore(state_path) as store:
        rows = store.connection.execute(
            "SELECT id, detail FROM run_log ORDER BY id"
        ).fetchall()
        active = store.integrity_incidents(active_only=True)
    assert rows[0]["id"] == bad_id and rows[0]["detail"] == bad_detail
    assert len(rows) == 2
    assert [(item["component"], item["generation"]) for item in active] == [
        ("run_log", 1)
    ]


def test_corrupt_run_log_preview_activation_failure_retries_same_generation(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.record_run(
            "stock-scan:US",
            "success",
            {"private_payload": "https://run.example/private-token"},
            now=at - timedelta(minutes=1),
        )
    secret = "https://incident.example/private-token"
    attempts = 0

    async def fail_once(_payload, settings=None):
        nonlocal attempts
        del settings
        attempts += 1
        if attempts == 1:
            raise RuntimeError(secret)

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(lambda: _snapshot_at(at, price=180.0)),
    )
    monkeypatch.setattr(main, "send_incident", fail_once)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    preview = asyncio.run(main.run_stock_scan(record_run=False, now=at))
    first = asyncio.run(
        main.run_stock_scan(
            notify=True,
            record_run=False,
            now=at + timedelta(minutes=1),
        )
    )
    second = asyncio.run(
        main.run_stock_scan(
            notify=True,
            record_run=False,
            now=at + timedelta(minutes=2),
        )
    )

    assert preview["protection"]["state"] == "BLIND"
    assert first["integrity_notification_errors"] == {"run_log": "runtimeerror"}
    assert secret not in str(first)
    assert second["integrity_incident_notified"] == 1
    assert attempts == 2
    with StateStore(state_path) as store:
        incidents = store.integrity_incidents(scope="global")
    assert len(incidents) == 1
    assert incidents[0]["generation"] == 1
    assert incidents[0]["delivery_status"] == "sent"


def test_scope_integrity_preview_activation_failure_retries_same_generation(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(["US"], now=at - timedelta(hours=1))
        store.connection.execute(
            "UPDATE protection_scope SET market_epochs_json = '{}'"
        )

    secret = "https://watcher.invalid/private-token"
    attempts = 0

    async def fail_once(_payload, settings=None):
        nonlocal attempts
        del settings
        attempts += 1
        if attempts == 1:
            raise RuntimeError(secret)

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(lambda: _snapshot_at(at, price=180.0)),
    )
    monkeypatch.setattr(main, "send_incident", fail_once)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    preview = asyncio.run(main.run_stock_scan(record_run=False, now=at))
    with StateStore(state_path) as store:
        preview_row = store.integrity_incidents(active_only=True)[0]
    first_active = asyncio.run(
        main.run_stock_scan(
            notify=True,
            record_run=False,
            now=at + timedelta(minutes=1),
        )
    )
    second_active = asyncio.run(
        main.run_stock_scan(
            notify=True,
            record_run=False,
            now=at + timedelta(minutes=2),
        )
    )

    assert preview["integrity_incident_attempted"] == 0
    assert preview_row["delivery_status"] == "suppressed"
    assert first_active["integrity_notification_errors"] == {
        "protection_scope": "runtimeerror"
    }
    assert secret not in str(first_active)
    assert second_active["integrity_incident_notified"] == 1
    assert attempts == 2
    with StateStore(state_path) as store:
        rows = store.integrity_incidents(scope="global")
    assert len(rows) == 1
    assert rows[0]["id"] == preview_row["id"]
    assert rows[0]["generation"] == 1
    assert rows[0]["delivery_kind"] == "activation_sync"
    assert rows[0]["delivery_status"] == "sent"


def test_us_hk_scans_share_one_global_scope_integrity_generation(
    monkeypatch, tmp_path
) -> None:
    us = _enabled_price_aapl()
    hk = _enabled_price_aapl().model_copy(
        update={"name": "HK probe", "market": "HK", "currency": "HKD"}
    )
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": us, "0700.HK": hk}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(["HK", "US"], now=at - timedelta(hours=1))
        store.connection.execute(
            "UPDATE protection_scope SET market_epochs_json = '{}'"
        )

    def market_snapshot(ticker, market, **kwargs):
        snapshot = _snapshot_at(at, price=180.0)
        snapshot["ticker"] = ticker
        snapshot["market"] = market
        report = evaluate_snapshot_reliability(
            snapshot,
            kwargs["required_fields"],
            kwargs["freshness_policies"],
            kwargs["freshness_context"],
            future_tolerance_seconds=kwargs["future_tolerance_seconds"],
        )
        return gate_snapshot_for_decision(
            snapshot, report, kwargs["required_fields"]
        )

    incident_sends = 0

    async def capture_incident(_payload, settings=None):
        nonlocal incident_sends
        del settings
        incident_sends += 1

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", market_snapshot)
    monkeypatch.setattr(main, "send_incident", capture_incident)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    asyncio.run(
        main.run_stock_scan(
            market="US", notify=True, record_run=False, now=at
        )
    )
    asyncio.run(
        main.run_stock_scan(
            market="HK",
            notify=True,
            record_run=False,
            now=at + timedelta(minutes=1),
        )
    )
    with StateStore(state_path) as store:
        active = store.integrity_incidents(active_only=True)
        assert [(item["scope"], item["component"]) for item in active] == [
            ("global", "protection_scope")
        ]
        assert active[0]["generation"] == 1
        assert active[0]["delivery_status"] == "sent"
        store.repair_corrupt_protection_scope(
            ["HK", "US"],
            enabled_instruments=2,
            now=at + timedelta(minutes=2),
        )
        assert store.integrity_incidents(active_only=True) == []
        store.connection.execute(
            "UPDATE protection_scope SET market_epochs_json = '{}'"
        )

    asyncio.run(
        main.run_stock_scan(
            market="US",
            record_run=False,
            now=at + timedelta(minutes=3),
        )
    )
    with StateStore(state_path) as store:
        recurrence = store.integrity_incidents(active_only=True)
    assert len(recurrence) == 1
    assert recurrence[0]["generation"] == 2
    assert incident_sends == 1


def test_corrupt_integrity_ledger_cannot_kill_fresh_signal_plane(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(["US"], now=at - timedelta(hours=1))
        store.observe_integrity_incident(
            "global",
            "protection_scope",
            "state_corrupt",
            delivery_status="pending",
            now=at - timedelta(minutes=1),
        )
        store.connection.execute(
            "UPDATE integrity_incidents SET reason_code = ?",
            ("https://watcher.invalid/private-token",),
        )

    signal_sends = 0
    incident_sends = 0

    async def capture_signal(_payload, settings=None):
        nonlocal signal_sends
        del settings
        signal_sends += 1

    async def capture_incident(_payload, settings=None):
        nonlocal incident_sends
        del settings
        incident_sends += 1

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(lambda: _snapshot_at(at, price=160.0)),
    )
    monkeypatch.setattr(main, "send_signal", capture_signal)
    monkeypatch.setattr(main, "send_incident", capture_incident)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(notify=True, record_run=False, now=at))

    assert signal_sends == 1
    assert incident_sends == 0
    assert outcome["protection"]["state"] == "BLIND"
    assert outcome["integrity_ledger_error"] == "state_corrupt"
    assert "watcher.invalid" not in str(outcome)


@pytest.mark.parametrize("corrupt_target", ["scope", "state"])
def test_zero_enabled_integrity_corruption_is_red_not_unconfigured(
    monkeypatch, tmp_path, corrupt_target
) -> None:
    rules = load_rules_config().model_copy(update={"watchlist": {}})
    state_path = tmp_path / f"{corrupt_target}.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope([], now=at - timedelta(hours=1))
        if corrupt_target == "scope":
            store.connection.execute(
                "UPDATE protection_scope SET enabled_markets_json = '{broken'"
            )
        else:
            store.observe_protection(
                main.BlindnessObservation(
                    observation_id="unconfigured-baseline",
                    observed_at=at - timedelta(minutes=1),
                    enabled_instruments=0,
                    usable_instruments=0,
                )
            )
            store.connection.execute(
                "UPDATE protection_state SET snapshot_json = '{broken'"
            )
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(record_run=False, now=at))

    assert outcome["selected"] == 0
    assert outcome["protection"]["state"] == "BLIND"
    assert outcome["protection"]["color"] == "RED"
    assert outcome["protection"]["integrity_overlay"] is True
    assert outcome["protection"]["coverage"]["enabled_instruments"] == 0


def test_active_runtime_creates_one_restart_safe_sync_for_preview_blind(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(
            ["US"], now=main.datetime(2026, 8, 10, 12, 0, tzinfo=main.UTC)
        )
        _transition, preview_event = store.observe_protection(
            main.BlindnessObservation(
                observation_id="preview-blind",
                observed_at=at - timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=0,
                reason_codes=("no_data",),
            ),
            delivery_status="suppressed",
        )
        assert preview_event is not None

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider-private-detail")

    sent = 0
    incidents = 0

    async def capture(_payload, settings=None):
        nonlocal sent
        del settings
        sent += 1

    async def capture_incident(_payload, settings=None):
        nonlocal incidents
        del settings
        incidents += 1

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", fail)
    monkeypatch.setattr(main, "send_signal", capture)
    monkeypatch.setattr(main, "send_incident", capture_incident)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    first = asyncio.run(main.run_stock_scan(notify=True, record_run=False, now=at))
    second = asyncio.run(
        main.run_stock_scan(
            notify=True,
            record_run=False,
            now=at + timedelta(minutes=1),
        )
    )

    assert first["protection_event_id"] == second["protection_event_id"]
    assert sent == 0
    assert incidents == 1
    with StateStore(state_path) as store:
        events = sorted(
            store.protection_events(scope="global"), key=lambda item: item["id"]
        )
    assert [event["event_type"] for event in events] == [
        "blind",
        "activation_sync",
    ]
    assert [event["delivery_status"] for event in events] == [
        "suppressed",
        "sent",
    ]


def test_incident_failure_retries_same_current_edge_across_restart(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(["US"], now=at - timedelta(hours=1))
        store.observe_protection(
            main.BlindnessObservation(
                observation_id="preview-blind",
                observed_at=at - timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=0,
                reason_codes=("no_data",),
            ),
            delivery_status="suppressed",
        )

    def fail_provider(*_args, **_kwargs):
        raise RuntimeError("provider-private-detail")

    secret = "https://heartbeat.invalid/private-token"
    attempts = 0

    async def fail_once(_payload, settings=None):
        nonlocal attempts
        del settings
        attempts += 1
        if attempts == 1:
            raise RuntimeError(secret)

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", fail_provider)
    monkeypatch.setattr(main, "send_incident", fail_once)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    first = asyncio.run(main.run_stock_scan(notify=True, now=at))
    second = asyncio.run(
        main.run_stock_scan(notify=True, now=at + timedelta(minutes=1))
    )

    assert attempts == 2
    assert first["incident_attempted"] is True
    assert first["incident_notified"] is False
    assert first["incident_notification_error"] == "runtimeerror"
    assert second["incident_notified"] is True
    assert second["incident_notification_error"] is None
    assert first["protection_event_id"] == second["protection_event_id"]
    assert secret not in str(first)
    with StateStore(state_path) as store:
        events = store.protection_events(scope="global")
        runs = store.recent_status(limit=2)
        delivery = store.delivery_states()["telegram"]
    activation = next(
        event for event in events if event["event_type"] == "activation_sync"
    )
    assert activation["delivery_status"] == "sent"
    assert runs[0]["detail"]["incident_notified"] is True
    assert runs[1]["detail"]["incident_notification_error"] == "runtimeerror"
    assert secret not in str(runs)
    assert delivery["error_code"] is None


@pytest.mark.parametrize("scan_market", ["US", None])
def test_late_scan_coalesces_red_edge_and_sends_only_current_blue(
    monkeypatch, tmp_path, scan_market
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    started = main.datetime(2026, 8, 10, 13, 25, tzinfo=main.UTC)
    completed = main.datetime(2026, 8, 10, 13, 41, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={
                "US": protection_contract_version(rules, "US")
            },
            now=started - timedelta(hours=1),
        )
        baseline, _event = store.observe_protection(
            main.BlindnessObservation(
                scope="market:US" if scan_market else "global",
                observation_id="baseline-full",
                observed_at=started - timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        assert baseline.snapshot.state.value == "HEALTHY"

    delivered_states: list[str] = []

    async def capture(payload, settings=None):
        del settings
        delivered_states.append(payload["state"])

    moments = iter(
        (started, completed, completed, completed, completed, completed)
    )
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(lambda: _snapshot_at(started, price=180.0)),
    )
    monkeypatch.setattr(main, "send_incident", capture)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(
        main.run_stock_scan(
            market=scan_market,
            notify=True,
            record_run=False,
            now=started,
            clock=lambda: next(moments),
        )
    )

    assert outcome["protection"]["state"] == "RECOVERING"
    assert outcome["incident_notified"] is True
    assert delivered_states == ["RECOVERING"]
    with StateStore(state_path) as store:
        events = store.protection_events()
        window = store.protection_windows()[0]
        watchdog = store.watchdog_incidents()[0]
        pending_market = store.pending_current_incident_event("market:US")
        pending_global = store.pending_current_incident_event("global")
    assert all(event["delivery_status"] == "suppressed" for event in events)
    assert watchdog["delivery_status"] == "sent"
    assert watchdog["state"] == "BLIND"
    assert pending_market is None
    assert pending_global is None
    assert window["status"] == "bad"


@pytest.mark.parametrize("failed_channel", ["signal", "incident"])
def test_signal_and_incident_delivery_are_aggregated_without_washing_failure(
    monkeypatch, tmp_path, failed_channel
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / f"{failed_channel}.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope(["US"], now=at - timedelta(hours=1))
        store.observe_protection(
            main.BlindnessObservation(
                observation_id="preview-blind",
                observed_at=at - timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=0,
                reason_codes=("no_data",),
            ),
            delivery_status="suppressed",
        )

    signal_attempts = 0
    incident_attempts = 0

    async def deliver_signal(_payload, settings=None):
        nonlocal signal_attempts
        del settings
        signal_attempts += 1
        if failed_channel == "signal":
            raise TimeoutError("signal-private-detail")

    async def deliver_incident(_payload, settings=None):
        nonlocal incident_attempts
        del settings
        incident_attempts += 1
        if failed_channel == "incident":
            raise ConnectionError("incident-private-detail")

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(lambda: _snapshot_at(at, price=160.0)),
    )
    monkeypatch.setattr(main, "send_signal", deliver_signal)
    monkeypatch.setattr(main, "send_incident", deliver_incident)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(notify=True, now=at))

    assert signal_attempts == 1
    assert incident_attempts == 1
    assert outcome["protection"]["state"] == "RECOVERING"
    if failed_channel == "signal":
        assert outcome["notification_errors"] == {"AAPL": "timeout"}
        assert outcome["incident_notification_error"] is None
        expected_error = "timeout"
    else:
        assert outcome["notification_errors"] == {}
        assert outcome["incident_notification_error"] == "connection"
        expected_error = "connection"
    with StateStore(state_path) as store:
        delivery = store.delivery_states()["telegram"]
        run = store.recent_status(limit=1)[0]
    assert delivery["error_code"] == expected_error
    assert run["detail"]["incident_notification_error"] == outcome[
        "incident_notification_error"
    ]


def test_incident_render_failure_releases_claim_for_retry(monkeypatch, tmp_path) -> None:
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(tmp_path / "state.db") as store:
        _transition, event_id = store.observe_protection(
            main.BlindnessObservation(
                observation_id="blind",
                observed_at=at,
                enabled_instruments=1,
                usable_instruments=0,
                reason_codes=("no_data",),
            ),
            delivery_status="pending",
        )
        assert event_id is not None

        def fail_render(_payload):
            raise RuntimeError("render-private-detail")

        monkeypatch.setattr(main, "render_incident_alert", fail_render)
        result = asyncio.run(
            main._deliver_current_incident(
                store,
                scope="global",
                settings=_ready_settings(),
                clock=lambda: at,
            )
        )

        assert result == (True, False, "runtimeerror", event_id)
        retry = store.claim_incident_notification(
            event_id,
            now=at + timedelta(seconds=1),
        )
        assert retry is not None


def test_incident_send_completion_after_lease_expiry_remains_retryable(
    monkeypatch, tmp_path
) -> None:
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(tmp_path / "state.db") as store:
        _transition, event_id = store.observe_protection(
            main.BlindnessObservation(
                observation_id="blind",
                observed_at=at,
                enabled_instruments=1,
                usable_instruments=0,
                reason_codes=("no_data",),
            ),
            delivery_status="pending",
        )
        assert event_id is not None

        async def slow_success(_payload, settings=None):
            del settings

        moments = iter((at, at + timedelta(seconds=301)))
        monkeypatch.setattr(main, "send_incident", slow_success)
        result = asyncio.run(
            main._deliver_current_incident(
                store,
                scope="global",
                settings=_ready_settings(),
                clock=lambda: next(moments),
            )
        )

        assert result == (True, False, "valueerror", event_id)
        retry = store.claim_incident_notification(
            event_id,
            now=at + timedelta(seconds=302),
        )
        assert retry is not None


def test_observation_collision_sends_one_current_protection_incident_only(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    unavailable = {"active": False}
    reliable = _reliable_fake(lambda: _snapshot_at(at, price=180.0))

    def provider(*args, **kwargs):
        if unavailable["active"]:
            raise RuntimeError("provider-private-detail")
        return reliable(*args, **kwargs)

    incident_sends = 0

    async def capture_incident(_payload, settings=None):
        nonlocal incident_sends
        del settings
        incident_sends += 1

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", provider)
    monkeypatch.setattr(main, "send_incident", capture_incident)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    baseline = asyncio.run(
        main.run_stock_scan(notify=True, record_run=False, now=at)
    )
    unavailable["active"] = True
    first_collision = asyncio.run(
        main.run_stock_scan(notify=True, record_run=False, now=at)
    )
    replay_collision = asyncio.run(
        main.run_stock_scan(notify=True, record_run=False, now=at)
    )

    assert baseline["protection"]["state"] == "HEALTHY"
    assert first_collision["protection"]["state"] == "BLIND"
    assert replay_collision["protection"]["state"] == "BLIND"
    assert incident_sends == 1
    with StateStore(state_path) as store:
        events = store.protection_events()
        integrity = store.integrity_incidents(active_only=True)
    collision_events = [
        event
        for event in events
        if event["payload"]["reason_codes"] == ["observation_id_collision"]
    ]
    assert len(collision_events) == 1
    assert collision_events[0]["delivery_status"] == "sent"
    assert integrity == []


def test_repair_state_requires_confirmation_and_backs_up_before_scope_repair(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime.now(main.UTC) - timedelta(hours=1)
    with StateStore(state_path) as store:
        store.set_protection_scope(["US"], now=at)
        store.observe_protection(
            main.BlindnessObservation(
                observation_id="healthy",
                observed_at=at,
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        store.connection.execute(
            "UPDATE protection_scope SET enabled_markets_json = '{broken'"
        )
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    refused = runner.invoke(main.app, ["repair-state"])
    assert refused.exit_code == 2
    assert "--confirm" in refused.output
    assert not list(tmp_path.glob("state.db.backup-*.sqlite3"))

    repaired = runner.invoke(main.app, ["repair-state", "--confirm"])
    assert repaired.exit_code == 0, repaired.output
    assert "scope_sha256=" in repaired.output
    backups = list(tmp_path.glob("state.db.backup-*.sqlite3"))
    assert len(backups) == 1 and backups[0].is_file()
    with StateStore(state_path) as store:
        sentinel = store.load_protection_state()
        assert sentinel is not None
        assert sentinel.state.value == "BLIND"
        first, _ = store.observe_protection(
            main.BlindnessObservation(
                observation_id="post-repair-scan-1",
                observed_at=sentinel.updated_at + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        replay, replay_event = store.observe_protection(
            main.BlindnessObservation(
                observation_id="post-repair-scan-1",
                observed_at=sentinel.updated_at + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        second, _ = store.observe_protection(
            main.BlindnessObservation(
                observation_id="post-repair-scan-2",
                observed_at=sentinel.updated_at + timedelta(minutes=2),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
    assert first.snapshot.state.value == "RECOVERING"
    assert replay.snapshot.state.value == "RECOVERING"
    assert replay_event is None
    assert second.snapshot.state.value == "HEALTHY"


@pytest.mark.parametrize("repair_scope", ["provider-runtime", "run-log"])
def test_repair_state_refuses_valid_runtime_ledgers_without_backup(
    monkeypatch,
    tmp_path,
    repair_scope: str,
) -> None:
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        if repair_scope == "provider-runtime":
            store.save_provider_runtime_state(
                {"circuits": {}, "caches": {}, "observations": {}}
            )
        else:
            store.record_run("custom-job", "success", {"safe": True})
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    result = runner.invoke(
        main.app,
        ["repair-state", "--scope", repair_scope, "--confirm"],
    )

    assert result.exit_code == 1
    assert not list(tmp_path.glob("state.db.backup-*.sqlite3"))


@pytest.mark.parametrize("repair_scope", ["provider-runtime", "run-log"])
@pytest.mark.parametrize("storage_kind", ["secret", "blob"])
def test_repair_state_quarantines_runtime_ledgers_without_leaking(
    monkeypatch,
    tmp_path,
    repair_scope: str,
    storage_kind: str,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / f"{repair_scope}-{storage_kind}.db"
    at = main.datetime.now(main.UTC) - timedelta(hours=1)
    secret = "https://repair.example/private-token"
    with StateStore(state_path) as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={
                "US": protection_contract_version(rules, "US")
            },
            now=at,
        )
        store.observe_protection(
            main.BlindnessObservation(
                scope="market:US",
                observation_id="pre-repair-healthy",
                observed_at=at,
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        if repair_scope == "provider-runtime":
            store.save_provider_runtime_state(
                {"circuits": {secret: {}}, "caches": {}, "observations": {}},
                now=at,
            )
            if storage_kind == "blob":
                store.connection.execute(
                    "UPDATE provider_runtime_state SET payload_json = ?",
                    (main.sqlite3.Binary(b"\x00\xff"),),
                )
        else:
            run_id = store.record_run(
                "stock-scan:US",
                "success",
                {"private_payload": secret},
                now=at,
            )
            if storage_kind == "blob":
                store.connection.execute(
                    "UPDATE run_log SET detail = ? WHERE id = ?",
                    (main.sqlite3.Binary(b"\x00\xff"), run_id),
                )
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    result = runner.invoke(
        main.app,
        ["repair-state", "--scope", repair_scope, "--confirm"],
    )

    assert result.exit_code == 0, result.output
    assert secret not in result.output
    assert "quarantine_sha256_1=" in result.output
    assert "state=RECOVERING color=BLUE" in result.output
    backups = list(tmp_path.glob(f"{state_path.name}.backup-*.sqlite3"))
    assert len(backups) == 1 and backups[0].is_file()


@pytest.mark.parametrize("repair_scope", ["global", "market:US"])
def test_repair_scope_passes_exact_current_contract_hashes(
    monkeypatch,
    tmp_path,
    repair_scope: str,
) -> None:
    us = _enabled_price_aapl()
    hk = us.model_copy(
        update={"name": "HK probe", "market": "HK", "currency": "HKD"}
    )
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": us, "0700.HK": hk}}
    )
    state_path = tmp_path / f"{repair_scope.replace(':', '-')}.db"
    at = main.datetime.now(main.UTC) - timedelta(hours=1)
    scope_markets = ["US", "HK"] if repair_scope == "global" else ["US"]
    scope_tickers = (
        {"US": ("AAPL",), "HK": ("0700.HK",)}
        if repair_scope == "global"
        else {"US": ("AAPL",)}
    )
    with StateStore(state_path) as store:
        store.set_protection_scope(
            scope_markets,
            enabled_instruments_by_market=scope_tickers,
            scope=repair_scope,
            now=at,
        )
        store.connection.execute(
            "UPDATE protection_scope SET enabled_markets_json = '{broken' WHERE scope_key = ?",
            (repair_scope,),
        )
    captured: dict[str, object] = {}
    original = StateStore.repair_corrupt_protection_scope

    def capture_repair(store, markets, **kwargs):
        captured["markets"] = markets
        captured.update(kwargs)
        return original(store, markets, **kwargs)

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(
        StateStore, "repair_corrupt_protection_scope", capture_repair
    )

    result = runner.invoke(
        main.app,
        ["repair-state", "--scope", repair_scope, "--confirm"],
    )

    assert result.exit_code == 0, result.output
    expected_markets = ("HK", "US") if repair_scope == "global" else ("US",)
    assert tuple(captured["markets"]) == expected_markets
    assert captured["market_contract_hashes"] == {
        market: protection_contract_version(rules, market)
        for market in expected_markets
    }


def test_explicit_zero_enabled_repair_returns_honest_unconfigured(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(update={"watchlist": {}})
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    with StateStore(state_path) as store:
        store.set_protection_scope([], now=at)
        store.observe_protection(
            main.BlindnessObservation(
                observation_id="unconfigured",
                observed_at=at,
                enabled_instruments=0,
                usable_instruments=0,
            )
        )
        store.connection.execute(
            "UPDATE protection_state SET snapshot_json = '{broken'"
        )
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    result = runner.invoke(main.app, ["repair-state", "--confirm"])

    assert result.exit_code == 0, result.output
    assert "state=UNCONFIGURED color=GRAY" in result.output
    with StateStore(state_path) as store:
        snapshot = store.load_protection_state()
    assert snapshot is not None
    assert snapshot.state.value == "UNCONFIGURED"
    assert snapshot.coverage.enabled_instruments == 0


def test_market_state_repair_uses_only_that_markets_enabled_count(
    monkeypatch, tmp_path
) -> None:
    us = _enabled_price_aapl()
    hk = _enabled_price_aapl().model_copy(
        update={"name": "HK probe", "market": "HK", "currency": "HKD"}
    )
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": us, "0700.HK": hk}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime.now(main.UTC) - timedelta(hours=1)
    with StateStore(state_path) as store:
        store.set_protection_scope(["HK", "US"], now=at)
        store.observe_protection(
            main.BlindnessObservation(
                scope="market:US",
                observation_id="us-healthy",
                observed_at=at,
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        store.connection.execute(
            """
            UPDATE protection_state SET snapshot_json = '{broken'
            WHERE scope_key = 'market:US'
            """
        )
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    result = runner.invoke(
        main.app,
        ["repair-state", "--scope", "market:US", "--confirm"],
    )

    assert result.exit_code == 0, result.output
    with StateStore(state_path) as store:
        sentinel = store.load_protection_state("market:US")
        assert sentinel is not None
        assert sentinel.state.value == "BLIND"
        assert sentinel.coverage.enabled_instruments == 1
        first, _event = store.observe_protection(
            main.BlindnessObservation(
                scope="market:US",
                observation_id="us-full-1",
                observed_at=sentinel.updated_at + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        second, _event = store.observe_protection(
            main.BlindnessObservation(
                scope="market:US",
                observation_id="us-full-2",
                observed_at=sentinel.updated_at + timedelta(minutes=2),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
    assert first.snapshot.state.value == "RECOVERING"
    assert second.snapshot.state.value == "HEALTHY"


def test_repair_state_quarantines_corrupt_event_and_next_active_run_rearms(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime.now(main.UTC) - timedelta(hours=1)
    with StateStore(state_path) as store:
        store.set_protection_scope(["US"], now=at)
        _transition, event_id = store.observe_protection(
            main.BlindnessObservation(
                observation_id="blind",
                observed_at=at,
                enabled_instruments=1,
                usable_instruments=0,
                reason_codes=("no_data",),
            ),
            delivery_status="pending",
        )
        assert event_id is not None
        store.observe_integrity_incident(
            "global",
            "protection_event",
            "state_corrupt",
            delivery_status="pending",
            now=at + timedelta(seconds=1),
        )
        store.connection.execute(
            "UPDATE protection_events SET payload_json = '{broken' WHERE id = ?",
            (event_id,),
        )
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    result = runner.invoke(main.app, ["repair-state", "--confirm"])

    assert result.exit_code == 0, result.output
    assert "event_sha256=" in result.output
    incident_sends = 0

    async def capture_incident(_payload, settings=None):
        nonlocal incident_sends
        del settings
        incident_sends += 1

    def fail_provider(*_args, **_kwargs):
        raise RuntimeError("provider-private-detail")

    with StateStore(state_path) as store:
        current = store.load_protection_state()
        assert current is not None
        assert store.integrity_incidents(active_only=True) == []
        repaired_event = next(
            event for event in store.protection_events() if event["id"] == event_id
        )
        assert repaired_event["delivery_status"] == "suppressed"
        scan_at = current.updated_at + timedelta(minutes=1)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", fail_provider)
    monkeypatch.setattr(main, "send_incident", capture_incident)

    outcome = asyncio.run(
        main.run_stock_scan(notify=True, record_run=False, now=scan_at)
    )

    assert outcome["incident_notified"] is True
    assert incident_sends == 1
    with StateStore(state_path) as store:
        activation = next(
            event
            for event in store.protection_events()
            if event["event_type"] == "activation_sync"
        )
    assert activation["delivery_status"] == "sent"


def test_default_scan_does_not_fetch_disabled_instruments(
    monkeypatch, tmp_path
) -> None:
    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("disabled instruments must not be fetched")

    monkeypatch.setattr(main, "get_stock", network_forbidden)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    result = runner.invoke(main.app, ["scan", "--json"])

    assert result.exit_code == 0, result.output
    assert '"selected": 0' in result.output
    assert '"notified": 0' in result.output


def test_scan_notify_requires_explicit_runtime_opt_in(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    result = runner.invoke(main.app, ["scan", "--notify"])

    assert result.exit_code == 1
    assert result.output.strip() == "失败：input_invalid"
    assert not (tmp_path / "state.db").exists()


def test_news_defaults_do_not_fetch_or_notify(monkeypatch, tmp_path) -> None:
    sent = False

    async def send_forbidden(*_args, **_kwargs):
        nonlocal sent
        sent = True

    monkeypatch.setattr(main, "send_news_alert", send_forbidden)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    result = runner.invoke(main.app, ["news", "--json"])

    assert result.exit_code == 0, result.output
    assert '"fetched": 0' in result.output
    assert '"notified": 0' in result.output
    assert sent is False


def test_status_does_not_create_database_when_absent(monkeypatch, tmp_path) -> None:
    state_path = tmp_path / "missing" / "state.db"
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    result = runner.invoke(main.app, ["status"])

    assert result.exit_code == 0, result.output
    assert "暂无运行记录" in result.output
    assert not state_path.exists()


def test_custom_fixture_must_exist() -> None:
    missing = Path("definitely-not-an-alpha-guard-fixture.json")

    result = runner.invoke(main.app, ["dry-run", "--fixture", str(missing)])

    assert result.exit_code == 2


@pytest.mark.parametrize(
    "unsafe_options",
    [
        {"include_disabled": True},
        {"fixture_path": main.DEFAULT_FIXTURE_PATH},
    ],
)
def test_notification_workflow_rejects_disabled_or_fixture_inputs(
    monkeypatch, tmp_path, unsafe_options
) -> None:
    """The workflow boundary must enforce safety, not only the Typer command."""

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError(
            "unsafe notification input must fail before provider access"
        )

    async def send_forbidden(*_args, **_kwargs):
        raise AssertionError("unsafe notification input must never be sent")

    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", network_forbidden)
    monkeypatch.setattr(main, "send_signal", send_forbidden)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    with pytest.raises(ValueError):
        asyncio.run(
            main.run_stock_scan(
                notify=True,
                record_run=False,
                **unsafe_options,
            )
        )


def test_disabled_instrument_keywords_cannot_generate_news_notifications(
    monkeypatch, tmp_path
) -> None:
    """News matching must use only keywords belonging to enabled instruments."""

    article = {
        "title": "Apple launches a new iPhone",
        "summary": "Apple product event",
        "source": "test-source",
        "url": "https://example.test/apple",
        "datetime": "2026-08-10T00:00:00+00:00",
    }
    sent: list[dict] = []

    async def capture(article, settings=None):
        del settings
        sent.append(article)

    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(
        main, "fetch_all_news", lambda *_args, **_kwargs: [dict(article)]
    )
    monkeypatch.setattr(main, "send_news_alert", capture)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    outcome = asyncio.run(main.run_news_scan(notify=True, record_run=False))

    assert all(not item.enabled for item in load_rules_config().watchlist.values())
    assert outcome["review_items"] == 0
    assert outcome["notified"] == 0
    assert outcome["alerts"] == []
    assert sent == []


def test_news_ai_configuration_is_forwarded_without_losing_enabled_or_limits(
    monkeypatch,
) -> None:
    configured = load_news_config().model_copy(
        update={
            "ai_filter": AIFilterConfig(
                enabled=True,
                alert_threshold=4,
                max_ai_calls_per_scan=7,
            )
        }
    )
    captured: dict = {}

    def capture_filter(*_args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(main, "load_news_config", lambda: configured)
    monkeypatch.setattr(main, "fetch_all_news", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main, "filter_news", capture_filter)

    outcome = asyncio.run(main.run_news_scan(notify=False, record_run=False))

    forwarded = captured["ai_filter"]
    assert forwarded.enabled is True
    assert forwarded.alert_threshold == 4
    assert forwarded.max_ai_calls_per_scan == 7
    assert outcome["status"] == "success"


def test_unknown_preserves_active_direction_and_conflict_sends_only_conflict(
    monkeypatch, tmp_path
) -> None:
    sent_actions: list[str] = []

    async def capture(payload, settings=None):
        del settings
        sent_actions.append(payload["action"])

    monkeypatch.setattr(main, "send_signal", capture)
    instrument = _enabled_aapl(cooldown_hours=0)
    buy_evidence = [_evidence("buy-price", "price_below")]
    buy = _decision_result(
        "BUY_REVIEW",
        buy_status="TRIGGERED",
        buy_evidence=buy_evidence,
    )
    unknown = _decision_result(
        "UNKNOWN",
        sell_status="UNKNOWN",
        buy_status="UNKNOWN",
        sell_evidence=[
            _evidence(
                "sell-price",
                "price_above",
                status="UNKNOWN",
                actual=None,
                operator=">=",
                threshold=220,
            )
        ],
        buy_evidence=[
            _evidence(
                "buy-price",
                "price_below",
                status="UNKNOWN",
                actual=None,
            )
        ],
    )
    conflict = _decision_result(
        "CONFLICT",
        sell_status="TRIGGERED",
        buy_status="TRIGGERED",
        sell_evidence=[
            _evidence(
                "sell-price",
                "price_above",
                operator=">=",
                threshold=150,
            )
        ],
        buy_evidence=buy_evidence,
    )

    with StateStore(tmp_path / "state.db") as store:
        assert asyncio.run(
            main._apply_notification_state(
                store,
                ticker="AAPL",
                instrument=instrument,
                result=buy,
                snapshot=_snapshot(),
                settings=_ready_settings(),
            )
        )
        before_unknown = dict(
            store.connection.execute(
                "SELECT * FROM signal_state WHERE signal_key = 'AAPL:BUY_REVIEW'"
            ).fetchone()
        )

        assert not asyncio.run(
            main._apply_notification_state(
                store,
                ticker="AAPL",
                instrument=instrument,
                result=unknown,
                snapshot=_snapshot(),
                settings=_ready_settings(),
            )
        )
        after_unknown = dict(
            store.connection.execute(
                "SELECT * FROM signal_state WHERE signal_key = 'AAPL:BUY_REVIEW'"
            ).fetchone()
        )
        assert after_unknown == before_unknown

        assert asyncio.run(
            main._apply_notification_state(
                store,
                ticker="AAPL",
                instrument=instrument,
                result=conflict,
                snapshot=_snapshot(),
                settings=_ready_settings(),
            )
        )

    assert sent_actions == ["BUY_REVIEW", "CONFLICT"]


def test_explicit_reset_makes_same_evidence_eligible_again(
    monkeypatch, tmp_path
) -> None:
    sent_actions: list[str] = []

    async def capture(payload, settings=None):
        del settings
        sent_actions.append(payload["action"])

    monkeypatch.setattr(main, "send_signal", capture)
    instrument = _enabled_aapl(cooldown_hours=0)
    buy = _decision_result(
        "BUY_REVIEW",
        buy_status="TRIGGERED",
        buy_evidence=[_evidence("buy-price", "price_below")],
    )
    reset = _decision_result("NONE")

    with StateStore(tmp_path / "state.db") as store:
        assert asyncio.run(
            main._apply_notification_state(
                store,
                ticker="AAPL",
                instrument=instrument,
                result=buy,
                snapshot=_snapshot(),
                settings=_ready_settings(),
            )
        )
        assert not asyncio.run(
            main._apply_notification_state(
                store,
                ticker="AAPL",
                instrument=instrument,
                result=reset,
                snapshot=_snapshot(price=180),
                settings=_ready_settings(),
            )
        )
        assert asyncio.run(
            main._apply_notification_state(
                store,
                ticker="AAPL",
                instrument=instrument,
                result=buy,
                snapshot=_snapshot(),
                settings=_ready_settings(),
            )
        )

    assert sent_actions == ["BUY_REVIEW", "BUY_REVIEW"]


def test_failed_send_retains_evaluation_and_is_retried(monkeypatch, tmp_path) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    attempts = 0

    fake_stock = _reliable_fake(_snapshot)

    async def fail_once(_payload, settings=None):
        del settings
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated transport failure")

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", fake_stock)
    monkeypatch.setattr(main, "send_signal", fail_once)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    replay_now = main.datetime(2026, 8, 10, 7, 30, tzinfo=main.UTC)
    first = asyncio.run(
        main.run_stock_scan(notify=True, record_run=False, now=replay_now)
    )

    assert first["selected"] == 1
    assert first["evaluated"] == 1
    assert first["notified"] == 0
    assert len(first["results"]) == 1
    assert first["results"][0]["decision"] == "BUY_REVIEW"
    assert first["results"][0]["notified"] is False

    second = asyncio.run(
        main.run_stock_scan(notify=True, record_run=False, now=replay_now)
    )

    assert attempts == 2
    assert second["evaluated"] == 1
    assert second["notified"] == 1
    assert second["results"][0]["notified"] is True


def test_real_preview_scan_persists_reset_before_next_notification(
    monkeypatch, tmp_path
) -> None:
    """A supervised preview must still re-arm a later true state edge."""

    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_aapl(cooldown_hours=0)}}
    )
    prices = iter((160.0, 180.0, 160.0))
    sent: list[str] = []

    fake_stock = _reliable_fake(lambda: _snapshot(next(prices)))

    async def capture(payload, settings=None):
        del settings
        sent.append(payload["action"])

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", fake_stock)
    monkeypatch.setattr(main, "send_signal", capture)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    replay_now = main.datetime(2026, 8, 10, 7, 30, tzinfo=main.UTC)
    first = asyncio.run(
        main.run_stock_scan(notify=True, record_run=False, now=replay_now)
    )
    preview_reset = asyncio.run(
        main.run_stock_scan(notify=False, record_run=True, now=replay_now)
    )
    reactivated = asyncio.run(
        main.run_stock_scan(notify=True, record_run=False, now=replay_now)
    )

    assert first["notified"] == 1
    assert preview_reset["results"][0]["decision"] == "NONE"
    assert reactivated["notified"] == 1
    assert sent == ["BUY_REVIEW", "BUY_REVIEW"]


def test_signal_fingerprint_ignores_quote_and_timestamp_changes() -> None:
    original = _evidence("price-upper", "price_above", operator=">=", threshold=220)
    moved_quote = {
        **original,
        "actual_value": 235.0,
        "as_of": "2026-08-10T00:01:00+00:00",
    }

    assert main._fingerprint("SELL_REVIEW", [original]) == main._fingerprint(
        "SELL_REVIEW", [moved_quote]
    )


def test_signal_fingerprint_is_independent_of_evidence_order() -> None:
    price = _evidence("price-upper", "price_above", operator=">=", threshold=220)
    pe = _evidence(
        "pe-upper",
        "pe_above",
        actual=36,
        operator=">=",
        threshold=35,
        unit="ratio",
    )

    assert main._fingerprint("SELL_REVIEW", [price, pe]) == main._fingerprint(
        "SELL_REVIEW", [pe, price]
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"rule_type": "pe_above"},
        {"unit": "ratio"},
    ],
)
def test_signal_fingerprint_includes_rule_type_and_unit(changed) -> None:
    original = _evidence("upper", "price_above", operator=">=", threshold=220)
    semantically_changed = {**original, **changed}

    assert main._fingerprint("SELL_REVIEW", [original]) != main._fingerprint(
        "SELL_REVIEW", [semantically_changed]
    )


def test_instrument_version_changes_when_freshness_eligibility_policy_changes() -> None:
    rules = load_rules_config()
    instrument = _enabled_aapl()
    changed_freshness = rules.reliability.freshness.model_copy(
        update={
            "future_tolerance_seconds": (
                rules.reliability.freshness.future_tolerance_seconds + 1
            )
        }
    )
    changed_rules = rules.model_copy(
        update={
            "reliability": rules.reliability.model_copy(
                update={"freshness": changed_freshness}
            )
        }
    )

    assert main._instrument_rules_version(
        instrument, rules
    ) != main._instrument_rules_version(instrument, changed_rules)


def test_injected_replay_clock_is_used_for_run_log_boundaries(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_aapl()}}
    )
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(_snapshot))
    state_path = tmp_path / "state.db"
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    replay_now = main.datetime(2026, 8, 10, 7, 30, tzinfo=main.UTC)

    asyncio.run(main.run_stock_scan(record_run=True, now=replay_now))

    with StateStore(state_path) as store:
        record = store.recent_status(limit=1)[0]
    expected = replay_now.isoformat(timespec="microseconds")
    assert record["started_at"] == expected
    assert record["finished_at"] == expected


def test_status_json_absent_configured_is_blue_without_creating_database(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_aapl()}}
    )
    state_path = tmp_path / "missing" / "state.db"
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    result = runner.invoke(main.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert (receipt["state"], receipt["overall_color"]) == (
        "RECOVERING",
        "BLUE",
    )
    assert receipt["reason_codes"] == ["protection_not_activated"]
    assert not state_path.exists()
    assert not state_path.parent.exists()


def test_status_json_absent_zero_enabled_is_gray_without_creating_database(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(update={"watchlist": {}})
    state_path = tmp_path / "missing" / "state.db"
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    result = runner.invoke(main.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert (receipt["state"], receipt["overall_color"]) == (
        "UNCONFIGURED",
        "GRAY",
    )
    assert not state_path.exists()


def test_status_uses_safe_cockpit_receipt_limits_runs_and_answers_four_questions(
    monkeypatch, tmp_path
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_aapl()}}
    )
    calls: list[dict] = []

    def cockpit_stub(**kwargs):
        calls.append(kwargs)
        return {
            "generated_at": "2026-08-10T12:00:00+00:00",
            "delivery_mode": "PREVIEW",
            "overall_color": "GREEN",
            "state": "HEALTHY",
            "reason_codes": [],
            "schedule": {
                "markets": [
                    {
                        "market": "US",
                        "expected_at": "2026-08-10T13:25:00+00:00",
                        "deadline_at": "2026-08-10T13:40:00+00:00",
                        "deadline_state": "completed",
                        "slo_30d": {"good": 1, "expected": 1},
                    }
                ],
                "slo_30d": {"target": 0.99, "good": 1, "expected": 1},
            },
            "silence": {
                "state": "HEALTHY",
                "color": "GREEN",
                "enabled": 1,
                "usable": 1,
                "ratio": 1.0,
                "affected": [],
                "fresh_data": {
                    "enabled": 1,
                    "usable": 1,
                    "ratio": 1.0,
                    "affected": [],
                    "known": True,
                },
                "trusted_decision": {
                    "enabled": 1,
                    "usable": 1,
                    "ratio": 1.0,
                    "affected": [],
                    "known": True,
                },
            },
            "providers": {
                "capabilities": [
                    {
                        "provider": "yfinance",
                        "operation": "quote",
                        "market": "US",
                        "sample_count": 20,
                        "wilson_lower_bound": 0.84,
                        "circuit_state": "closed",
                        "reasons": [],
                    }
                ]
            },
            "delivery": {
                "telegram": {
                    "configured": False,
                    "mode": "PREVIEW",
                    "last_attempt_at": None,
                    "last_success_at": None,
                    "success": None,
                    "error_code": None,
                },
                "external_watcher": {
                    "configured": False,
                    "mode": "PREVIEW",
                    "last_attempt_at": None,
                    "last_success_at": None,
                    "success": None,
                    "error_code": None,
                },
            },
            "recent_runs": [
                {
                    "job": "stock-scan:US",
                    "market": "US",
                    "status": "success",
                    "started_at": f"2026-08-{day:02d}T13:25:00+00:00",
                    "finished_at": f"2026-08-{day:02d}T13:26:00+00:00",
                    "selected": 1,
                    "evaluated": 1,
                    "notified": 0,
                }
                for day in range(1, 31)
            ],
        }

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "build_reliability_cockpit", cockpit_stub)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "absent.db")

    machine = runner.invoke(main.app, ["status", "--json", "--limit", "25"])
    human = runner.invoke(main.app, ["status", "--limit", "1"])

    assert machine.exit_code == 0, machine.output
    receipt = json.loads(machine.output)
    assert len(receipt["recent_runs"]) == 25
    assert len(calls) == 2
    assert [call["recent_run_limit"] for call in calls] == [25, 1]
    assert set(receipt) >= {
        "schedule",
        "silence",
        "providers",
        "delivery",
    }
    assert human.exit_code == 0, human.output
    for heading in ("计划保护窗口", "可信沉默", "提供者能力", "交付守望"):
        assert heading in human.output
    assert human.output.count("stock-scan:US") == 1


def test_status_corrupt_evidence_is_red_without_leaking_secret(
    monkeypatch, tmp_path
) -> None:
    secret = "https://heartbeat.example/private-status-token"
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_aapl()}}
    )
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        store.record_run(
            "stock-scan:US",
            "success",
            {"private_payload": secret},
            now=main.datetime.now(main.UTC) - timedelta(minutes=1),
        )
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    result = runner.invoke(main.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
    assert receipt["reason_codes"] == ["state_corrupt"]
    assert secret not in result.output


@pytest.mark.parametrize("json_output", [True, False])
def test_status_binary_database_is_fixed_red_without_exception_or_path_leak(
    monkeypatch,
    tmp_path,
    json_output: bool,
) -> None:
    secret = "https://heartbeat.example/binary-private-token"
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_aapl()}}
    )
    state_path = tmp_path / "state.db"
    state_path.write_bytes(b"not-sqlite\x00" + secret.encode())
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    arguments = ["status", "--json"] if json_output else ["status"]

    result = runner.invoke(main.app, arguments)

    assert result.exit_code == 0, result.output
    if json_output:
        receipt = json.loads(result.output)
        assert (receipt["state"], receipt["overall_color"]) == ("BLIND", "RED")
        assert receipt["reason_codes"] == ["state_corrupt"]
        assert receipt["recent_runs"] == []
    else:
        assert "BLIND / RED" in result.output
    assert secret not in result.output
    assert str(state_path) not in result.output
    assert "Traceback" not in result.output


def test_doctor_absent_database_is_offline_and_does_not_create_parent(
    monkeypatch, tmp_path
) -> None:
    state_path = tmp_path / "missing" / "state.db"
    calls = 0

    def network_forbidden(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("doctor must stay offline")

    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "get_stock", network_forbidden)
    monkeypatch.setattr(main, "send_message", network_forbidden)
    monkeypatch.setattr(main, "send_signal", network_forbidden)
    monkeypatch.setattr(main, "send_incident", network_forbidden)

    result = runner.invoke(main.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert calls == 0
    assert "missing" in result.output
    assert "未执行联网探测" in result.output
    assert not state_path.exists()
    assert not state_path.parent.exists()


def test_doctor_existing_database_is_read_only_and_hides_heartbeat_secret(
    monkeypatch, tmp_path
) -> None:
    state_path = tmp_path / "state.db"
    connection = sqlite3.connect(state_path)
    connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
    connection.execute("INSERT INTO sentinel(value) VALUES ('unchanged')")
    connection.commit()
    connection.close()
    before_bytes = state_path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_stat = state_path.stat()
    secret = "https://watcher.example/private-heartbeat-token"
    settings = Settings(heartbeat_enabled=True, heartbeat_url=secret)
    monkeypatch.setattr(main, "STATE_PATH", state_path)
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    result = runner.invoke(main.app, ["doctor"])

    after_stat = state_path.stat()
    assert result.exit_code == 0, result.output
    assert "SQLite" in result.output and "ok" in result.output
    assert "Heartbeat configured" in result.output
    assert "true" in result.output
    assert "下次美股扫描" in result.output
    assert "下次港股扫描" in result.output
    assert secret not in result.output
    assert hashlib.sha256(state_path.read_bytes()).hexdigest() == before_hash
    assert state_path.read_bytes() == before_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    check = sqlite3.connect(state_path)
    try:
        assert check.execute("SELECT value FROM sentinel").fetchone()[0] == "unchanged"
        assert {
            row[0]
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } == {"sentinel"}
    finally:
        check.close()


def test_doctor_corrupt_database_uses_fixed_code_without_leaking(
    monkeypatch, tmp_path
) -> None:
    secret = b"https://watcher.example/corrupt-private-token"
    state_path = tmp_path / "state.db"
    state_path.write_bytes(b"not-sqlite\x00" + secret)
    before = state_path.read_bytes()
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    result = runner.invoke(main.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "corrupt" in result.output
    assert secret.decode() not in result.output
    assert state_path.read_bytes() == before


def test_active_first_trust_receipt_proves_telegram_once_then_does_not_repeat(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    probes = 0

    async def no_business_attempt(*_args, **_kwargs):
        return False

    async def capture_probe(_message, settings=None):
        nonlocal probes
        del settings
        probes += 1

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "_apply_notification_state", no_business_attempt)
    monkeypatch.setattr(main, "send_message", capture_probe)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    first = asyncio.run(main.run_stock_scan(notify=True, record_run=True, now=at))
    second = asyncio.run(
        main.run_stock_scan(
            notify=True,
            record_run=True,
            now=at + timedelta(minutes=1),
        )
    )

    assert probes == 1, first
    assert first["telegram_probe_attempted"] is True
    assert first["telegram_probe_success"] is True
    assert first["telegram_probe_error"] is None
    assert second["telegram_probe_attempted"] is False
    with StateStore(state_path) as store:
        telegram = store.delivery_states()["telegram"]
        runs = store.run_records()
    assert telegram["success"] is True
    assert telegram["error_code"] is None
    assert runs[-1]["detail"] is not None


def test_disable_then_reenable_same_secret_requires_new_trust_receipt(
    monkeypatch,
    tmp_path,
) -> None:
    state_path = tmp_path / "state.db"
    enabled = _ready_settings()
    disabled = enabled.model_copy(update={"notifications_enabled": False})
    at = main.datetime(2026, 8, 10, 13, 25, tzinfo=main.UTC)
    sends = 0

    async def capture_probe(_message, settings=None):
        nonlocal sends
        assert settings == enabled
        sends += 1

    monkeypatch.setattr(main, "send_message", capture_probe)
    with StateStore(state_path) as store:
        main._record_delivery_state(
            store,
            enabled,
            "telegram",
            mode="active",
            attempted_at=at,
            success=True,
            now=at,
        )
    with StateStore(state_path) as store:
        main._record_delivery_state(
            store,
            disabled,
            "telegram",
            mode="preview",
            now=at + timedelta(minutes=1),
        )
        disabled_state = store.delivery_states()["telegram"]
        assert disabled_state["configured"] is False
        assert disabled_state["last_success_at"] is None
    with StateStore(state_path) as store:
        main._record_delivery_state(
            store,
            enabled,
            "telegram",
            mode="active",
            now=at + timedelta(minutes=2),
        )
        unproven = store.delivery_states()["telegram"]
        assert unproven["last_success_at"] is None
        assert unproven["success"] is None
        attempted, success, error = asyncio.run(
            main._ensure_trust_receipt(
                store,
                enabled,
                now=at + timedelta(minutes=2),
            )
        )
        proven = store.delivery_states()["telegram"]

    assert (attempted, success, error) == (True, True, None)
    assert sends == 1
    assert proven["last_success_at"] is not None
    assert proven["success"] is True


def test_reenable_after_real_trust_send_uses_new_daily_generation(
    monkeypatch,
    tmp_path,
) -> None:
    state_path = tmp_path / "state.db"
    enabled = _ready_settings()
    disabled = enabled.model_copy(update={"notifications_enabled": False})
    first = main.datetime(2026, 8, 10, 13, 25, tzinfo=main.UTC)
    reenabled = first + timedelta(hours=2)
    sends = 0

    async def capture_probe(_message, settings=None):
        nonlocal sends
        assert settings == enabled
        sends += 1

    monkeypatch.setattr(main, "send_message", capture_probe)
    with StateStore(state_path) as store:
        assert asyncio.run(
            main._ensure_trust_receipt(store, enabled, now=first)
        ) == (True, True, None)
        main._record_delivery_state(
            store,
            disabled,
            "telegram",
            mode="preview",
            now=first + timedelta(hours=1),
        )
        attempted, success, error = asyncio.run(
            main._ensure_trust_receipt(store, enabled, now=reenabled)
        )

    assert (attempted, success, error) == (True, True, None)
    assert sends == 2


def test_active_trust_receipt_failure_is_sticky_and_retries_next_run(
    monkeypatch,
    tmp_path,
) -> None:
    secret = "https://telegram.example/private-probe-token"
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    attempts = 0

    async def no_business_attempt(*_args, **_kwargs):
        return False

    async def fail_once(_message, settings=None):
        nonlocal attempts
        del settings
        attempts += 1
        if attempts == 1:
            raise TimeoutError(secret)

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "_apply_notification_state", no_business_attempt)
    monkeypatch.setattr(main, "send_message", fail_once)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    failed = asyncio.run(main.run_stock_scan(notify=True, record_run=True, now=at))
    with StateStore(state_path) as store:
        sticky = store.delivery_states()["telegram"]
    retried = asyncio.run(
        main.run_stock_scan(
            notify=True,
            record_run=True,
            now=at + timedelta(minutes=1),
        )
    )

    assert attempts == 2
    assert failed["telegram_probe_error"] == "timeout"
    assert sticky["error_code"] == "timeout"
    assert retried["telegram_probe_success"] is True
    assert secret not in str((failed, retried))
    with StateStore(state_path) as store:
        assert store.delivery_states()["telegram"]["success"] is True
        assert secret not in str(store.recent_status())


def test_preview_never_sends_trust_receipt(monkeypatch, tmp_path) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    probes = 0

    async def forbidden(*_args, **_kwargs):
        nonlocal probes
        probes += 1
        raise AssertionError("preview must not probe Telegram")

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "send_message", forbidden)
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.db")

    outcome = asyncio.run(main.run_stock_scan(notify=False, now=at))

    assert probes == 0
    assert outcome["telegram_probe_attempted"] is False


def test_probe_success_makes_heartbeat_eligible_while_watcher_is_unproven(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    settings = _ready_heartbeat_settings()
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    state_path = tmp_path / "state.db"

    async def no_business_attempt(*_args, **_kwargs):
        return False

    async def successful_probe(_message, settings=None):
        del settings

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_stock", _reliable_fake(lambda: _snapshot_at(at)))
    monkeypatch.setattr(main, "_apply_notification_state", no_business_attempt)
    monkeypatch.setattr(main, "send_message", successful_probe)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    asyncio.run(main.run_stock_scan(notify=True, record_run=True, now=at))

    with StateStore(state_path) as store:
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={"AAPL": "US"},
            market_contract_hashes={
                "US": protection_contract_version(rules, "US")
            },
            current_delivery_fingerprints=delivery_config_fingerprints(settings),
            generated_at=at,
        )
    assert "watcher_unproven" in receipt["reason_codes"]
    assert heartbeat_eligible(receipt) is True


def test_signal_failure_does_not_send_probe_or_wash_sticky_error(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    at = main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    probes = 0

    async def fail_signal(_payload, settings=None):
        del settings
        raise TimeoutError("private-signal-token")

    async def capture_probe(*_args, **_kwargs):
        nonlocal probes
        probes += 1

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(lambda: _snapshot_at(at, price=160.0)),
    )
    monkeypatch.setattr(main, "send_signal", fail_signal)
    monkeypatch.setattr(main, "send_message", capture_probe)
    state_path = tmp_path / "state.db"
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    outcome = asyncio.run(main.run_stock_scan(notify=True, now=at))

    assert probes == 0
    assert outcome["telegram_probe_attempted"] is False
    with StateStore(state_path) as store:
        assert store.delivery_states()["telegram"]["error_code"] == "timeout"


def test_news_delivery_failure_is_sticky_and_no_attempt_preserves_it(
    monkeypatch,
    tmp_path,
) -> None:
    settings = _ready_settings()
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    article = {
        "title": "Apple review item",
        "summary": "bounded summary",
        "source": "test-source",
        "url": "https://example.test/apple",
        "datetime": "2026-08-10T00:00:00+00:00",
    }
    state_path = tmp_path / "state.db"

    async def fail_news(_article, settings=None):
        del settings
        raise TimeoutError("https://telegram.example/private-news-token")

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "fetch_all_news", lambda *_args, **_kwargs: [article])
    monkeypatch.setattr(main, "filter_news", lambda *_args, **_kwargs: [article])
    monkeypatch.setattr(main, "send_news_alert", fail_news)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    failed = asyncio.run(main.run_news_scan(notify=True, record_run=False))
    assert failed["notification_errors"] == ["timeout"]
    with StateStore(state_path) as store:
        sticky = store.delivery_states()["telegram"]
    assert sticky["mode"] == "active"
    assert sticky["success"] is False
    assert sticky["error_code"] == "timeout"

    monkeypatch.setattr(main, "filter_news", lambda *_args, **_kwargs: [])
    no_attempt = asyncio.run(main.run_news_scan(notify=True, record_run=False))
    assert no_attempt["notification_errors"] == []
    with StateStore(state_path) as store:
        preserved = store.delivery_states()["telegram"]
    assert preserved["success"] is False
    assert preserved["error_code"] == "timeout"

    async def succeed_news(_article, settings=None):
        del settings

    monkeypatch.setattr(main, "filter_news", lambda *_args, **_kwargs: [article])
    monkeypatch.setattr(main, "send_news_alert", succeed_news)
    recovered = asyncio.run(main.run_news_scan(notify=True, record_run=False))
    assert recovered["notified"] == 1
    with StateStore(state_path) as store:
        ready = store.delivery_states()["telegram"]
    assert ready["success"] is True
    assert ready["error_code"] is None
    assert "private-news-token" not in str(
        (failed, no_attempt, recovered, preserved, ready)
    )


def test_daily_summary_restores_active_delivery_and_records_real_result(
    monkeypatch,
    tmp_path,
) -> None:
    settings = _ready_settings()
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    attempts = 0

    async def preview_scan(*_args, **_kwargs):
        now = main.datetime.now(main.UTC)
        with StateStore(state_path) as store:
            main._record_delivery_state(
                store,
                settings,
                "telegram",
                mode="preview",
                now=now,
            )
        return {
            "results": [
                {"ticker": "AAPL", "decision": "NONE", "price": 160.0}
            ]
        }

    async def fail_then_succeed(_message, settings=None):
        nonlocal attempts
        del settings
        attempts += 1
        if attempts == 1:
            raise TimeoutError("https://telegram.example/private-summary-token")

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "run_stock_scan", preview_scan)
    monkeypatch.setattr(main, "send_message", fail_then_succeed)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    with pytest.raises(TimeoutError):
        asyncio.run(main._send_daily_summary("US", notify=True))
    with StateStore(state_path) as store:
        failed = store.delivery_states()["telegram"]
    assert failed["mode"] == "active"
    assert failed["success"] is False
    assert failed["error_code"] == "timeout"

    asyncio.run(main._send_daily_summary("US", notify=True))
    with StateStore(state_path) as store:
        recovered = store.delivery_states()["telegram"]
    assert recovered["mode"] == "active"
    assert recovered["success"] is True
    assert recovered["error_code"] is None
    assert "private-summary-token" not in str((failed, recovered))


def test_scheduler_active_startup_proves_delivery_once_without_market_fetch(
    monkeypatch,
    tmp_path,
) -> None:
    settings = _ready_settings()
    state_path = tmp_path / "state.db"
    probes = 0

    async def capture_probe(_message, settings=None):
        nonlocal probes
        del settings
        probes += 1

    def fetch_forbidden(*_args, **_kwargs):
        raise AssertionError("scheduler delivery startup must not fetch market data")

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "send_message", capture_probe)
    monkeypatch.setattr(main, "get_stock", fetch_forbidden)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    asyncio.run(main._initialise_scheduler_delivery(notify=True))
    asyncio.run(main._initialise_scheduler_delivery(notify=True))

    assert probes == 1
    with StateStore(state_path) as store:
        telegram = store.delivery_states()["telegram"]
    assert telegram["mode"] == "active"
    assert telegram["success"] is True
    assert telegram["error_code"] is None


def test_scheduler_service_runs_watchdog_before_start(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class StopScheduler(Exception):
        pass

    class FakeScheduler:
        def start(self) -> None:
            calls.append("start")

        def shutdown(self, *, wait: bool) -> None:
            assert wait is False
            calls.append("shutdown")

    class FakeEvent:
        async def wait(self) -> None:
            calls.append("wait")
            raise StopScheduler

    async def watchdog(*, notify: bool) -> None:
        assert notify is True
        calls.append("watchdog")

    def build(*_args, **_kwargs):
        calls.append("build")
        return FakeScheduler()

    monkeypatch.setattr(main, "_run_trust_watchdog", watchdog)
    monkeypatch.setattr(main, "build_scheduler", build)
    monkeypatch.setattr(main.asyncio, "Event", FakeEvent)

    with pytest.raises(StopScheduler):
        asyncio.run(main._serve_scheduler(notify=True))

    assert calls == ["watchdog", "build", "start", "wait", "shutdown"]


def test_scheduler_callbacks_share_one_hydrated_provider_runtime(
    monkeypatch,
) -> None:
    calls: list[tuple[str, object]] = []
    shared_runtime = object()
    callbacks: dict[str, object] = {}

    class StopScheduler(Exception):
        pass

    class FakeScheduler:
        def start(self) -> None:
            pass

        def shutdown(self, *, wait: bool) -> None:
            assert wait is False

    class FakeEvent:
        async def wait(self) -> None:
            raise StopScheduler

    async def watchdog(*, notify: bool) -> None:
        assert notify is True

    async def scan(*, market=None, notify=False, provider_runtime=None):
        calls.append((f"scan:{market}:{notify}", provider_runtime))
        return {"status": "success"}

    async def summary(market, *, notify, provider_runtime=None):
        calls.append((f"summary:{market}:{notify}", provider_runtime))

    def build(scan_market, _scan_news, daily_summary, **kwargs):
        callbacks.update(
            scan=scan_market,
            summary=daily_summary,
            watchdog=kwargs["trust_watchdog"],
        )
        return FakeScheduler()

    monkeypatch.setattr(main, "_shared_provider_runtime", lambda _rules: shared_runtime)
    monkeypatch.setattr(main, "_run_trust_watchdog", watchdog)
    monkeypatch.setattr(main, "run_stock_scan", scan)
    monkeypatch.setattr(main, "_send_daily_summary", summary)
    monkeypatch.setattr(main, "build_scheduler", build)
    monkeypatch.setattr(main.asyncio, "Event", FakeEvent)

    with pytest.raises(StopScheduler):
        asyncio.run(main._serve_scheduler(notify=True))
    asyncio.run(callbacks["scan"]("US"))
    asyncio.run(callbacks["summary"]("HK"))

    assert calls == [
        ("scan:US:True", shared_runtime),
        ("summary:HK:True", shared_runtime),
    ]


def test_watchdog_orders_incident_before_trust_and_heartbeat(
    monkeypatch,
) -> None:
    calls: list[str] = []
    now = main.datetime(2026, 8, 12, 3, 0, tzinfo=main.UTC)

    class FakeStore:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(main, "StateStore", lambda _path: FakeStore())
    monkeypatch.setattr(
        main,
        "_finalize_due_protection_windows",
        lambda *_args, **_kwargs: calls.append("finalize")
        or {"finalized": ["US:2026-08-11"], "event_ids": [1]},
    )

    async def deliver_watchdog(*_args, **_kwargs):
        calls.append("incident")
        return True, True, None, 1

    async def deliver_integrity(*_args, **_kwargs):
        calls.append("integrity")
        return 0, 0, {}, []

    async def trust(*_args, **_kwargs):
        calls.append("trust")
        return True, True, None

    monkeypatch.setattr(main, "_deliver_watchdog_incident", deliver_watchdog)
    monkeypatch.setattr(
        main, "_deliver_pending_integrity_incidents", deliver_integrity
    )
    monkeypatch.setattr(main, "_ensure_trust_receipt", trust)
    monkeypatch.setattr(
        main, "_record_mobile_delivery_modes", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        main, "_record_delivery_state", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        main,
        "build_reliability_cockpit",
        lambda **_kwargs: calls.append("cockpit")
        or {
            "reason_codes": [],
            "watchdog": {"active": True},
        },
    )
    monkeypatch.setattr(
        main,
        "heartbeat_eligible",
        lambda _receipt: calls.append("eligible") or False,
    )

    result = asyncio.run(
        main._run_trust_watchdog(notify=True, clock=lambda: now)
    )

    assert result["incident_attempted"] is True
    assert result["heartbeat_attempted"] is False
    assert calls == ["finalize", "incident", "integrity", "cockpit", "eligible"]
    assert "trust" not in calls


@pytest.mark.parametrize("command", [["validate"], ["doctor"]])
def test_cli_configuration_error_never_echoes_heartbeat_secret(
    monkeypatch,
    command,
) -> None:
    secret = "https://user:private-token@watcher.example/ping"
    with pytest.raises(ValidationError) as caught:
        Settings(heartbeat_enabled=True, heartbeat_url=secret)

    def invalid_settings():
        raise caught.value

    monkeypatch.setattr(main, "get_settings", invalid_settings)
    result = runner.invoke(main.app, command)

    assert result.exit_code == 1
    assert "configuration_invalid" in result.output
    assert "private-token" not in result.output
    assert secret not in result.output


@pytest.mark.parametrize("verbose", [False, True])
def test_logging_handlers_allow_only_application_records(
    caplog,
    capsys,
    verbose,
) -> None:
    secret = "private-third-party-debug-token-real"
    third_party = main._SECRET_TRANSPORT_LOGGERS
    main._configure_logging(verbose)
    root_logger = logging.getLogger()
    root_logger.addHandler(caplog.handler)
    try:
        for logger_name in third_party:
            third_party_logger = logging.getLogger(logger_name)
            for level in (
                logging.DEBUG,
                logging.INFO,
                logging.WARNING,
                logging.ERROR,
            ):
                third_party_logger.log(level, "GET /ping/%s", secret)
        logging.getLogger("main").info("application-info-marker")
        logging.getLogger("main").debug("application-debug-marker")
        captured = capsys.readouterr()
    finally:
        root_logger.removeHandler(caplog.handler)
        main._configure_logging(False)

    combined = captured.out + captured.err + caplog.text
    assert secret not in combined
    assert "/ping/" not in combined
    assert "application-info-marker" in combined
    assert ("application-debug-marker" in combined) is verbose
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def test_production_handler_filters_unknown_third_party_errors(
    capsys,
) -> None:
    secret = "private-unknown-library-error-token"
    main._configure_logging(True)
    try:
        logging.getLogger("untrusted_dependency").error("request=%s", secret)
        logging.getLogger("main").info("application-info-marker")
        captured = capsys.readouterr()
    finally:
        main._configure_logging(False)

    combined = captured.out + captured.err
    assert secret not in combined
    assert "application-info-marker" in combined


@pytest.mark.parametrize("status_code", [204, 503])
def test_real_heartbeat_http_transport_never_logs_secret_url(
    monkeypatch,
    caplog,
    capsys,
    status_code,
) -> None:
    secret = "private-heartbeat-http-token-real"
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            self.send_response(status_code)
            self.end_headers()

        def log_message(self, _format, *_args) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        heartbeat_enabled=True,
        heartbeat_url=(
            f"http://127.0.0.1:{server.server_port}/ping/{secret}"
        ),
        heartbeat_timeout_seconds=2.0,
    )
    main._configure_logging(True)
    root_logger = logging.getLogger()
    root_logger.addHandler(caplog.handler)
    try:
        logging.getLogger("main").debug("application-debug-marker")
        result = ping_heartbeat(settings)
    finally:
        root_logger.removeHandler(caplog.handler)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        captured = capsys.readouterr()
        main._configure_logging(False)

    assert result.success is (status_code == 204)
    combined = captured.out + captured.err + caplog.text
    assert "application-debug-marker" in combined
    assert secret not in combined
    assert "/ping/" not in combined
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def test_real_heartbeat_connection_failure_never_logs_secret_url(
    monkeypatch,
    caplog,
    capsys,
) -> None:
    secret = "private-heartbeat-connection-token-real"
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    class DropConnectionHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def log_message(self, _format, *_args) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), DropConnectionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        heartbeat_enabled=True,
        heartbeat_url=(
            f"http://127.0.0.1:{server.server_port}/ping/{secret}"
        ),
        heartbeat_timeout_seconds=1.0,
    )
    main._configure_logging(True)
    root_logger = logging.getLogger()
    root_logger.addHandler(caplog.handler)
    try:
        logging.getLogger("main").debug("application-debug-marker")
        result = ping_heartbeat(settings)
    finally:
        root_logger.removeHandler(caplog.handler)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        captured = capsys.readouterr()
        main._configure_logging(False)

    assert result.error_code == "connection"
    combined = captured.out + captured.err + caplog.text
    assert "application-debug-marker" in combined
    assert secret not in combined
    assert "/ping/" not in combined
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


@pytest.mark.parametrize(
    "exception_factory",
    [
        lambda secret: ValueError(secret),
        lambda secret: TypeError(secret),
        lambda secret: OSError(secret),
        lambda secret: json.JSONDecodeError(secret, secret, 0),
        lambda secret: RuntimeError(secret),
    ],
)
@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
def test_status_failure_boundary_never_leaks_exception_payload(
    monkeypatch,
    capsys,
    exception_factory,
    verbose,
    json_output,
) -> None:
    secret = "https://watcher.example/private-value-token"
    absolute_path = "/private/absolute/state/path"
    error = exception_factory(f"{secret} {absolute_path}")

    def fail_rules():
        raise error

    monkeypatch.setattr(main, "load_rules_config", fail_rules)
    main._configure_logging(verbose)
    try:
        arguments = ["status"] + (["--json"] if json_output else [])
        result = runner.invoke(main.app, arguments)
        captured = capsys.readouterr()
    finally:
        main._configure_logging(False)

    combined = result.output + captured.out + captured.err
    assert result.exit_code == 1
    assert secret not in combined
    assert "private-value-token" not in combined
    assert absolute_path not in combined
    assert "Traceback" not in combined
    assert any(
        code in combined
        for code in ("input_invalid", "local_io_error", "json_invalid", "internal_error")
    )
    if json_output:
        payload = json.loads(result.output)
        assert set(payload) == {"status", "error_code"}
        assert payload["status"] == "error"
        assert payload["error_code"] in {
            "input_invalid",
            "local_io_error",
            "json_invalid",
            "internal_error",
        }


def _seed_watchdog_responsibility(
    store: StateStore,
    rules,
    *,
    activated_at,
    healthy_at,
) -> None:
    store.set_protection_scope(
        ["US"],
        now=activated_at,
        enabled_instruments_by_market={"US": ("AAPL",)},
        market_contract_hashes={
            "US": protection_contract_version(rules, "US")
        },
    )
    store.observe_protection(
        main.BlindnessObservation(
            scope="market:US",
            observation_id="scan:market:US:watchdog-baseline",
            observed_at=healthy_at,
            enabled_instruments=1,
            usable_instruments=1,
            full_coverage_scan=True,
        )
    )


def _open_test_watchdog_incident(
    store: StateStore,
    rules,
    *,
    delivery_status: str = "pending",
):
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    missed_at = expected.deadline_at.astimezone(main.UTC) + timedelta(
        microseconds=1
    )
    _seed_watchdog_responsibility(
        store,
        rules,
        activated_at=expected.expected_at.astimezone(main.UTC)
        - timedelta(hours=1),
        healthy_at=expected.expected_at.astimezone(main.UTC),
    )
    main._finalize_due_protection_windows(
        store,
        rules,
        now=missed_at,
        delivery_status=delivery_status,
    )
    return expected, missed_at, store.watchdog_incidents(active_only=True)[0]


def test_watchdog_delivery_marks_pending_blind_incident_sent(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    captured: list[dict] = []

    async def capture(payload, settings=None):
        del settings
        captured.append(payload)

    monkeypatch.setattr(main, "send_incident", capture)
    with StateStore(tmp_path / "state.db") as store:
        _expected, missed_at, opened = _open_test_watchdog_incident(store, rules)
        result = asyncio.run(
            main._deliver_watchdog_incident(
                store,
                settings=_ready_settings(),
                clock=lambda: missed_at + timedelta(minutes=1),
            )
        )
        persisted = store.watchdog_incidents()[0]

    assert result == (True, True, None, opened["id"])
    assert len(captured) == 1
    assert captured[0]["state"] == "BLIND"
    assert persisted["delivery_status"] == "sent"
    assert persisted["notified_at"] is not None


def test_watchdog_delivery_failure_releases_and_retries_same_incident(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    attempts = 0

    async def fail_once(_payload, settings=None):
        nonlocal attempts
        del settings
        attempts += 1
        if attempts == 1:
            raise TimeoutError("private-watchdog-delivery-token")

    monkeypatch.setattr(main, "send_incident", fail_once)
    with StateStore(tmp_path / "state.db") as store:
        _expected, missed_at, opened = _open_test_watchdog_incident(store, rules)
        first = asyncio.run(
            main._deliver_watchdog_incident(
                store,
                settings=_ready_settings(),
                clock=lambda: missed_at + timedelta(minutes=1),
            )
        )
        assert store.pending_watchdog_incident() is not None
        assert store.connection.execute(
            "SELECT 1 FROM notification_claims WHERE claim_key = ?",
            (f"watchdog:{opened['id']}",),
        ).fetchone() is None
        second = asyncio.run(
            main._deliver_watchdog_incident(
                store,
                settings=_ready_settings(),
                clock=lambda: missed_at + timedelta(minutes=2),
            )
        )

    assert first == (True, False, "timeout", opened["id"])
    assert second == (True, True, None, opened["id"])
    assert attempts == 2


def test_watchdog_delivery_uses_current_recovering_market_snapshot(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    captured: list[dict] = []

    async def capture(payload, settings=None):
        del settings
        captured.append(payload)

    monkeypatch.setattr(main, "send_incident", capture)
    with StateStore(tmp_path / "state.db") as store:
        _expected, missed_at, opened = _open_test_watchdog_incident(store, rules)
        transition, _event_id = store.observe_protection(
            main.BlindnessObservation(
                scope="market:US",
                observation_id="scan:market:US:first-full-after-deadline",
                observed_at=missed_at + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            ),
            delivery_status="suppressed",
        )
        assert transition.snapshot.state is ProtectionState.RECOVERING
        result = asyncio.run(
            main._deliver_watchdog_incident(
                store,
                settings=_ready_settings(),
                clock=lambda: missed_at + timedelta(minutes=2),
            )
        )
        market_events = store.protection_events(scope="market:US")

    assert result == (True, True, None, opened["id"])
    assert len(captured) == 1
    assert captured[0]["state"] == "RECOVERING"
    assert "recovery_confirmation_1_of_2" in captured[0]["reason_codes"]
    assert all(item["delivery_status"] == "suppressed" for item in market_events)


@pytest.mark.parametrize(
    "tamper",
    ["future_state", "future_scope", "corrupt_scope"],
)
def test_watchdog_delivery_claim_boundary_releases_on_corrupt_evidence(
    monkeypatch,
    tmp_path,
    tamper,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    sends = 0

    async def forbidden(*_args, **_kwargs):
        nonlocal sends
        sends += 1

    monkeypatch.setattr(main, "send_incident", forbidden)
    with StateStore(tmp_path / "state.db") as store:
        _expected, missed_at, opened = _open_test_watchdog_incident(store, rules)
        claim_at = missed_at + timedelta(minutes=1)
        original_claim = store.claim_watchdog_incident_notification

        def claim_then_tamper(incident_id, **kwargs):
            token = original_claim(incident_id, **kwargs)
            if tamper == "future_state":
                snapshot = store.load_protection_state("market:US")
                assert snapshot is not None
                future = claim_at + timedelta(minutes=1)
                corrupted = snapshot.model_copy(update={"updated_at": future})
                store.connection.execute(
                    """
                    UPDATE protection_state
                    SET snapshot_json = ?, updated_at = ?
                    WHERE scope_key = 'market:US'
                    """,
                    (corrupted.model_dump_json(), future.isoformat()),
                )
            elif tamper == "future_scope":
                store.connection.execute(
                    "UPDATE protection_scope SET updated_at = ? WHERE scope_key = 'global'",
                    ((claim_at + timedelta(minutes=1)).isoformat(),),
                )
            else:
                store.connection.execute(
                    """
                    UPDATE protection_scope
                    SET market_epochs_json = ?
                    WHERE scope_key = 'global'
                    """,
                    (sqlite3.Binary(b"private-scope-token"),),
                )
            return token

        monkeypatch.setattr(
            store,
            "claim_watchdog_incident_notification",
            claim_then_tamper,
        )
        result = asyncio.run(
            main._deliver_watchdog_incident(
                store,
                settings=_ready_settings(),
                clock=lambda: claim_at,
            )
        )
        claim_row = store.connection.execute(
            "SELECT 1 FROM notification_claims WHERE claim_key = ?",
            (f"watchdog:{opened['id']}",),
        ).fetchone()

    assert result == (False, False, "state_corrupt", opened["id"])
    assert sends == 0
    assert claim_row is None


@pytest.mark.parametrize("race", ["resolve", "supersede", "expand"])
def test_watchdog_delivery_live_claim_freezes_concurrent_mutation(
    monkeypatch,
    tmp_path,
    race,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    sent_payloads: list[dict] = []

    async def capture(payload, settings=None):
        del settings
        sent_payloads.append(payload)

    monkeypatch.setattr(main, "send_incident", capture)
    with StateStore(tmp_path / "state.db") as store:
        _expected, missed_at, opened = _open_test_watchdog_incident(store, rules)
        claim_at = missed_at + timedelta(minutes=1)
        original_claim = store.claim_watchdog_incident_notification

        def claim_then_change(incident_id, **kwargs):
            token = original_claim(incident_id, **kwargs)
            assert token is not None
            if race == "resolve":
                store.resolve_watchdog_incident(
                    delivery_status="pending",
                    now=claim_at,
                )
            elif race == "supersede":
                store.observe_watchdog_incident(
                    scope_generation="b" * 64,
                    enabled_instruments=1,
                    affected_tickers=("AAPL",),
                    markets=("US",),
                    window_keys=(opened["payload"]["window_keys"][0],),
                    first_seen_at=main.datetime.fromisoformat(
                        opened["payload"]["first_seen_at"]
                    ),
                    delivery_status="pending",
                    now=claim_at,
                )
            else:
                store.observe_watchdog_incident(
                    scope_generation=opened["payload"]["scope_generation"],
                    enabled_instruments=1,
                    affected_tickers=("AAPL",),
                    markets=("US",),
                    window_keys=(
                        *opened["payload"]["window_keys"],
                        "US:2026-08-11",
                    ),
                    first_seen_at=main.datetime.fromisoformat(
                        opened["payload"]["first_seen_at"]
                    ),
                    delivery_status="pending",
                    now=claim_at,
                )
            return token

        monkeypatch.setattr(
            store,
            "claim_watchdog_incident_notification",
            claim_then_change,
        )
        result = asyncio.run(
            main._deliver_watchdog_incident(
                store,
                settings=_ready_settings(),
                clock=lambda: claim_at,
            )
        )
        sends_after_claim_race = len(sent_payloads)
        persisted = store.watchdog_incidents()[0]
        replay = asyncio.run(
            main._deliver_watchdog_incident(
                store,
                settings=_ready_settings(),
                clock=lambda: claim_at + timedelta(seconds=1),
            )
        )

    assert result == (True, True, None, opened["id"])
    assert sends_after_claim_race == 1
    assert persisted["id"] == opened["id"]
    assert persisted["generation"] == opened["generation"]
    assert persisted["state"] == "BLIND"
    assert persisted["delivery_status"] == "sent"
    assert replay == (False, False, None, None)


def test_watchdog_delivery_exact_claim_survives_scope_change_before_validation(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    sent_payloads: list[dict] = []

    async def capture(payload, settings=None):
        del settings
        sent_payloads.append(payload)

    monkeypatch.setattr(main, "send_incident", capture)
    with StateStore(tmp_path / "state.db") as store:
        _expected, missed_at, opened = _open_test_watchdog_incident(store, rules)
        claim_at = missed_at + timedelta(minutes=1)
        transition_at = claim_at + timedelta(microseconds=1)
        validation_at = claim_at + timedelta(microseconds=2)
        sent_at = claim_at + timedelta(microseconds=3)
        original_claim = store.claim_watchdog_incident_notification

        def claim_then_change_scope(incident_id, **kwargs):
            token = original_claim(incident_id, **kwargs)
            assert token is not None
            store.set_protection_scope(
                ["US"],
                enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
                now=transition_at,
            )
            return token

        delivery_clock = iter((claim_at, validation_at, sent_at))
        monkeypatch.setattr(
            store,
            "claim_watchdog_incident_notification",
            claim_then_change_scope,
        )
        result = asyncio.run(
            main._deliver_watchdog_incident(
                store,
                settings=_ready_settings(),
                clock=lambda: next(delivery_clock),
            )
        )
        persisted = store.watchdog_incidents()[0]
        replay = asyncio.run(
            main._deliver_watchdog_incident(
                store,
                settings=_ready_settings(),
                clock=lambda: sent_at + timedelta(seconds=1),
            )
        )

    assert result == (True, True, None, opened["id"])
    assert len(sent_payloads) == 1
    assert persisted["state"] == "RECOVERED"
    assert persisted["active"] is False
    assert persisted["delivery_status"] == "suppressed"
    assert persisted["detected_notified_at"] == sent_at.isoformat()
    assert replay == (False, False, None, None)


def test_watchdog_send_linearizes_before_concurrent_recovery(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    state_path = tmp_path / "state.db"
    send_started = asyncio.Event()
    allow_send = asyncio.Event()
    states_sent: list[str] = []

    async def blocking_send(payload, settings=None):
        del settings
        states_sent.append(payload["state"])
        send_started.set()
        await allow_send.wait()

    monkeypatch.setattr(main, "send_incident", blocking_send)

    async def scenario():
        with StateStore(state_path) as sender_store:
            _expected, missed_at, opened = _open_test_watchdog_incident(
                sender_store,
                rules,
            )
            task = asyncio.create_task(
                main._deliver_watchdog_incident(
                    sender_store,
                    settings=_ready_settings(),
                    clock=lambda: missed_at + timedelta(minutes=1),
                )
            )
            await send_started.wait()
            with StateStore(state_path) as scanner_store:
                scope = scanner_store.get_protection_scope("global")
                assert scope is not None
                for index in (1, 2):
                    scanner_store.observe_protection(
                        main.BlindnessObservation(
                            scope="market:US",
                            observation_id=f"scan:market:US:concurrent-{index}",
                            observed_at=missed_at + timedelta(minutes=1 + index),
                            enabled_instruments=1,
                            usable_instruments=1,
                            full_coverage_scan=True,
                        ),
                        delivery_status="suppressed",
                    )
                deferred = main._reconcile_watchdog_incident(
                    scanner_store,
                    rules,
                    scope,
                    now=missed_at + timedelta(minutes=3),
                    delivery_status="pending",
                )
                assert deferred is not None
                assert deferred["id"] == opened["id"]
                assert deferred["state"] == "BLIND"
            allow_send.set()
            first_result = await task

        with StateStore(state_path) as next_tick:
            scope = next_tick.get_protection_scope("global")
            assert scope is not None
            recovered = main._reconcile_watchdog_incident(
                next_tick,
                rules,
                scope,
                now=missed_at + timedelta(minutes=4),
                delivery_status="pending",
            )
            assert recovered is not None and recovered["state"] == "RECOVERED"
            second_result = await main._deliver_watchdog_incident(
                next_tick,
                settings=_ready_settings(),
                clock=lambda: missed_at + timedelta(minutes=5),
            )
            replay = await main._deliver_watchdog_incident(
                next_tick,
                settings=_ready_settings(),
                clock=lambda: missed_at + timedelta(minutes=6),
            )
        return first_result, second_result, replay

    first_result, second_result, replay = asyncio.run(scenario())
    assert first_result[:3] == (True, True, None)
    assert second_result[:3] == (True, True, None)
    assert replay == (False, False, None, None)
    assert states_sent == ["BLIND", "RECOVERED"]


def test_watchdog_corruption_after_send_started_counts_real_attempt(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    sends = 0
    with StateStore(tmp_path / "state.db") as store:
        _expected, missed_at, opened = _open_test_watchdog_incident(store, rules)

        async def corrupt_after_send(_payload, settings=None):
            nonlocal sends
            del settings
            sends += 1
            store.connection.execute(
                "UPDATE watchdog_incidents SET payload_json = ? WHERE id = ?",
                (sqlite3.Binary(b"private-after-send-token"), opened["id"]),
            )

        monkeypatch.setattr(main, "send_incident", corrupt_after_send)
        result = asyncio.run(
            main._deliver_watchdog_incident(
                store,
                settings=_ready_settings(),
                clock=lambda: missed_at + timedelta(minutes=1),
            )
        )
        claim = store.connection.execute(
            "SELECT 1 FROM notification_claims WHERE claim_key = ?",
            (f"watchdog:{opened['id']}",),
        ).fetchone()

    assert result == (True, False, "state_corrupt", opened["id"])
    assert sends == 1
    assert claim is None


def test_watchdog_exact_deadline_does_not_finalize_missing_window(tmp_path) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    with StateStore(tmp_path / "state.db") as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=expected.expected_at.astimezone(main.UTC)
            - timedelta(hours=1),
            healthy_at=expected.expected_at.astimezone(main.UTC),
        )
        before_events = len(store.protection_events(scope="market:US"))

        result = main._finalize_due_protection_windows(
            store,
            rules,
            now=expected.deadline_at.astimezone(main.UTC),
            delivery_status="suppressed",
        )

        assert result == {"finalized": [], "event_ids": []}
        assert store.protection_windows() == []
        assert len(store.protection_events(scope="market:US")) == before_events


def test_watchdog_finalizes_missing_window_one_microsecond_after_deadline(
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    now = expected.deadline_at.astimezone(main.UTC) + timedelta(microseconds=1)
    with StateStore(tmp_path / "state.db") as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=expected.expected_at.astimezone(main.UTC)
            - timedelta(hours=1),
            healthy_at=expected.expected_at.astimezone(main.UTC),
        )
        before_events = len(store.protection_events(scope="market:US"))

        result = main._finalize_due_protection_windows(
            store,
            rules,
            now=now,
            delivery_status="pending",
        )

        window = store.protection_windows()[0]
        snapshot = store.load_protection_state("market:US")
        assert result["finalized"] == [expected.key]
        assert len(result["event_ids"]) == 1
        assert window["status"] == "bad"
        assert snapshot is not None and snapshot.state is ProtectionState.BLIND
        assert len(store.protection_events(scope="market:US")) == before_events + 1
        watchdog_event = store.protection_events(scope="market:US")[-1]
        assert watchdog_event["delivery_status"] == "suppressed"
        watchdog_incident = store.watchdog_incidents(active_only=True)[0]
        assert watchdog_incident["state"] == "BLIND"
        assert watchdog_incident["delivery_status"] == "pending"
        assert watchdog_incident["payload"]["markets"] == ["US"]
        assert watchdog_incident["payload"]["affected_tickers"] == ["AAPL"]


def test_watchdog_finalizes_pending_window_and_freezes_its_evidence(tmp_path) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    pending_at = expected.expected_at.astimezone(main.UTC) + timedelta(minutes=5)
    now = expected.deadline_at.astimezone(main.UTC) + timedelta(microseconds=1)
    with StateStore(tmp_path / "state.db") as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=expected.expected_at.astimezone(main.UTC)
            - timedelta(hours=1),
            healthy_at=expected.expected_at.astimezone(main.UTC),
        )
        store.record_protection_window(
            expected.key,
            "US",
            expected.expected_at,
            expected.deadline_at,
            "pending",
            actual_at=pending_at,
            enabled_instruments=1,
            usable_instruments=0,
            affected_tickers=("AAPL",),
            reason_codes=("partial_coverage",),
            now=pending_at,
        )

        main._finalize_due_protection_windows(
            store,
            rules,
            now=now,
            delivery_status="suppressed",
        )

        window = store.protection_windows()[0]
        assert window["status"] == "bad"
        assert window["actual_at"] == pending_at.isoformat(timespec="microseconds")
        assert window["enabled_instruments"] == 1
        assert window["usable_instruments"] == 0
        assert window["affected"] == ["AAPL"]
        assert window["reasons"] == [
            "partial_coverage",
            "expected_window_missing",
        ]


def test_watchdog_restart_replay_keeps_terminal_evidence_byte_identical(
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    first_now = expected.deadline_at.astimezone(main.UTC) + timedelta(microseconds=1)
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=expected.expected_at.astimezone(main.UTC)
            - timedelta(hours=1),
            healthy_at=expected.expected_at.astimezone(main.UTC),
        )
        main._finalize_due_protection_windows(
            store,
            rules,
            now=first_now,
            delivery_status="pending",
        )
        before_window = dict(
            store.connection.execute(
                "SELECT * FROM protection_windows WHERE window_key = ?",
                (expected.key,),
            ).fetchone()
        )
        before_state = dict(
            store.connection.execute(
                "SELECT * FROM protection_state WHERE scope_key = 'market:US'"
            ).fetchone()
        )
        before_events = [
            dict(row)
            for row in store.connection.execute(
                "SELECT * FROM protection_events ORDER BY id"
            ).fetchall()
        ]

    with StateStore(state_path) as store:
        replay = main._finalize_due_protection_windows(
            store,
            rules,
            now=first_now + timedelta(minutes=5),
            delivery_status="pending",
        )
        after_window = dict(
            store.connection.execute(
                "SELECT * FROM protection_windows WHERE window_key = ?",
                (expected.key,),
            ).fetchone()
        )
        after_state = dict(
            store.connection.execute(
                "SELECT * FROM protection_state WHERE scope_key = 'market:US'"
            ).fetchone()
        )
        after_events = [
            dict(row)
            for row in store.connection.execute(
                "SELECT * FROM protection_events ORDER BY id"
            ).fetchall()
        ]

    assert replay == {"finalized": [], "event_ids": []}
    assert after_window == before_window
    assert after_state == before_state
    assert after_events == before_events


def test_watchdog_never_reinterprets_terminal_bad_from_late_scan(tmp_path) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    completed_at = expected.deadline_at.astimezone(main.UTC) + timedelta(minutes=1)
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=expected.expected_at.astimezone(main.UTC)
            - timedelta(hours=1),
            healthy_at=expected.expected_at.astimezone(main.UTC),
        )
        store.observe_protection(
            main.BlindnessObservation(
                scope="market:US",
                observation_id=f"deadline:market:US:{expected.key}",
                observed_at=completed_at,
                enabled_instruments=1,
                usable_instruments=1,
                reason_codes=("expected_window_missing",),
                deadline_missed=True,
            ),
            delivery_status="pending",
        )
        store.record_protection_window(
            expected.key,
            "US",
            expected.expected_at,
            expected.deadline_at,
            "bad",
            actual_at=completed_at,
            enabled_instruments=1,
            usable_instruments=1,
            reason_codes=("expected_window_missing",),
            now=completed_at,
        )
        before_window = dict(
            store.connection.execute(
                "SELECT * FROM protection_windows WHERE window_key = ?",
                (expected.key,),
            ).fetchone()
        )
        before_state = dict(
            store.connection.execute(
                "SELECT * FROM protection_state WHERE scope_key = 'market:US'"
            ).fetchone()
        )
        before_events = [
            dict(row)
            for row in store.connection.execute(
                "SELECT * FROM protection_events ORDER BY id"
            ).fetchall()
        ]

    for replay_at in (
        completed_at + timedelta(minutes=1),
        completed_at + timedelta(minutes=6),
    ):
        with StateStore(state_path) as store:
            replay = main._finalize_due_protection_windows(
                store,
                rules,
                now=replay_at,
                delivery_status="pending",
            )
            assert replay == {"finalized": [], "event_ids": []}

    with StateStore(state_path) as store:
        after_window = dict(
            store.connection.execute(
                "SELECT * FROM protection_windows WHERE window_key = ?",
                (expected.key,),
            ).fetchone()
        )
        after_state = dict(
            store.connection.execute(
                "SELECT * FROM protection_state WHERE scope_key = 'market:US'"
            ).fetchone()
        )
        after_events = [
            dict(row)
            for row in store.connection.execute(
                "SELECT * FROM protection_events ORDER BY id"
            ).fetchall()
        ]

    assert after_window == before_window
    assert after_state == before_state
    assert after_events == before_events


def test_watchdog_batch_finalizes_all_historical_due_after_newer_health(
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    now = main.datetime(2026, 8, 10, 13, 40, tzinfo=main.UTC) + timedelta(
        microseconds=1
    )
    activated_at = main.datetime(2026, 8, 6, 12, 0, tzinfo=main.UTC)
    due = main.expected_market_scans_between("US", activated_at, now)
    assert len(due) == 3
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=activated_at,
            healthy_at=main.datetime(2026, 8, 10, 13, 25, tzinfo=main.UTC),
        )
        before_events = len(store.protection_events(scope="market:US"))

        result = main._finalize_due_protection_windows(
            store,
            rules,
            now=now,
            delivery_status="pending",
        )

        assert result["finalized"] == [window.key for window in due]
        assert len(result["event_ids"]) == 1
        assert {row["status"] for row in store.protection_windows()} == {"bad"}
        assert len(store.protection_windows()) == 3
        snapshot = store.load_protection_state("market:US")
        assert snapshot is not None and snapshot.state is ProtectionState.BLIND
        assert len(store.protection_events(scope="market:US")) == before_events + 1

    with StateStore(state_path) as store:
        before = (
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_windows")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_state")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_events")],
            [
                dict(row)
                for row in store.connection.execute(
                    "SELECT * FROM protection_observations"
                )
            ],
        )
        replay = main._finalize_due_protection_windows(
            store,
            rules,
            now=now + timedelta(minutes=5),
            delivery_status="pending",
        )
        after = (
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_windows")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_state")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_events")],
            [
                dict(row)
                for row in store.connection.execute(
                    "SELECT * FROM protection_observations"
                )
            ],
        )
    assert replay == {"finalized": [], "event_ids": []}
    assert after == before


def test_watchdog_batch_failure_rolls_back_state_event_observation_and_windows(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    now = expected.deadline_at.astimezone(main.UTC) + timedelta(microseconds=1)
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=expected.expected_at.astimezone(main.UTC)
            - timedelta(hours=1),
            healthy_at=expected.expected_at.astimezone(main.UTC),
        )
        original = StateStore._watchdog_batch_failure_point

        def crash(_self) -> None:
            raise RuntimeError("injected watchdog transaction failure")

        monkeypatch.setattr(StateStore, "_watchdog_batch_failure_point", crash)
        before = (
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_windows")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_state")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_events")],
            [
                dict(row)
                for row in store.connection.execute(
                    "SELECT * FROM protection_observations"
                )
            ],
        )
        with pytest.raises(RuntimeError, match="injected watchdog"):
            main._finalize_due_protection_windows(
                store,
                rules,
                now=now,
                delivery_status="pending",
            )
        after_failure = (
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_windows")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_state")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_events")],
            [
                dict(row)
                for row in store.connection.execute(
                    "SELECT * FROM protection_observations"
                )
            ],
        )
        assert after_failure == before

        monkeypatch.setattr(StateStore, "_watchdog_batch_failure_point", original)
        recovered = main._finalize_due_protection_windows(
            store,
            rules,
            now=now,
            delivery_status="pending",
        )
        assert recovered["finalized"] == [expected.key]


def test_atomic_watchdog_us_batch_leaves_hk_state_and_events_unchanged(
    tmp_path,
) -> None:
    at = main.datetime(2026, 8, 10, 13, 40, tzinfo=main.UTC) + timedelta(
        microseconds=1
    )
    us_window = main.latest_expected_market_scan("US", at)
    with StateStore(tmp_path / "state.db") as store:
        for market in ("US", "HK"):
            store.observe_protection(
                main.BlindnessObservation(
                    scope=f"market:{market}",
                    observation_id=f"scan:market:{market}:baseline",
                    observed_at=at - timedelta(minutes=20),
                    enabled_instruments=1,
                    usable_instruments=1,
                    full_coverage_scan=True,
                )
            )
        hk_state_before = dict(
            store.connection.execute(
                "SELECT * FROM protection_state WHERE scope_key = 'market:HK'"
            ).fetchone()
        )
        hk_events_before = [
            dict(row) for row in store.protection_events(scope="market:HK")
        ]

        finalized, event_id = store.finalize_overdue_market_windows(
            "US",
            (
                (
                    us_window.key,
                    us_window.expected_at,
                    us_window.deadline_at,
                ),
            ),
            enabled_tickers=("AAPL",),
            now=at,
        )

        hk_state_after = dict(
            store.connection.execute(
                "SELECT * FROM protection_state WHERE scope_key = 'market:HK'"
            ).fetchone()
        )
        hk_events_after = [
            dict(row) for row in store.protection_events(scope="market:HK")
        ]
    assert finalized == (us_window.key,)
    assert event_id is not None
    assert hk_state_after == hk_state_before
    assert hk_events_after == hk_events_before


def test_watchdog_reconciles_global_incident_after_market_commit_crash(
    monkeypatch,
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    now = expected.deadline_at.astimezone(main.UTC) + timedelta(microseconds=1)
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=expected.expected_at.astimezone(main.UTC)
            - timedelta(hours=1),
            healthy_at=expected.expected_at.astimezone(main.UTC),
        )

        def crash(*_args, **_kwargs):
            raise RuntimeError("crash after market commit")

        monkeypatch.setattr(main, "_reconcile_watchdog_incident", crash)
        with pytest.raises(RuntimeError, match="after market commit"):
            main._finalize_due_protection_windows(
                store,
                rules,
                now=now,
                delivery_status="pending",
            )
        assert store.protection_windows()[0]["status"] == "bad"
        assert store.watchdog_incidents() == []

    monkeypatch.undo()
    with StateStore(state_path) as reopened:
        replay = main._finalize_due_protection_windows(
            reopened,
            rules,
            now=now + timedelta(minutes=1),
            delivery_status="pending",
        )
        incident = reopened.watchdog_incidents(active_only=True)[0]
        assert replay == {"finalized": [], "event_ids": []}
        assert incident["generation"] == 1
        assert incident["delivery_status"] == "pending"


def test_watchdog_preview_incident_activates_same_generation_on_active_restart(
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    now = expected.deadline_at.astimezone(main.UTC) + timedelta(microseconds=1)
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=expected.expected_at.astimezone(main.UTC)
            - timedelta(hours=1),
            healthy_at=expected.expected_at.astimezone(main.UTC),
        )
        main._finalize_due_protection_windows(
            store,
            rules,
            now=now,
            delivery_status="suppressed",
        )
        preview = store.watchdog_incidents(active_only=True)[0]
        assert preview["delivery_status"] == "suppressed"

    with StateStore(state_path) as reopened:
        main._finalize_due_protection_windows(
            reopened,
            rules,
            now=now + timedelta(minutes=1),
            delivery_status="pending",
        )
        active = reopened.watchdog_incidents(active_only=True)[0]
        assert active["id"] == preview["id"]
        assert active["generation"] == preview["generation"]
        assert active["delivery_status"] == "pending"


def test_watchdog_recovery_resolves_same_incident_without_rewriting_slo(
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    missed_at = expected.deadline_at.astimezone(main.UTC) + timedelta(
        microseconds=1
    )
    with StateStore(tmp_path / "state.db") as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=expected.expected_at.astimezone(main.UTC)
            - timedelta(hours=1),
            healthy_at=expected.expected_at.astimezone(main.UTC),
        )
        main._finalize_due_protection_windows(
            store,
            rules,
            now=missed_at,
            delivery_status="pending",
        )
        incident = store.watchdog_incidents(active_only=True)[0]
        before_window = dict(
            store.connection.execute(
                "SELECT * FROM protection_windows WHERE window_key = ?",
                (expected.key,),
            ).fetchone()
        )
        first, _ = store.observe_protection(
            main.BlindnessObservation(
                scope="market:US",
                observation_id="scan:market:US:recovery-1",
                observed_at=missed_at + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        second, _ = store.observe_protection(
            main.BlindnessObservation(
                scope="market:US",
                observation_id="scan:market:US:recovery-2",
                observed_at=missed_at + timedelta(minutes=2),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        assert first.snapshot.state is ProtectionState.RECOVERING
        assert second.snapshot.state is ProtectionState.HEALTHY
        main._finalize_due_protection_windows(
            store,
            rules,
            now=missed_at + timedelta(minutes=3),
            delivery_status="pending",
        )
        recovered = store.watchdog_incidents()[0]
        after_window = dict(
            store.connection.execute(
                "SELECT * FROM protection_windows WHERE window_key = ?",
                (expected.key,),
            ).fetchone()
        )
        before_replay = (
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_windows")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_state")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_events")],
            [dict(row) for row in store.connection.execute("SELECT * FROM watchdog_incidents")],
        )
        main._finalize_due_protection_windows(
            store,
            rules,
            now=missed_at + timedelta(minutes=4),
            delivery_status="pending",
        )
        after_replay = (
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_windows")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_state")],
            [dict(row) for row in store.connection.execute("SELECT * FROM protection_events")],
            [dict(row) for row in store.connection.execute("SELECT * FROM watchdog_incidents")],
        )
        next_miss = missed_at + timedelta(days=1)
        main._finalize_due_protection_windows(
            store,
            rules,
            now=next_miss,
            delivery_status="pending",
        )
        next_generation = store.watchdog_incidents(active_only=True)[0]

    assert recovered["id"] == incident["id"]
    assert recovered["generation"] == incident["generation"]
    assert recovered["state"] == "RECOVERED"
    assert recovered["delivery_status"] == "pending"
    assert after_window == before_window
    assert after_replay == before_replay
    assert next_generation["generation"] == 2
    assert next_generation["state"] == "BLIND"


def test_watchdog_partial_market_recovery_keeps_union_until_all_markets_green(
    tmp_path,
) -> None:
    base = load_rules_config()
    rules = base.model_copy(
        update={
            "watchlist": {
                "AAPL": _enabled_price_aapl(),
                "00700": base.watchlist["00700"].model_copy(
                    update={"enabled": True}
                ),
            }
        }
    )
    now = main.datetime(2026, 8, 10, 14, 0, tzinfo=main.UTC)
    windows = {
        market: main.latest_expected_market_scan(market, now)
        for market in ("US", "HK")
    }
    with StateStore(tmp_path / "state.db") as store:
        scope = store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            market_contract_hashes={
                market: protection_contract_version(rules, market)
                for market in ("US", "HK")
            },
            now=min(window.expected_at for window in windows.values())
            - timedelta(hours=1),
        )
        for market in ("US", "HK"):
            store.observe_protection(
                main.BlindnessObservation(
                    scope=f"market:{market}",
                    observation_id=f"scan:market:{market}:baseline",
                    observed_at=windows[market].expected_at,
                    enabled_instruments=1,
                    usable_instruments=1,
                    full_coverage_scan=True,
                )
            )
            store.finalize_overdue_market_windows(
                market,
                (
                    (
                        windows[market].key,
                        windows[market].expected_at,
                        windows[market].deadline_at,
                    ),
                ),
                enabled_tickers=("AAPL" if market == "US" else "00700",),
                now=now,
            )
        incident = main._reconcile_watchdog_incident(
            store,
            rules,
            scope,
            now=now,
            delivery_status="pending",
        )
        assert incident is not None
        claim = store.claim_watchdog_incident_notification(
            incident["id"], now=now + timedelta(seconds=1)
        )
        assert claim is not None
        store.mark_watchdog_incident_notified(
            incident["id"], claim, now=now + timedelta(seconds=2)
        )
        for index in (1, 2):
            store.observe_protection(
                main.BlindnessObservation(
                    scope="market:US",
                    observation_id=f"scan:market:US:recovery-{index}",
                    observed_at=now + timedelta(minutes=index),
                    enabled_instruments=1,
                    usable_instruments=1,
                    full_coverage_scan=True,
                )
            )
        partial = main._reconcile_watchdog_incident(
            store,
            rules,
            scope,
            now=now + timedelta(minutes=3),
            delivery_status="pending",
        )
        assert partial is not None
        assert partial["id"] == incident["id"]
        assert partial["generation"] == 1
        assert partial["active"] is True
        assert partial["delivery_status"] == "sent"
        assert partial["payload"]["markets"] == ["HK", "US"]
        assert store.pending_watchdog_incident() is None

        for index in (1, 2):
            store.observe_protection(
                main.BlindnessObservation(
                    scope="market:HK",
                    observation_id=f"scan:market:HK:recovery-{index}",
                    observed_at=now + timedelta(minutes=3 + index),
                    enabled_instruments=1,
                    usable_instruments=1,
                    full_coverage_scan=True,
                )
            )
        resolved = main._reconcile_watchdog_incident(
            store,
            rules,
            scope,
            now=now + timedelta(minutes=6),
            delivery_status="pending",
        )

    assert resolved is not None
    assert resolved["id"] == incident["id"]
    assert resolved["state"] == "RECOVERED"
    assert resolved["delivery_status"] == "pending"


def test_watchdog_reconcile_rejects_future_window_evidence_before_view(
    tmp_path,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    now = main.datetime(2026, 8, 10, 13, 20, tzinfo=main.UTC)
    expected_at = now + timedelta(minutes=10)
    deadline_at = expected_at + timedelta(minutes=10)
    with StateStore(tmp_path / "state.db") as store:
        scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={
                "US": protection_contract_version(rules, "US")
            },
            now=now - timedelta(hours=1),
        )
        store.record_protection_window(
            "US:2026-08-10",
            "US",
            expected_at,
            deadline_at,
            "pending",
            enabled_instruments=1,
            usable_instruments=0,
            affected_tickers=("AAPL",),
            reason_codes=("expected_window_missing",),
            now=now,
        )

        with pytest.raises(main.CorruptProtectionStateError):
            main._reconcile_watchdog_incident(
                store,
                rules,
                scope,
                now=now,
                delivery_status="suppressed",
            )


@pytest.mark.parametrize(
    "tamper",
    ["activated_at", "updated_at", "market_epoch", "blob_epoch"],
)
def test_watchdog_reconcile_rejects_future_or_malformed_scope_before_writes(
    tmp_path,
    tamper,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    now = main.datetime(2026, 8, 10, 13, 20, tzinfo=main.UTC)
    with StateStore(tmp_path / "state.db") as store:
        scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={
                "US": protection_contract_version(rules, "US")
            },
            now=now - timedelta(hours=1),
        )
        store.observe_protection(
            main.BlindnessObservation(
                scope="market:US",
                observation_id="scan:market:US:scope-future-baseline",
                observed_at=now - timedelta(minutes=30),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        tampered = {**scope, "market_epochs": dict(scope["market_epochs"])}
        future = (now + timedelta(minutes=1)).isoformat()
        if tamper in {"activated_at", "updated_at"}:
            tampered[tamper] = future
        elif tamper == "market_epoch":
            tampered["market_epochs"]["US"] = future
        else:
            tampered["market_epochs"]["US"] = b"private-scope-token"
        tables = (
            "protection_windows",
            "protection_state",
            "protection_events",
            "watchdog_incidents",
            "notification_claims",
        )
        before = {
            table: [
                tuple(row)
                for row in store.connection.execute(f"SELECT * FROM {table}")
            ]
            for table in tables
        }

        with pytest.raises(main.CorruptProtectionStateError):
            main._reconcile_watchdog_incident(
                store,
                rules,
                tampered,
                now=now,
                delivery_status="pending",
            )

        after = {
            table: [
                tuple(row)
                for row in store.connection.execute(f"SELECT * FROM {table}")
            ]
            for table in tables
        }
    assert after == before


@pytest.mark.parametrize("scan_market", ["US", None])
def test_real_market_scans_advance_and_resolve_watchdog_incident(
    monkeypatch,
    tmp_path,
    scan_market,
) -> None:
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    missed_at = expected.deadline_at.astimezone(main.UTC) + timedelta(
        microseconds=1
    )
    first_scan_at = missed_at + timedelta(minutes=1)
    second_scan_at = missed_at + timedelta(minutes=2)
    current = {"at": first_scan_at}
    state_path = tmp_path / "state.db"
    operational_messages: list[dict] = []

    async def no_signal_attempt(*_args, **_kwargs):
        return False

    async def no_integrity_incident(*_args, **_kwargs):
        return False, False, {}, []

    async def no_probe(*_args, **_kwargs):
        return False, None, None

    async def capture_incident(payload, settings=None):
        del settings
        operational_messages.append(payload)

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(lambda: _snapshot_at(current["at"], price=170.0)),
    )
    monkeypatch.setattr(main, "_apply_notification_state", no_signal_attempt)
    monkeypatch.setattr(
        main,
        "_deliver_pending_integrity_incidents",
        no_integrity_incident,
    )
    monkeypatch.setattr(main, "_ensure_trust_receipt", no_probe)
    monkeypatch.setattr(main, "send_incident", capture_incident)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    with StateStore(state_path) as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=expected.expected_at.astimezone(main.UTC)
            - timedelta(hours=1),
            healthy_at=expected.expected_at.astimezone(main.UTC),
        )
        main._finalize_due_protection_windows(
            store,
            rules,
            now=missed_at,
            delivery_status="pending",
        )
        opened = store.watchdog_incidents(active_only=True)[0]
        terminal_before = dict(
            store.connection.execute(
                "SELECT * FROM protection_windows WHERE window_key = ?",
                (expected.key,),
            ).fetchone()
        )

    asyncio.run(
        main.run_stock_scan(
            market=scan_market,
            notify=True,
            record_run=False,
            now=first_scan_at,
        )
    )
    with StateStore(state_path) as store:
        first = store.load_protection_state("market:US")
        still_active = store.watchdog_incidents(active_only=True)[0]
        assert first is not None and first.state is ProtectionState.RECOVERING
        assert still_active["id"] == opened["id"]
        assert still_active["generation"] == opened["generation"]
        assert still_active["delivery_status"] == "sent"
        assert store.pending_current_incident_event("market:US") is None
        assert store.pending_current_incident_event("global") is None
    assert [item["state"] for item in operational_messages] == ["RECOVERING"]

    current["at"] = second_scan_at
    asyncio.run(
        main.run_stock_scan(
            market=scan_market,
            notify=True,
            record_run=False,
            now=second_scan_at,
        )
    )
    with StateStore(state_path) as store:
        second = store.load_protection_state("market:US")
        recovered = store.watchdog_incidents()[0]
        terminal_after = dict(
            store.connection.execute(
                "SELECT * FROM protection_windows WHERE window_key = ?",
                (expected.key,),
            ).fetchone()
        )

    assert second is not None and second.state is ProtectionState.HEALTHY
    assert recovered["id"] == opened["id"]
    assert recovered["generation"] == opened["generation"]
    assert recovered["state"] == "RECOVERED"
    assert recovered["delivery_status"] == "sent"
    assert terminal_after == terminal_before
    assert [item["state"] for item in operational_messages] == [
        "RECOVERING",
        "RECOVERED",
    ]
    with StateStore(state_path) as reopened:
        replay = asyncio.run(
            main._deliver_watchdog_incident(
                reopened,
                settings=_ready_settings(),
                clock=lambda: second_scan_at + timedelta(minutes=1),
            )
        )
    assert replay == (False, False, None, None)
    assert len(operational_messages) == 2


def test_corrupt_watchdog_ledger_fails_silence_closed_but_signal_continues(
    monkeypatch,
    tmp_path,
) -> None:
    secret = "https://watchdog.example/private-ledger-token"
    rules = load_rules_config().model_copy(
        update={"watchlist": {"AAPL": _enabled_price_aapl()}}
    )
    expected = main.latest_expected_market_scan(
        "US", main.datetime(2026, 8, 10, 13, 35, tzinfo=main.UTC)
    )
    missed_at = expected.deadline_at.astimezone(main.UTC) + timedelta(
        microseconds=1
    )
    scan_at = missed_at + timedelta(minutes=1)
    state_path = tmp_path / "state.db"
    signal_sends = 0

    async def capture_signal(_payload, settings=None):
        nonlocal signal_sends
        del settings
        signal_sends += 1

    async def leave_integrity_pending(*_args, **_kwargs):
        return False, False, {}, []

    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", _ready_settings)
    monkeypatch.setattr(
        main,
        "get_stock",
        _reliable_fake(lambda: _snapshot_at(scan_at, price=160.0)),
    )
    monkeypatch.setattr(main, "send_signal", capture_signal)
    monkeypatch.setattr(
        main,
        "_deliver_pending_integrity_incidents",
        leave_integrity_pending,
    )
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    with StateStore(state_path) as store:
        _seed_watchdog_responsibility(
            store,
            rules,
            activated_at=expected.expected_at.astimezone(main.UTC)
            - timedelta(hours=1),
            healthy_at=expected.expected_at.astimezone(main.UTC),
        )
        main._finalize_due_protection_windows(
            store,
            rules,
            now=missed_at,
            delivery_status="pending",
        )
        store.connection.execute(
            "UPDATE watchdog_incidents SET payload_json = ?",
            (secret,),
        )

    outcome = asyncio.run(
        main.run_stock_scan(
            market="US",
            notify=True,
            record_run=False,
            now=scan_at,
        )
    )

    with StateStore(state_path) as store:
        integrity = store.integrity_incidents(active_only=True)
    assert signal_sends == 1
    assert outcome["protection"]["state"] == "BLIND"
    assert outcome["protection"]["reason_codes"] == ["state_corrupt"]
    assert any(item["component"] == "watchdog_incidents" for item in integrity)
    assert secret not in str(outcome)
