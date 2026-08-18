"""Trading layer unit tests: config, guard, broker, executor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from config import FutuTradingConfig
from trading import DryRunBroker, TradingExecutor
from trading.broker import build_broker
from trading.guard import TradingGuard
from trading.models import OrderIntent


def _config(**overrides) -> FutuTradingConfig:
    payload = {
        "enabled": True,
        "mode": "dry",
        "auto_trade": {
            "00700": {
                "side": "sell",
                "quantity": 100,
                "limit_offset_pct": 0.5,
                "max_orders_per_day": 1,
                "cooldown_minutes": 60,
            }
        },
    }
    payload.update(overrides)
    return FutuTradingConfig.model_validate(payload)


_SNAPSHOT = {"price": 300.0, "market": "HK", "currency": "HKD", "as_of": "2026-01-01"}


class TestConfig:
    def test_live_requires_confirm(self) -> None:
        with pytest.raises(Exception, match="confirm_live"):
            FutuTradingConfig.model_validate(
                {"enabled": True, "mode": "live", "auto_trade": {}}
            )

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(Exception):
            FutuTradingConfig.model_validate({"enabled": False, "oops": 1})

    def test_default_is_fully_off(self) -> None:
        config = FutuTradingConfig.model_validate({})
        assert not config.enabled
        assert config.mode == "dry"


class TestGuard:
    def test_denies_when_disabled(self) -> None:
        verdict = TradingGuard(FutuTradingConfig.model_validate({})).evaluate(
            "00700", "SELL_REVIEW", _SNAPSHOT
        )
        assert not verdict.allowed
        assert verdict.reasons == ("trading_disabled",)

    def test_denies_unknown_and_conflict(self) -> None:
        guard = TradingGuard(_config())
        for decision in ("UNKNOWN", "CONFLICT", "NONE"):
            verdict = guard.evaluate("00700", decision, _SNAPSHOT)
            assert not verdict.allowed
            assert verdict.reasons[0].startswith("decision_not_actionable")

    def test_denies_ticker_not_opted_in(self) -> None:
        verdict = TradingGuard(_config()).evaluate("AAPL", "SELL_REVIEW", _SNAPSHOT)
        assert not verdict.allowed

    def test_denies_side_mismatch(self) -> None:
        verdict = TradingGuard(_config()).evaluate("00700", "BUY_REVIEW", _SNAPSHOT)
        assert not verdict.allowed
        assert verdict.reasons[0].startswith("side_mismatch")

    def test_denies_bad_price(self) -> None:
        verdict = TradingGuard(_config()).evaluate(
            "00700", "SELL_REVIEW", {"price": None}
        )
        assert not verdict.allowed
        assert "price_missing_or_non_positive" in verdict.reasons

    def test_allows_matching_policy(self) -> None:
        guard = TradingGuard(_config())
        verdict = guard.evaluate("00700", "SELL_REVIEW", _SNAPSHOT)
        assert verdict.allowed

    def test_daily_limit_and_cooldown(self) -> None:
        guard = TradingGuard(_config())
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert guard.evaluate("00700", "SELL_REVIEW", _SNAPSHOT, now=now).allowed
        guard.record_placement("00700", now=now)
        later = now + timedelta(minutes=5)
        verdict = guard.evaluate("00700", "SELL_REVIEW", _SNAPSHOT, now=later)
        assert not verdict.allowed
        assert "cooldown_active" in verdict.reasons
        next_day = now + timedelta(days=1)
        assert guard.evaluate(
            "00700", "SELL_REVIEW", _SNAPSHOT, now=next_day
        ).allowed


class TestBrokers:
    def test_dry_run_never_submits(self) -> None:
        broker = DryRunBroker()
        outcome = broker.place_order(
            OrderIntent(
                ticker="00700",
                market="HK",
                side="sell",
                quantity=100,
                order_type="limit",
                limit_price=299.5,
                currency="HKD",
                decision="SELL_REVIEW",
            )
        )
        assert outcome.status == "skipped_dry_run"
        assert broker.mode == "dry"
        assert len(broker.intents) == 1

    def test_build_broker_respects_mode(self) -> None:
        class _Settings:
            futu_opend_host = "127.0.0.1"
            futu_opend_trade_port = 11111

        assert build_broker(FutuTradingConfig.model_validate({}), _Settings).mode == "dry"
        assert build_broker(_config(), _Settings).mode == "dry"
        live = build_broker(
            FutuTradingConfig.model_validate(
                {
                    "enabled": True,
                    "mode": "live",
                    "confirm_live": True,
                    "auto_trade": {
                        "00700": {"side": "sell", "quantity": 100}
                    },
                }
            ),
            _Settings,
        )
        assert live.mode == "live"


def _evaluation(decision: str) -> dict:
    return {
        "ticker": "00700",
        "decision": decision,
        "rule_results": [
            {"rule_id": "sell-price", "status": "TRIGGERED"},
            {"rule_id": "buy-pe", "status": "NOT_TRIGGERED"},
        ],
    }


class TestExecutor:

    def test_disabled_executor_is_noop(self) -> None:
        executor = TradingExecutor(
            FutuTradingConfig.model_validate({}), DryRunBroker()
        )
        assert executor.execute(_evaluation("SELL_REVIEW"), _SNAPSHOT) is None

    def test_dry_run_produces_audit_record(self) -> None:
        executor = TradingExecutor(_config(), DryRunBroker())
        record = executor.execute(_evaluation("SELL_REVIEW"), _SNAPSHOT)
        assert record is not None
        assert record.guard_allowed
        assert record.outcome is not None
        assert record.outcome.status == "skipped_dry_run"
        # sell limit is conservative (below reference price)
        assert record.intent.limit_price < _SNAPSHOT["price"]
        assert record.intent.rule_ids == ("sell-price",)
        assert not record.placed

    def test_denied_decision_records_reasons(self) -> None:
        executor = TradingExecutor(_config(), DryRunBroker())
        record = executor.execute(_evaluation("CONFLICT"), _SNAPSHOT)
        assert record is not None
        assert not record.guard_allowed
        assert record.guard_reasons[0].startswith("decision_not_actionable")

    def test_audit_record_serializable(self) -> None:
        executor = TradingExecutor(_config(), DryRunBroker())
        record = executor.execute(_evaluation("SELL_REVIEW"), _SNAPSHOT)
        assert record is not None
        payload = record.to_dict()
        assert payload["intent"]["ticker"] == "00700"
        assert payload["outcome"]["status"] == "skipped_dry_run"


class TestRenderTradeAlert:
    def test_render_contains_key_facts(self) -> None:
        from notifier.telegram_bot import render_trade_alert

        executor = TradingExecutor(_config(), DryRunBroker())
        record = executor.execute(_evaluation("SELL_REVIEW"), _SNAPSHOT)
        assert record is not None
        text = render_trade_alert(record.to_dict())
        assert "00700" in text
        assert "skipped_dry_run" in text
        assert "SELL_REVIEW" in text
