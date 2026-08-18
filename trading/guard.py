"""Pre-trade risk gate: deny by default, allow only with full evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from config import AutoTradeInstrumentConfig, FutuTradingConfig

_ALLOWED_DECISIONS = {"BUY_REVIEW", "SELL_REVIEW"}


@dataclass(frozen=True)
class GuardVerdict:
    """Why an intent was allowed or denied; reasons are audit material."""

    allowed: bool
    reasons: tuple[str, ...]


class TradingGuard:
    """Stateful risk gate enforcing the per-instrument trading policy.

    Limits and cooldowns are tracked in memory per process; the audit record
    persisted by the executor remains the durable source of truth.
    """

    def __init__(self, config: FutuTradingConfig) -> None:
        self._config = config
        self._order_counts: dict[tuple[str, object], int] = {}
        self._last_order_at: dict[str, datetime] = {}

    def _orders_on(self, ticker: str, day: object) -> int:
        return self._order_counts.get((ticker, day), 0)

    def evaluate(
        self,
        ticker: str,
        decision: str,
        snapshot: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> GuardVerdict:
        reasons: list[str] = []
        if not self._config.enabled:
            reasons.append("trading_disabled")
            return GuardVerdict(False, tuple(reasons))

        policy: AutoTradeInstrumentConfig | None = self._config.auto_trade.get(ticker)
        if policy is None:
            reasons.append("ticker_not_opted_in")
            return GuardVerdict(False, tuple(reasons))

        if decision not in _ALLOWED_DECISIONS:
            reasons.append(f"decision_not_actionable:{decision}")
            return GuardVerdict(False, tuple(reasons))

        expected = {"BUY_REVIEW": "buy", "SELL_REVIEW": "sell"}[decision]
        if policy.side != expected:
            reasons.append(f"side_mismatch:configured={policy.side}")
            return GuardVerdict(False, tuple(reasons))

        price = snapshot.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            reasons.append("price_missing_or_non_positive")
            return GuardVerdict(False, tuple(reasons))

        moment = now or datetime.now(UTC)
        if self._orders_on(ticker, moment.date()) >= policy.max_orders_per_day:
            reasons.append("daily_order_limit_reached")
        last = self._last_order_at.get(ticker)
        if last is not None and moment - last < timedelta(minutes=policy.cooldown_minutes):
            reasons.append("cooldown_active")
        if reasons:
            return GuardVerdict(False, tuple(reasons))
        return GuardVerdict(True, ("policy_satisfied",))

    def record_placement(self, ticker: str, *, now: datetime | None = None) -> None:
        """Update in-memory limits after a successful submission."""

        moment = now or datetime.now(UTC)
        key = (ticker, moment.date())
        self._order_counts[key] = self._order_counts.get(key, 0) + 1
        self._last_order_at[ticker] = moment
