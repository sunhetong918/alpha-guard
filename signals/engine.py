"""Three-state, evidence-producing rule evaluation engine."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from reliability import fields_for_rule_type

_yaml: Any
try:  # Rule checks remain importable when only the optional YAML loader is absent.
    import yaml as _yaml
except ModuleNotFoundError:  # pragma: no cover - exercised by load_rules guard
    _yaml = None

yaml = _yaml


RULES_PATH = Path(__file__).parent / "rules.yaml"


class RuleStatus(str, Enum):
    """The only valid outcomes for one deterministic rule."""

    TRIGGERED = "TRIGGERED"
    NOT_TRIGGERED = "NOT_TRIGGERED"
    UNKNOWN = "UNKNOWN"


class EvaluationDecision(str, Enum):
    NONE = "NONE"
    BUY_REVIEW = "BUY_REVIEW"
    SELL_REVIEW = "SELL_REVIEW"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class RuleResult:
    """Evidence for one rule evaluation.

    ``bool(result)`` is retained as a narrow compatibility convenience and is true
    only for ``TRIGGERED``. Callers needing uncertainty must inspect ``status``.
    """

    rule_id: str
    rule_type: str
    status: RuleStatus
    actual_value: float | None
    operator: str
    threshold: float
    unit: str
    reason: str
    note: str | None = None

    def __bool__(self) -> bool:
        return self.status is RuleStatus.TRIGGERED

    @property
    def triggered(self) -> bool:
        return self.status is RuleStatus.TRIGGERED

    @property
    def evidence(self) -> dict[str, Any]:
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result

    def __getitem__(self, key: str) -> Any:
        """Allow evidence-style access without giving up the typed result object."""

        return self.to_dict()[key]


_RULE_META: dict[str, tuple[str, str, str]] = {
    "price_above": ("price", ">=", "currency"),
    "price_below": ("price", "<=", "currency"),
    "pe_above": ("pe_ttm", ">=", "ratio"),
    "pe_below": ("pe_ttm", "<=", "ratio"),
    "roe_above": ("roe", ">=", "%"),
    "price_drop_pct": ("price", ">=", "%"),
}


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            number = float(text)
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
    return number if math.isfinite(number) else None


def load_rules() -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to load signals/rules.yaml")
    with RULES_PATH.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise TypeError("Rules snapshot must be a mapping")
    return loaded


def _validated_rule(
    rule: Mapping[str, Any],
) -> tuple[str, float, str, str, str, str]:
    if not isinstance(rule, Mapping):
        raise TypeError("Rule must be a mapping")
    rule_type = rule.get("type")
    if not isinstance(rule_type, str) or rule_type not in _RULE_META:
        raise ValueError(f"Unsupported rule type: {rule_type!r}")

    threshold = _finite_number(rule.get("value"))
    if threshold is None:
        raise ValueError(f"Rule {rule_type!r} requires a finite numeric value")
    if (
        rule_type in {"price_above", "price_below", "pe_above", "pe_below"}
        and threshold <= 0
    ):
        raise ValueError(f"Rule {rule_type!r} requires a positive threshold")
    if rule_type == "price_drop_pct" and threshold < 0:
        raise ValueError("Rule 'price_drop_pct' requires a non-negative threshold")

    field, operator, default_unit = _RULE_META[rule_type]
    rule_id = str(rule.get("id") or rule.get("rule_id") or rule_type)
    unit = str(rule.get("unit") or default_unit)
    return rule_type, threshold, field, operator, rule_id, unit


def _result(
    *,
    rule: Mapping[str, Any],
    rule_type: str,
    rule_id: str,
    status: RuleStatus,
    actual: float | None,
    operator: str,
    threshold: float,
    unit: str,
    reason: str,
) -> RuleResult:
    note = rule.get("note")
    return RuleResult(
        rule_id=rule_id,
        rule_type=rule_type,
        status=status,
        actual_value=actual,
        operator=operator,
        threshold=threshold,
        unit=unit,
        reason=reason,
        note=str(note) if note is not None else None,
    )


def _explicitly_unusable_fields(
    stock: Mapping[str, Any], rule_type: str
) -> tuple[str, ...]:
    """Read the stable reliability report when an integration supplied one."""

    report: Any = stock.get("reliability")
    if hasattr(report, "model_dump"):
        report = report.model_dump(mode="python")
    if not isinstance(report, Mapping):
        return ()
    fields: Any = report.get("fields")
    if not isinstance(fields, Mapping):
        return ()
    unusable: list[str] = []
    for field in sorted(fields_for_rule_type(rule_type)):
        evidence: Any = fields.get(field)
        if hasattr(evidence, "model_dump"):
            evidence = evidence.model_dump(mode="python")
        if isinstance(evidence, Mapping) and evidence.get("usable_for_signal") is False:
            unusable.append(field)
    return tuple(unusable)


def check_rule(
    rule: Mapping[str, Any],
    stock: Mapping[str, Any],
    cost_basis: float | None = None,
) -> RuleResult:
    """Evaluate one rule and return typed evidence.

    A usable positive price is a snapshot-wide safety gate. If it is missing,
    non-finite or non-positive, no rule can trigger, including PE/ROE rules.
    Invalid rule definitions raise immediately instead of masquerading as false.
    """

    rule_type, threshold, field, operator, rule_id, unit = _validated_rule(rule)
    unusable_fields = _explicitly_unusable_fields(stock, rule_type)
    if unusable_fields:
        return _result(
            rule=rule,
            rule_type=rule_type,
            rule_id=rule_id,
            status=RuleStatus.UNKNOWN,
            actual=None,
            operator=operator,
            threshold=threshold,
            unit=unit,
            reason=(
                "Reliability gate rejected required fields: "
                + ", ".join(unusable_fields)
            ),
        )
    price = _finite_number(stock.get("price"))
    if price is None or price <= 0:
        return _result(
            rule=rule,
            rule_type=rule_type,
            rule_id=rule_id,
            status=RuleStatus.UNKNOWN,
            actual=None,
            operator=operator,
            threshold=threshold,
            unit=unit,
            reason="Snapshot price is missing, non-finite, or non-positive",
        )

    actual: float | None
    if rule_type == "price_drop_pct":
        basis = _finite_number(cost_basis)
        if basis is None or basis <= 0:
            return _result(
                rule=rule,
                rule_type=rule_type,
                rule_id=rule_id,
                status=RuleStatus.UNKNOWN,
                actual=None,
                operator=operator,
                threshold=threshold,
                unit=unit,
                reason="Cost basis is missing, non-finite, or non-positive",
            )
        actual = (basis - price) / basis * 100
    else:
        actual = price if field == "price" else _finite_number(stock.get(field))
        if actual is None:
            return _result(
                rule=rule,
                rule_type=rule_type,
                rule_id=rule_id,
                status=RuleStatus.UNKNOWN,
                actual=None,
                operator=operator,
                threshold=threshold,
                unit=(
                    str(stock.get("currency") or "currency")
                    if field == "price"
                    else unit
                ),
                reason=f"{field} is missing or non-finite",
            )

    if rule_type == "pe_below" and actual <= 0:
        return _result(
            rule=rule,
            rule_type=rule_type,
            rule_id=rule_id,
            status=RuleStatus.NOT_TRIGGERED,
            actual=actual,
            operator=operator,
            threshold=threshold,
            unit=unit,
            reason="Non-positive PE cannot satisfy a pe_below value rule",
        )

    if rule_type in {"price_above", "pe_above", "roe_above", "price_drop_pct"}:
        triggered = actual >= threshold
    else:
        triggered = actual <= threshold
    status = RuleStatus.TRIGGERED if triggered else RuleStatus.NOT_TRIGGERED
    resolved_unit = (
        str(stock.get("currency") or "currency")
        if field == "price" and rule_type != "price_drop_pct"
        else unit
    )
    return _result(
        rule=rule,
        rule_type=rule_type,
        rule_id=rule_id,
        status=status,
        actual=actual,
        operator=operator,
        threshold=threshold,
        unit=resolved_unit,
        reason="Condition satisfied" if triggered else "Condition not satisfied",
    )


def _rule_list(config: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    rules = config.get(key, [])
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise TypeError(f"{key} must be a list")
    if not all(isinstance(rule, Mapping) for rule in rules):
        raise TypeError(f"Every entry in {key} must be a mapping")
    return rules


def _sell_group_status(results: list[RuleResult]) -> RuleStatus:
    if any(result.status is RuleStatus.TRIGGERED for result in results):
        return RuleStatus.TRIGGERED
    if any(result.status is RuleStatus.UNKNOWN for result in results):
        return RuleStatus.UNKNOWN
    return RuleStatus.NOT_TRIGGERED


def _buy_group_status(results: list[RuleResult]) -> RuleStatus:
    if not results:
        return RuleStatus.NOT_TRIGGERED
    if any(result.status is RuleStatus.NOT_TRIGGERED for result in results):
        return RuleStatus.NOT_TRIGGERED
    if any(result.status is RuleStatus.UNKNOWN for result in results):
        return RuleStatus.UNKNOWN
    return RuleStatus.TRIGGERED


def _watchlist(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    watchlist = snapshot.get("watchlist", snapshot)
    if not isinstance(watchlist, Mapping):
        raise TypeError("Rules snapshot watchlist must be a mapping")
    return watchlist


def evaluate(
    ticker: str,
    stock_data: Mapping[str, Any],
    rules_snapshot: Mapping[str, Any] | None = None,
    *,
    rules: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a ticker against an optional immutable rules snapshot.

    The result retains legacy ``sell``/``buy`` keys while adding a five-state
    decision and complete rule evidence. ``CONFLICT`` deliberately clears both
    legacy directional fields so old callers cannot emit two opposing alerts.
    """

    if rules_snapshot is not None and rules is not None:
        raise ValueError("Pass either rules_snapshot or rules, not both")
    snapshot = rules_snapshot if rules_snapshot is not None else rules
    if snapshot is None:
        snapshot = load_rules()
    if not isinstance(snapshot, Mapping):
        raise TypeError("Rules snapshot must be a mapping")

    watchlist = _watchlist(snapshot)
    config = watchlist.get(ticker)
    if config is None:
        return {
            "ticker": ticker,
            "name": ticker,
            "price": stock_data.get("price"),
            "decision": EvaluationDecision.NONE.value,
            "sell": [],
            "buy": False,
            "sell_status": RuleStatus.NOT_TRIGGERED.value,
            "buy_status": RuleStatus.NOT_TRIGGERED.value,
            "evidence": {"sell": [], "buy": []},
            "rule_results": [],
            "directional_suppressed": False,
            "message": f"{ticker} 不在监控列表",
        }
    if not isinstance(config, Mapping):
        raise TypeError(f"Watchlist entry for {ticker!r} must be a mapping")

    cost_basis = config.get("cost_basis")
    sell_rules = _rule_list(config, "sell_rules")
    buy_rules = _rule_list(config, "buy_rules")
    sell_results = [check_rule(rule, stock_data, cost_basis) for rule in sell_rules]
    buy_results = [check_rule(rule, stock_data, cost_basis) for rule in buy_rules]
    sell_status = _sell_group_status(sell_results)
    buy_status = _buy_group_status(buy_results)

    if sell_status is RuleStatus.TRIGGERED and buy_status is RuleStatus.TRIGGERED:
        decision = EvaluationDecision.CONFLICT
    elif sell_status is RuleStatus.TRIGGERED:
        decision = EvaluationDecision.SELL_REVIEW
    elif buy_status is RuleStatus.TRIGGERED:
        decision = EvaluationDecision.BUY_REVIEW
    elif any(
        result.status is RuleStatus.UNKNOWN for result in sell_results + buy_results
    ):
        decision = EvaluationDecision.UNKNOWN
    else:
        decision = EvaluationDecision.NONE

    conflict = decision is EvaluationDecision.CONFLICT
    legacy_sells = []
    if decision is EvaluationDecision.SELL_REVIEW:
        legacy_sells = [
            result.note or result.rule_id
            for result in sell_results
            if result.status is RuleStatus.TRIGGERED
        ]
    legacy_buy = decision is EvaluationDecision.BUY_REVIEW
    sell_evidence = [result.to_dict() for result in sell_results]
    buy_evidence = [result.to_dict() for result in buy_results]

    result: dict[str, Any] = {
        "ticker": ticker,
        "name": config.get("name", ticker),
        "price": stock_data.get("price"),
        "decision": decision.value,
        "sell": legacy_sells,
        "buy": legacy_buy,
        "sell_status": sell_status.value,
        "buy_status": buy_status.value,
        "evidence": {"sell": sell_evidence, "buy": buy_evidence},
        "rule_results": sell_evidence + buy_evidence,
        "directional_suppressed": conflict,
    }
    if "version" in snapshot:
        result["rules_version"] = snapshot["version"]
    return result
