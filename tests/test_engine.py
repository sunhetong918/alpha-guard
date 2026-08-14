import math

import pytest

from signals.engine import RuleResult, RuleStatus, check_rule, evaluate


def _snapshot(*, sell_rules=None, buy_rules=None, cost_basis=100):
    return {
        "version": "test-v1",
        "watchlist": {
            "TEST": {
                "name": "Test Co",
                "cost_basis": cost_basis,
                "sell_rules": sell_rules or [],
                "buy_rules": buy_rules or [],
            }
        },
    }


@pytest.mark.parametrize("price", [None, math.nan, math.inf, -math.inf, 0, -1])
def test_invalid_price_is_unknown_and_never_triggers(price):
    rule = {"id": "cheap", "type": "price_below", "value": 100}
    checked = check_rule(rule, {"price": price, "currency": "USD"})

    assert isinstance(checked, RuleResult)
    assert checked.status is RuleStatus.UNKNOWN
    assert not checked
    assert checked.actual_value is None

    result = evaluate("TEST", {"price": price}, _snapshot(buy_rules=[rule]))
    assert result["decision"] == "UNKNOWN"
    assert result["sell"] == []
    assert result["buy"] is False


def test_missing_price_is_unknown_for_non_price_rule_too():
    checked = check_rule(
        {"type": "roe_above", "value": 15},
        {"roe": 30},
    )
    assert checked.status is RuleStatus.UNKNOWN
    assert not checked


@pytest.mark.parametrize("pe", [0, -0.01, -50])
def test_nonpositive_pe_never_satisfies_pe_below(pe):
    result = check_rule(
        {"id": "pe-value", "type": "pe_below", "value": 20},
        {"price": 100, "pe_ttm": pe},
    )

    assert result.status is RuleStatus.NOT_TRIGGERED
    assert not result
    assert result.actual_value == pe
    assert "Non-positive PE" in result.reason


def test_rule_result_contains_auditable_evidence_and_bool_compatibility():
    result = check_rule(
        {"id": "target", "type": "price_above", "value": 100, "note": "review"},
        {"price": 100, "currency": "USD"},
    )

    assert result.status is RuleStatus.TRIGGERED
    assert bool(result) is True
    assert result.evidence == {
        "rule_id": "target",
        "rule_type": "price_above",
        "status": "TRIGGERED",
        "actual_value": 100.0,
        "operator": ">=",
        "threshold": 100.0,
        "unit": "USD",
        "reason": "Condition satisfied",
        "note": "review",
    }
    assert result["status"] == "TRIGGERED"


@pytest.mark.parametrize("bad_value", [None, math.nan, math.inf, "bad"])
def test_invalid_rule_threshold_fails_validation(bad_value):
    with pytest.raises(ValueError, match="finite numeric value"):
        check_rule(
            {"type": "price_above", "value": bad_value},
            {"price": 100},
        )


def test_unknown_rule_type_fails_even_when_snapshot_price_is_invalid():
    with pytest.raises(ValueError, match="Unsupported rule type"):
        check_rule({"type": "mystery", "value": 1}, {"price": None})


def test_missing_or_nonpositive_cost_basis_is_unknown():
    for basis in (None, 0, math.inf):
        result = check_rule(
            {"type": "price_drop_pct", "value": 10},
            {"price": 80},
            basis,
        )
        assert result.status is RuleStatus.UNKNOWN
        assert not result


def test_buy_review_preserves_legacy_buy_field_and_evidence():
    snapshot = _snapshot(
        sell_rules=[{"type": "price_above", "value": 200}],
        buy_rules=[
            {"type": "price_below", "value": 150},
            {"type": "pe_below", "value": 20},
        ],
    )
    result = evaluate("TEST", {"price": 100, "pe_ttm": 10}, snapshot)

    assert result["decision"] == "BUY_REVIEW"
    assert result["buy"] is True
    assert result["sell"] == []
    assert result["buy_status"] == "TRIGGERED"
    assert len(result["evidence"]["buy"]) == 2
    assert result["rules_version"] == "test-v1"


def test_sell_review_preserves_legacy_reason_list():
    snapshot = _snapshot(
        sell_rules=[
            {
                "id": "take-profit",
                "type": "price_above",
                "value": 120,
                "note": "达到目标价，人工复核",
            }
        ],
        buy_rules=[{"type": "price_below", "value": 80}],
    )
    result = evaluate("TEST", {"price": 130}, rules=snapshot)

    assert result["decision"] == "SELL_REVIEW"
    assert result["sell"] == ["达到目标价，人工复核"]
    assert result["buy"] is False


def test_conflict_suppresses_both_legacy_directional_fields():
    snapshot = _snapshot(
        sell_rules=[{"type": "price_above", "value": 100}],
        buy_rules=[{"type": "price_below", "value": 200}],
    )
    result = evaluate("TEST", {"price": 150}, snapshot)

    assert result["decision"] == "CONFLICT"
    assert result["sell_status"] == "TRIGGERED"
    assert result["buy_status"] == "TRIGGERED"
    assert result["sell"] == []
    assert result["buy"] is False
    assert result["directional_suppressed"] is True


def test_no_trigger_is_none_while_missing_metric_is_unknown():
    snapshot = _snapshot(buy_rules=[{"type": "pe_below", "value": 20}])

    none_result = evaluate("TEST", {"price": 100, "pe_ttm": 30}, snapshot)
    unknown_result = evaluate("TEST", {"price": 100}, snapshot)

    assert none_result["decision"] == "NONE"
    assert unknown_result["decision"] == "UNKNOWN"
    assert unknown_result["evidence"]["buy"][0]["status"] == "UNKNOWN"


def test_unmonitored_ticker_retains_compatibility_shape():
    result = evaluate("OTHER", {"price": None}, _snapshot())

    assert result["decision"] == "NONE"
    assert result["sell"] == []
    assert result["buy"] is False
    assert result["evidence"] == {"sell": [], "buy": []}


def test_explicit_field_reliability_is_a_final_rule_level_safety_gate():
    stock = {
        "price": 150,
        "pe_ttm": 40,
        "reliability": {
            "usable_for_signal": False,
            "fields": {
                "price": {"usable_for_signal": True},
                "pe_ttm": {"usable_for_signal": False},
            },
        },
    }
    snapshot = _snapshot(
        sell_rules=[
            {"id": "fresh-price", "type": "price_above", "value": 120},
            {"id": "stale-pe", "type": "pe_above", "value": 30},
        ]
    )

    result = evaluate("TEST", stock, snapshot)

    assert result["decision"] == "SELL_REVIEW"
    assert result["evidence"]["sell"][0]["status"] == "TRIGGERED"
    assert result["evidence"]["sell"][1]["status"] == "UNKNOWN"
    assert "Reliability gate" in result["evidence"]["sell"][1]["reason"]
