"""Decision-to-order executor with an immutable audit trail."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from config import AutoTradeInstrumentConfig, FutuTradingConfig

from .broker import Broker
from .guard import TradingGuard
from .models import OrderIntent, TradeAuditRecord


def _limit_price(
    price: float, side: str, offset_pct: float
) -> float:
    """Conservative limit: pay no more than reference+offset when buying."""

    factor = 1 + offset_pct / 100 if side == "buy" else 1 - offset_pct / 100
    return round(price * factor, 3)


class TradingExecutor:
    """Turns one engine evaluation into at most one guarded order."""

    def __init__(self, config: FutuTradingConfig, broker: Broker) -> None:
        self._config = config
        self._broker = broker
        self._guard = TradingGuard(config)
        self.audit: list[TradeAuditRecord] = []

    def execute(
        self,
        evaluation: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> TradeAuditRecord | None:
        """Evaluate, guard and (maybe) place one order for this ticker."""

        if not self._config.enabled:
            return None
        ticker = str(evaluation.get("ticker") or "")
        policy: AutoTradeInstrumentConfig | None = self._config.auto_trade.get(
            ticker
        )
        if policy is None:
            return None
        decision = str(evaluation.get("decision") or "NONE")

        verdict = self._guard.evaluate(ticker, decision, snapshot)
        if not verdict.allowed:
            record = TradeAuditRecord(
                intent=self._intent(ticker, snapshot, evaluation, policy),
                guard_allowed=False,
                guard_reasons=verdict.reasons,
                snapshot_as_of=snapshot.get("as_of"),
            )
            self.audit.append(record)
            return record

        intent = self._intent(ticker, snapshot, evaluation, policy)
        outcome = self._broker.record_intent(intent)
        record = TradeAuditRecord(
            intent=intent,
            guard_allowed=True,
            guard_reasons=verdict.reasons,
            outcome=outcome,
            snapshot_as_of=snapshot.get("as_of"),
        )
        self.audit.append(record)
        return record

    def _intent(
        self,
        ticker: str,
        snapshot: Mapping[str, Any],
        evaluation: Mapping[str, Any],
        policy: AutoTradeInstrumentConfig,
    ) -> OrderIntent:
        raw_price = snapshot.get("price")
        assert raw_price is not None  # guard verified positivity before us
        price = float(raw_price)
        rule_ids = tuple(
            str(item.get("rule_id")) for item in _triggered_rules(evaluation)
        )
        return OrderIntent(
            ticker=ticker,
            market=str(snapshot.get("market") or ""),
            side=policy.side,
            quantity=policy.quantity,
            order_type=policy.order_type,
            limit_price=_limit_price(price, policy.side, policy.limit_offset_pct),
            currency=str(snapshot.get("currency") or ""),
            decision=str(evaluation.get("decision") or "NONE"),
            rule_ids=rule_ids,
        )


def _triggered_rules(evaluation: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Pull triggered rule ids from an engine evaluation result."""

    evidence = evaluation.get("rule_results")
    if not isinstance(evidence, list):
        return []
    return [
        item
        for item in evidence
        if isinstance(item, Mapping) and item.get("status") == "TRIGGERED"
    ]
