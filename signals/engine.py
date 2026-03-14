"""
信号引擎 —— 根据 rules.yaml 判断是否触发买卖提醒
"""
import yaml
from pathlib import Path
from typing import Optional


RULES_PATH = Path(__file__).parent / "rules.yaml"


def load_rules() -> dict:
    with open(RULES_PATH) as f:
        return yaml.safe_load(f)


def check_rule(rule: dict, stock: dict, cost_basis: Optional[float] = None) -> bool:
    """判断单条规则是否触发"""
    t = rule["type"]
    v = rule.get("value")
    price = stock.get("price", 0)
    pe = stock.get("pe_ttm")
    roe = stock.get("roe")

    if t == "price_above":
        return price >= v
    if t == "price_below":
        return price <= v
    if t == "pe_above":
        return pe is not None and pe >= v
    if t == "pe_below":
        return pe is not None and pe <= v
    if t == "roe_above":
        return roe is not None and roe >= v
    if t == "price_drop_pct" and cost_basis:
        drop = (cost_basis - price) / cost_basis * 100
        return drop >= v
    return False


def evaluate(ticker: str, stock_data: dict) -> dict:
    """
    对单只股票评估所有规则
    返回 {"sell": [...触发的卖出规则], "buy": bool（买入条件是否全满足）}
    """
    rules = load_rules()
    cfg = rules.get("watchlist", {}).get(ticker)
    if not cfg:
        return {"sell": [], "buy": False, "message": f"{ticker} 不在监控列表"}

    cost_basis = cfg.get("cost_basis")

    # 卖出：任意一条触发
    triggered_sells = []
    for rule in cfg.get("sell_rules", []):
        if check_rule(rule, stock_data, cost_basis):
            triggered_sells.append(rule.get("note", rule["type"]))

    # 买入：全部条件满足
    buy_rules = cfg.get("buy_rules", [])
    buy_triggered = bool(buy_rules) and all(
        check_rule(r, stock_data, cost_basis) for r in buy_rules
    )

    return {
        "ticker": ticker,
        "name": cfg.get("name", ticker),
        "price": stock_data.get("price"),
        "sell": triggered_sells,
        "buy": buy_triggered,
    }
