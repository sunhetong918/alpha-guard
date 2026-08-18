"""Broker adapters: an offline-safe dry run and the Futu OpenD bridge."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .models import OrderIntent, OrderOutcome

# Futu market codes for the securities this project monitors.
_FUTU_MARKET_CODE = {"HK": "HK", "US": "US"}
_FUTU_SIDE = {"buy": "BUY", "sell": "SELL"}


class Broker(ABC):
    """Minimal order-placement boundary shared by dry-run and live adapters."""

    @abstractmethod
    def place_order(self, intent: OrderIntent) -> OrderOutcome:
        """Submit one resolved intent; adapter must never mutate it."""

    @property
    @abstractmethod
    def mode(self) -> str:
        """``"dry"`` or ``"live"``; drives audit and notification wording."""


class DryRunBroker(Broker):
    """Records intents without touching any network or account."""

    def __init__(self) -> None:
        self.intents: list[OrderIntent] = []

    @property
    def mode(self) -> str:
        return "dry"

    def place_order(self, intent: OrderIntent) -> OrderOutcome:
        self.intents.append(intent)
        return OrderOutcome(
            status="skipped_dry_run",
            broker_order_id=None,
            mode="dry",
            message="Dry-run mode: no order was sent to any broker",
        )


class FutuBroker(Broker):
    """Places real limit orders through the local Futu OpenD gateway.

    The adapter is intentionally thin: risk containment lives in
    :class:`trading.guard.TradingGuard`, never here.
    """

    def __init__(self, *, host: str, port: int) -> None:
        self._host = host
        self._port = port

    @property
    def mode(self) -> str:
        return "live"

    def place_order(self, intent: OrderIntent) -> OrderOutcome:
        import futu as futu_api  # optional extra; fail fast when absent

        market = _FUTU_MARKET_CODE.get(intent.market)
        side = _FUTU_SIDE.get(intent.side)
        if market is None or side is None:
            return OrderOutcome(
                status="rejected",
                broker_order_id=None,
                mode="live",
                message=f"Unsupported market/side: {intent.market}/{intent.side}",
            )
        code = (
            f"{market}.{intent.ticker}"
            if intent.market == "HK"
            else f"{market}.{intent.ticker}"
        )
        context = futu_api.OpenSecuContext(self._host, self._port)
        try:
            ret, data = context.place_order(
                code=code,
                qty=intent.quantity,
                price=float(intent.limit_price),
                trd_side=side,
                order_type=futu_api.OrderType.LIMIT,
            )
        finally:
            context.close()
        if ret != 0:
            return OrderOutcome(
                status="rejected",
                broker_order_id=None,
                mode="live",
                message=f"futu place_order ret={ret}",
            )
        order_id = str((data or {}).get("order_id") or "") or None
        return OrderOutcome(
            status="submitted",
            broker_order_id=order_id,
            mode="live",
            message="Submitted to Futu OpenD",
        )


def build_broker(config: Any, settings: Any) -> Broker:
    """Choose the adapter from the validated trading config."""

    if not getattr(config, "enabled", False):
        return DryRunBroker()
    if config.mode == "live":
        return FutuBroker(
            host=settings.futu_opend_host, port=settings.futu_opend_trade_port
        )
    return DryRunBroker()
