"""Stable decision-contract identity for trusted-silence baselines."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from config import InstrumentConfig, RulesConfig
from reliability import required_fields_for_rules


# Bump whenever engine semantics, rule-to-field mapping, or session eligibility
# changes even if the user-facing RulesConfig payload stays byte-for-byte equal.
PROTECTION_CONTRACT_SCHEMA_VERSION = 1


def _rule_contract(
    instrument: InstrumentConfig,
    group: str,
) -> list[dict[str, str | float]]:
    rules = instrument.buy_rules if group == "buy" else instrument.sell_rules
    return [
        {"group": group, "type": rule.type, "value": rule.value}
        for rule in sorted(rules, key=lambda item: (item.type, item.value))
    ]


def protection_contract_version(rules: RulesConfig, market: str) -> str:
    """Hash the exact decision/freshness contract for one enabled market.

    Presentation and delivery controls are intentionally excluded.  The hash
    changes only when evidence eligibility, rule semantics, or cache fallback
    availability changes, forcing a new protection baseline for that market.
    """

    if market not in {"US", "HK"}:
        raise ValueError("market must be US or HK")
    instruments: list[dict[str, Any]] = []
    freshness = rules.reliability.freshness
    for ticker, instrument in sorted(rules.watchlist.items()):
        if not instrument.enabled or instrument.market != market:
            continue
        required_fields = set(required_fields_for_rules(instrument))
        item: dict[str, Any] = {
            "ticker": ticker,
            "market": instrument.market,
            "currency": instrument.currency,
            "buy": _rule_contract(instrument, "buy"),
            "sell": _rule_contract(instrument, "sell"),
            "required_fields": {
                field: policy.model_dump(mode="json")
                for field, policy in freshness.fields.items()
                if field in required_fields
            },
            "future_tolerance_seconds": freshness.future_tolerance_seconds,
        }
        if any(
            rule.type == "price_drop_pct"
            for rule in (*instrument.buy_rules, *instrument.sell_rules)
        ):
            item["cost_basis"] = instrument.cost_basis
        instruments.append(item)
    if not instruments:
        raise ValueError("an enabled market requires at least one instrument")
    payload = {
        "schema_version": PROTECTION_CONTRACT_SCHEMA_VERSION,
        "market": market,
        "instruments": instruments,
        "provider_cache": {
            "fresh_cache_seconds": (
                rules.reliability.provider.fresh_cache_seconds
            ),
            "stale_if_error_seconds": (
                rules.reliability.provider.stale_if_error_seconds
            ),
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
