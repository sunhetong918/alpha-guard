"""Frozen trading models with full decision provenance."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class OrderIntent:
    """A fully-resolved order ready for a broker adapter."""

    ticker: str
    market: str
    side: str  # "buy" | "sell"
    quantity: int
    order_type: str  # "limit"
    limit_price: float
    currency: str
    decision: str  # EvaluationDecision value that produced this intent
    rule_ids: tuple[str, ...] = ()
    created_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "rule_ids": list(self.rule_ids)}


@dataclass(frozen=True)
class OrderOutcome:
    """Broker acknowledgement for one submitted order."""

    status: str  # "submitted" | "rejected" | "skipped_dry_run"
    broker_order_id: str | None
    mode: str  # always "dry" in the read-only product
    message: str = ""
    submitted_at: str = field(default_factory=_utc_now_iso)


@dataclass(frozen=True)
class TradeAuditRecord:
    """Immutable audit trail entry: intent + guard verdict + outcome."""

    intent: OrderIntent
    guard_allowed: bool
    guard_reasons: tuple[str, ...]
    outcome: OrderOutcome | None = None
    snapshot_as_of: str | None = None
    recorded_at: str = field(default_factory=_utc_now_iso)

    @property
    def placed(self) -> bool:
        return self.outcome is not None and self.outcome.status == "submitted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict(),
            "guard_allowed": self.guard_allowed,
            "guard_reasons": list(self.guard_reasons),
            "outcome": asdict(self.outcome) if self.outcome else None,
            "snapshot_as_of": self.snapshot_as_of,
            "recorded_at": self.recorded_at,
        }
