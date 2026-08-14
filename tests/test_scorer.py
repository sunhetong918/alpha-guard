import math

import pytest

from analysis.scorer import (
    analyze,
    score_52_week_position,
    score_growth,
    score_moat,
    score_roe,
    score_safety_margin,
    score_valuation,
)


def test_negative_or_zero_pb_never_receives_value_points():
    negative_score, negative_note = score_valuation(None, -0.5)
    zero_score, zero_note = score_valuation(None, 0)

    assert negative_score == 0
    assert zero_score == 0
    assert "不计估值分" in negative_note
    assert "不计估值分" in zero_note


@pytest.mark.parametrize("invalid", [None, math.nan, math.inf, -math.inf, "bad"])
def test_nonfinite_or_missing_metrics_receive_no_neutral_points(invalid):
    assert score_roe(invalid)[0] == 0
    assert score_valuation(invalid, invalid)[0] == 0
    assert score_moat(invalid, invalid)[0] == 0
    assert score_growth(invalid, invalid)[0] == 0
    assert score_52_week_position(invalid, 80, 120)[0] == 0


def test_missing_52_week_range_has_no_neutral_gift_and_alias_is_compatible():
    expected = (0, "52周价格位置数据缺失或无效")
    assert score_52_week_position(100, None, None) == expected
    assert score_safety_margin(100, None, None) == expected
    assert score_safety_margin(100, 80, 120) == score_52_week_position(100, 80, 120)


def test_insufficient_coverage_suppresses_positive_investment_language():
    result = analyze(
        {
            "ticker": "THIN",
            "name": "Thin Data",
            "price": None,
            "roe": 30,
            "pe_ttm": math.nan,
            "pb": math.inf,
        }
    )

    assert result["total_score"] == 25
    assert result["coverage"] == 0.25
    assert result["confidence"] == "low"
    assert result["limitations"]
    assert "数据覆盖不足" in result["verdict"]
    assert "优质标的" not in result["verdict"]
    assert "买入" not in result["verdict"]


def test_complete_data_reports_full_coverage_and_renamed_dimension():
    result = analyze(
        {
            "ticker": "FULL",
            "name": "Full Data",
            "price": 105,
            "roe": 25,
            "pe_ttm": 10,
            "pb": 0.8,
            "free_cashflow": 1_000_000_000,
            "debt_to_equity": 20,
            "revenue_growth": 0.25,
            "earnings_growth": 0.30,
            "52w_low": 100,
            "52w_high": 200,
            "quality_issues": [],
        }
    )

    assert result["coverage"] == 1.0
    assert result["coverage_pct"] == 100.0
    assert result["confidence"] == "high"
    assert result["total_score"] == 100
    assert "52周价格位置（15分）" in result["breakdown"]
    assert all("安全边际" not in key for key in result["breakdown"])


def test_provider_quality_issues_reduce_confidence_and_are_limitations():
    result = analyze(
        {
            "price": 100,
            "roe": 20,
            "pe_ttm": 15,
            "pb": 2,
            "free_cashflow": 1,
            "debt_to_equity": 20,
            "revenue_growth": 0.1,
            "earnings_growth": 0.1,
            "52w_low": 80,
            "52w_high": 120,
            "quality_issues": ["quote:delayed"],
        }
    )

    assert result["coverage"] == 1.0
    assert result["confidence"] == "medium"
    assert "数据质量：quote:delayed" in result["limitations"]
