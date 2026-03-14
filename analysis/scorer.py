"""
基本面分析模块 —— 巴菲特式价值投资评分体系

评分维度（满分 100）：
  - ROE 持续性      25 分
  - 估值合理性      25 分（PE / PB）
  - 护城河指标      20 分（自由现金流、负债率）
  - 成长性          15 分（营收/利润增速）
  - 安全边际        15 分（距 52 周低点距离）
"""
from typing import Optional


def score_roe(roe: Optional[float]) -> tuple[int, str]:
    """ROE 评分（满分 25）"""
    if roe is None:
        return 0, "ROE 数据缺失"
    if roe >= 20:
        return 25, f"ROE {roe:.1f}% — 优秀，具备强护城河"
    if roe >= 15:
        return 18, f"ROE {roe:.1f}% — 良好"
    if roe >= 10:
        return 10, f"ROE {roe:.1f}% — 一般"
    return 3, f"ROE {roe:.1f}% — 偏低，盈利能力存疑"


def score_valuation(pe: Optional[float], pb: Optional[float]) -> tuple[int, str]:
    """估值评分（满分 25）"""
    score = 0
    notes = []

    if pe is not None:
        if pe <= 0:
            notes.append("PE 为负（亏损）")
        elif pe <= 15:
            score += 15
            notes.append(f"PE {pe:.1f} — 低估")
        elif pe <= 25:
            score += 10
            notes.append(f"PE {pe:.1f} — 合理")
        elif pe <= 40:
            score += 5
            notes.append(f"PE {pe:.1f} — 偏高")
        else:
            notes.append(f"PE {pe:.1f} — 高估风险")

    if pb is not None:
        if pb <= 1:
            score += 10
            notes.append(f"PB {pb:.1f} — 破净，极度低估")
        elif pb <= 3:
            score += 7
            notes.append(f"PB {pb:.1f} — 合理")
        elif pb <= 6:
            score += 3
            notes.append(f"PB {pb:.1f} — 偏高")
        else:
            notes.append(f"PB {pb:.1f} — 高估")

    return min(score, 25), " | ".join(notes)


def score_moat(
    free_cashflow: Optional[float],
    debt_to_equity: Optional[float],
) -> tuple[int, str]:
    """护城河评分（满分 20）"""
    score = 0
    notes = []

    if free_cashflow is not None:
        if free_cashflow > 0:
            score += 12
            notes.append(f"自由现金流为正（{free_cashflow/1e8:.1f}亿）")
        else:
            notes.append("自由现金流为负，烧钱阶段")

    if debt_to_equity is not None:
        if debt_to_equity <= 30:
            score += 8
            notes.append(f"负债率 {debt_to_equity:.0f}% — 健康")
        elif debt_to_equity <= 80:
            score += 4
            notes.append(f"负债率 {debt_to_equity:.0f}% — 可接受")
        else:
            notes.append(f"负债率 {debt_to_equity:.0f}% — 偏高，注意风险")

    return score, " | ".join(notes) if notes else "护城河数据不足"


def score_growth(
    revenue_growth: Optional[float],
    earnings_growth: Optional[float],
) -> tuple[int, str]:
    """成长性评分（满分 15）"""
    score = 0
    notes = []

    if revenue_growth is not None:
        rg = revenue_growth * 100
        if rg >= 20:
            score += 8
            notes.append(f"营收增速 {rg:.1f}% — 高成长")
        elif rg >= 10:
            score += 5
            notes.append(f"营收增速 {rg:.1f}% — 稳健")
        elif rg >= 0:
            score += 2
            notes.append(f"营收增速 {rg:.1f}% — 缓慢")
        else:
            notes.append(f"营收增速 {rg:.1f}% — 收缩")

    if earnings_growth is not None:
        eg = earnings_growth * 100
        if eg >= 20:
            score += 7
            notes.append(f"利润增速 {eg:.1f}% — 高成长")
        elif eg >= 10:
            score += 4
            notes.append(f"利润增速 {eg:.1f}% — 稳健")
        elif eg >= 0:
            score += 1
            notes.append(f"利润增速 {eg:.1f}% — 缓慢")
        else:
            notes.append(f"利润增速 {eg:.1f}% — 下滑")

    return min(score, 15), " | ".join(notes) if notes else "成长性数据不足"


def score_safety_margin(
    price: float,
    low_52w: Optional[float],
    high_52w: Optional[float],
) -> tuple[int, str]:
    """安全边际评分（满分 15）：当前价距 52 周低点的位置"""
    if low_52w is None or high_52w is None or high_52w == low_52w:
        return 5, "52周数据不足，给中性分"

    # 当前价在 52 周区间的位置（0=最低，1=最高）
    position = (price - low_52w) / (high_52w - low_52w)
    pct_from_low = (price - low_52w) / low_52w * 100

    if position <= 0.2:
        return 15, f"距52周低点仅 {pct_from_low:.1f}%，安全边际充足"
    if position <= 0.4:
        return 10, f"距52周低点 {pct_from_low:.1f}%，位置偏低，可关注"
    if position <= 0.7:
        return 5, f"距52周低点 {pct_from_low:.1f}%，中间位置"
    return 2, f"距52周低点 {pct_from_low:.1f}%，接近高位，谨慎"


def analyze(stock_data: dict) -> dict:
    """
    综合评分入口
    返回评分报告 dict
    """
    d = stock_data

    s_roe, n_roe = score_roe(d.get("roe"))
    s_val, n_val = score_valuation(d.get("pe_ttm"), d.get("pb"))
    s_moat, n_moat = score_moat(d.get("free_cashflow"), d.get("debt_to_equity"))
    s_growth, n_growth = score_growth(d.get("revenue_growth"), d.get("earnings_growth"))
    s_safety, n_safety = score_safety_margin(
        d.get("price", 0), d.get("52w_low"), d.get("52w_high")
    )

    total = s_roe + s_val + s_moat + s_growth + s_safety

    if total >= 75:
        verdict = "⭐⭐⭐ 优质标的，可重点关注"
    elif total >= 55:
        verdict = "⭐⭐ 基本面良好，关注估值时机"
    elif total >= 35:
        verdict = "⭐ 一般，需更多研究再决策"
    else:
        verdict = "❌ 基本面较弱，谨慎"

    return {
        "ticker": d.get("ticker"),
        "name": d.get("name"),
        "price": d.get("price"),
        "total_score": total,
        "verdict": verdict,
        "breakdown": {
            "ROE（25分）": (s_roe, n_roe),
            "估值（25分）": (s_val, n_val),
            "护城河（20分）": (s_moat, n_moat),
            "成长性（15分）": (s_growth, n_growth),
            "安全边际（15分）": (s_safety, n_safety),
        },
    }


def format_report(result: dict) -> str:
    """格式化为可读文本，用于 Telegram 消息"""
    lines = [
        f"📊 *{result['name']}* ({result['ticker']})",
        f"💰 当前价格：{result['price']}",
        f"🏆 综合评分：*{result['total_score']}/100*",
        f"{result['verdict']}",
        "",
        "─── 评分明细 ───",
    ]
    for dim, (score, note) in result["breakdown"].items():
        lines.append(f"• {dim}：{score}分 — {note}")

    lines += [
        "",
        "⚠️ 以上为量化规则评分，不构成投资建议。",
        "最终决策请结合自身判断。",
    ]
    return "\n".join(lines)
