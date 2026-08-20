"""Offline-only order-intent rehearsal.

The production product is a read-only market monitor.  This module keeps the
existing local what-if workflow, but contains no broker SDK import, account
unlock, or network order path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .models import OrderIntent, OrderOutcome


class Broker(ABC):
    """Minimal boundary for local order-intent simulation."""

    @abstractmethod
    def record_intent(self, intent: OrderIntent) -> OrderOutcome:
        """Record one hypothetical intent without any external side effect."""

    @property
    @abstractmethod
    def mode(self) -> str:
        """Always ``"dry"`` in the read-only product."""


class DryRunBroker(Broker):
    """Records intents without touching any network or account."""

    def __init__(self) -> None:
        self.intents: list[OrderIntent] = []

    @property
    def mode(self) -> str:
        return "dry"

    def record_intent(self, intent: OrderIntent) -> OrderOutcome:
        self.intents.append(intent)
        return OrderOutcome(
            status="skipped_dry_run",
            broker_order_id=None,
            mode="dry",
            message="Dry-run mode: no order was sent to any broker",
        )


def build_broker(config: Any, settings: Any) -> Broker:
    """Return the only supported adapter: an offline dry run."""

    del config, settings
    return DryRunBroker()
