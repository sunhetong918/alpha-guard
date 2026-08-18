"""Optional Futu OpenAPI trading layer, dry-run by default."""

from .broker import Broker, DryRunBroker, FutuBroker
from .executor import TradingExecutor
from .guard import GuardVerdict, TradingGuard
from .models import OrderIntent, OrderOutcome, TradeAuditRecord

__all__ = [
    "Broker",
    "DryRunBroker",
    "FutuBroker",
    "GuardVerdict",
    "OrderIntent",
    "OrderOutcome",
    "TradeAuditRecord",
    "TradingExecutor",
    "TradingGuard",
]
