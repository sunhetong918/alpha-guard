"""Finite-safe, coverage-aware fundamental scoring.

The score remains a 100-point compatibility surface, but missing data earns no
points and is reported separately as coverage/confidence. The former "safety
margin" dimension is explicitly a 52-week price-position heuristic; it is not an
estimate of intrinsic value.
"""

from __future__ import annotations

import math
from typing import Any

MIN_DECISION_COVERAGE = 0.60


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text or text.lower() in {"nan", "inf", "+inf", "-inf", "none", "n/a"}:
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


def score_roe(roe: float | None) -> tuple[int, str]:
    """ROE score (25 points); ROE is expressed in percentage points."""

    value = _finite_number(roe)
    if value is None:
        return 0, "ROE 数据缺失或无效"
    if value >= 20:
        return 25, f"ROE {value:.1f}% — 优秀，具备强盈利能力"
    if value >= 15:
        return 18, f"ROE {value:.1f}% — 良好"
    if value >= 10:
        return 10, f"ROE {value:.1f}% — 一般"
    return 3, f"ROE {value:.1f}% — 偏低，盈利能力存疑"


def score_valuation(pe: float | None, pb: float | None) -> tuple[int, str]:
    """PE/PB valuation score (25 points). Non-positive multiples earn zero."""

    score = 0
    notes: list[str] = []
    pe_value = _finite_number(pe)
    pb_value = _finite_number(pb)

    if pe_value is None:
        notes.append("PE 数据缺失或无效")
    elif pe_value <= 0:
        notes.append("PE 非正（通常代表亏损或口径不可用），不计估值分")
    elif pe_value <= 15:
        score += 15
        notes.append(f"PE {pe_value:.1f} — 较低")
    elif pe_value <= 25:
        score += 10
        notes.append(f"PE {pe_value:.1f} — 合理")
    elif pe_value <= 40:
        score += 5
        notes.append(f"PE {pe_value:.1f} — 偏高")
    else:
        notes.append(f"PE {pe_value:.1f} — 高估值风险")

    if pb_value is None:
        notes.append("PB 数据缺失或无效")
    elif pb_value <= 0:
        notes.append("PB 非正（可能为负净资产），不计估值分")
    elif pb_value <= 1:
        score += 10
        notes.append(f"PB {pb_value:.1f} — 低于账面价值")
    elif pb_value <= 3:
        score += 7
        notes.append(f"PB {pb_value:.1f} — 合理")
    elif pb_value <= 6:
        score += 3
        notes.append(f"PB {pb_value:.1f} — 偏高")
    else:
        notes.append(f"PB {pb_value:.1f} — 高估值")

    return min(score, 25), " | ".join(notes)


def score_moat(
    free_cashflow: float | None,
    debt_to_equity: float | None,
) -> tuple[int, str]:
    """Cash-flow/balance-sheet proxy score (20 points)."""

    score = 0
    notes: list[str] = []
    cashflow = _finite_number(free_cashflow)
    leverage = _finite_number(debt_to_equity)

    if cashflow is None:
        notes.append("自由现金流数据缺失或无效")
    elif cashflow > 0:
        score += 12
        notes.append(f"自由现金流为正（{cashflow / 1e8:.1f}亿）")
    else:
        notes.append("自由现金流非正")

    if leverage is None:
        notes.append("负债权益比数据缺失或无效")
    elif leverage < 0:
        notes.append("负债权益比为负（可能为负净资产），不计分")
    elif leverage <= 30:
        score += 8
        notes.append(f"负债权益比 {leverage:.0f}% — 较低")
    elif leverage <= 80:
        score += 4
        notes.append(f"负债权益比 {leverage:.0f}% — 可接受")
    else:
        notes.append(f"负债权益比 {leverage:.0f}% — 偏高，注意风险")

    return score, " | ".join(notes)


def score_growth(
    revenue_growth: float | None,
    earnings_growth: float | None,
) -> tuple[int, str]:
    """Revenue/earnings growth score (15 points); inputs are provider ratios."""

    score = 0
    notes: list[str] = []
    revenue = _finite_number(revenue_growth)
    earnings = _finite_number(earnings_growth)

    if revenue is None:
        notes.append("营收增速数据缺失或无效")
    else:
        revenue_pct = revenue * 100
        if revenue_pct >= 20:
            score += 8
            notes.append(f"营收增速 {revenue_pct:.1f}% — 高成长")
        elif revenue_pct >= 10:
            score += 5
            notes.append(f"营收增速 {revenue_pct:.1f}% — 稳健")
        elif revenue_pct >= 0:
            score += 2
            notes.append(f"营收增速 {revenue_pct:.1f}% — 缓慢")
        else:
            notes.append(f"营收增速 {revenue_pct:.1f}% — 收缩")

    if earnings is None:
        notes.append("利润增速数据缺失或无效")
    else:
        earnings_pct = earnings * 100
        if earnings_pct >= 20:
            score += 7
            notes.append(f"利润增速 {earnings_pct:.1f}% — 高成长")
        elif earnings_pct >= 10:
            score += 4
            notes.append(f"利润增速 {earnings_pct:.1f}% — 稳健")
        elif earnings_pct >= 0:
            score += 1
            notes.append(f"利润增速 {earnings_pct:.1f}% — 缓慢")
        else:
            notes.append(f"利润增速 {earnings_pct:.1f}% — 下滑")

    return min(score, 15), " | ".join(notes)


def score_52_week_position(
    price: float | None,
    low_52w: float | None,
    high_52w: float | None,
) -> tuple[int, str]:
    """Score the current price's position in its 52-week range (15 points).

    This is a price-position heuristic only. It must not be described as an
    intrinsic-value safety margin.
    """

    current = _finite_number(price)
    low = _finite_number(low_52w)
    high = _finite_number(high_52w)
    if (
        current is None
        or current <= 0
        or low is None
        or low <= 0
        or high is None
        or high <= 0
    ):
        return 0, "52周价格位置数据缺失或无效"
    if high <= low:
        return 0, "52周价格区间无效（最高价必须高于最低价）"

    position = (current - low) / (high - low)
    pct_from_low = (current - low) / low * 100

    if position <= 0.2:
        return 15, f"距52周低点 {pct_from_low:.1f}%，位于区间低位"
    if position <= 0.4:
        return 10, f"距52周低点 {pct_from_low:.1f}%，位于区间偏低位置"
    if position <= 0.7:
        return 5, f"距52周低点 {pct_from_low:.1f}%，位于区间中部"
    return 2, f"距52周低点 {pct_from_low:.1f}%，位于区间高位"


def score_safety_margin(
    price: float | None,
    low_52w: float | None,
    high_52w: float | None,
) -> tuple[int, str]:
    """Compatibility alias for :func:`score_52_week_position`."""

    return score_52_week_position(price, low_52w, high_52w)


def _coverage_and_limitations(stock_data: dict[str, Any]) -> tuple[float, list[str]]:
    covered = 0
    limitations: list[str] = []

    weighted_fields = (
        ("roe", 25, "ROE"),
        ("pe_ttm", 15, "PE"),
        ("pb", 10, "PB"),
        ("free_cashflow", 12, "自由现金流"),
        ("debt_to_equity", 8, "负债权益比"),
        ("revenue_growth", 8, "营收增速"),
        ("earnings_growth", 7, "利润增速"),
    )
    for key, weight, label in weighted_fields:
        value = _finite_number(stock_data.get(key))
        invalid_domain = (
            key in {"pe_ttm", "pb"} and value is not None and value <= 0
        ) or (key == "debt_to_equity" and value is not None and value < 0)
        if value is None or invalid_domain:
            limitations.append(f"{label} 数据缺失或非有限")
        else:
            covered += weight

    price = _finite_number(stock_data.get("price"))
    low = _finite_number(stock_data.get("52w_low"))
    high = _finite_number(stock_data.get("52w_high"))
    if (
        price is not None
        and price > 0
        and low is not None
        and low > 0
        and high is not None
        and high > low
    ):
        covered += 15
    else:
        limitations.append("52周价格位置数据缺失或区间无效")

    pe = _finite_number(stock_data.get("pe_ttm"))
    pb = _finite_number(stock_data.get("pb"))
    leverage = _finite_number(stock_data.get("debt_to_equity"))
    if pe is not None and pe <= 0:
        limitations.append("PE 非正，不计估值分")
    if pb is not None and pb <= 0:
        limitations.append("PB 非正，不计估值分")
    if leverage is not None and leverage < 0:
        limitations.append("负债权益比为负，不计杠杆质量分")

    provider_issues = stock_data.get("quality_issues") or []
    if isinstance(provider_issues, str):
        provider_issues = [provider_issues]
    if isinstance(provider_issues, (list, tuple, set)):
        for issue in provider_issues:
            text = str(issue).strip()
            if text:
                limitations.append(f"数据质量：{text}")

    # Preserve order while avoiding repeated messages from provider + scorer gates.
    limitations = list(dict.fromkeys(limitations))
    return round(covered / 100, 2), limitations


def _confidence(coverage: float, stock_data: dict[str, Any]) -> str:
    if coverage >= 0.80:
        confidence = "high"
    elif coverage >= MIN_DECISION_COVERAGE:
        confidence = "medium"
    else:
        confidence = "low"

    if stock_data.get("quality_issues"):
        if confidence == "high":
            return "medium"
        if confidence == "medium":
            return "low"
    return confidence


def analyze(stock_data: dict[str, Any]) -> dict[str, Any]:
    """Build a coverage-aware 100-point score report."""

    d = stock_data
    s_roe, n_roe = score_roe(d.get("roe"))
    s_val, n_val = score_valuation(d.get("pe_ttm"), d.get("pb"))
    s_moat, n_moat = score_moat(d.get("free_cashflow"), d.get("debt_to_equity"))
    s_growth, n_growth = score_growth(d.get("revenue_growth"), d.get("earnings_growth"))
    s_position, n_position = score_52_week_position(
        d.get("price"), d.get("52w_low"), d.get("52w_high")
    )

    total = s_roe + s_val + s_moat + s_growth + s_position
    coverage, limitations = _coverage_and_limitations(d)
    confidence = _confidence(coverage, d)

    if coverage < MIN_DECISION_COVERAGE:
        verdict = "⚠️ 数据覆盖不足，无法解释该研究评分"
    elif total >= 75:
        verdict = "研究评分位于高分区间；需核对行业适用性与数据口径"
    elif total >= 55:
        verdict = "研究评分位于中高区间；需结合原始披露继续核验"
    elif total >= 35:
        verdict = "研究评分位于中低区间；多项指标仍需独立核验"
    else:
        verdict = "研究评分位于低分区间；该结果不等同于操作建议"

    return {
        "ticker": d.get("ticker"),
        "name": d.get("name"),
        "price": d.get("price"),
        "total_score": total,
        "verdict": verdict,
        "coverage": coverage,
        "coverage_pct": round(coverage * 100, 1),
        "confidence": confidence,
        "limitations": limitations,
        "breakdown": {
            "ROE（25分）": (s_roe, n_roe),
            "估值（25分）": (s_val, n_val),
            "现金流与杠杆代理（20分）": (s_moat, n_moat),
            "成长性（15分）": (s_growth, n_growth),
            "52周价格位置（15分）": (s_position, n_position),
        },
    }


def format_report(result: dict[str, Any]) -> str:
    """Format a score report for a text notifier."""

    lines = [
        f"📊 *{result.get('name')}* ({result.get('ticker')})",
        f"💰 提供者报价：{result.get('price')}",
        f"🏆 综合评分：*{result.get('total_score')}/100*",
    ]
    if "coverage" in result:
        coverage_pct = result.get("coverage_pct", result["coverage"] * 100)
        lines.append(
            f"🧾 数据覆盖率：{coverage_pct:.1f}%（置信度：{result.get('confidence', 'unknown')}）"
        )
    lines.extend([str(result.get("verdict", "")), "", "─── 评分明细 ───"])

    for dimension, (score, note) in result.get("breakdown", {}).items():
        lines.append(f"• {dimension}：{score}分 — {note}")

    limitations = result.get("limitations") or []
    if limitations:
        lines.extend(["", "─── 数据限制 ───"])
        lines.extend(f"• {limitation}" for limitation in limitations)

    lines += [
        "",
        "⚠️ 以上为描述性研究评分，不构成投资建议或适当性评估。",
        "Alpha Guard 未执行任何交易；请核对原始披露与券商报价。",
    ]
    return "\n".join(lines)
