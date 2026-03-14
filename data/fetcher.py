"""
数据获取模块
- 美股：yfinance
- 港股：akshare
"""
import yfinance as yf
import akshare as ak
import pandas as pd
from typing import Optional


def get_us_stock(ticker: str) -> dict:
    """获取美股基本面数据，ticker 如 'AAPL'"""
    t = yf.Ticker(ticker)
    info = t.info
    hist = t.history(period="1y")

    return {
        "ticker": ticker,
        "market": "US",
        "name": info.get("longName", ticker),
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "pe_ttm": info.get("trailingPE"),
        "pb": info.get("priceToBook"),
        "roe": _calc_roe(info),
        "market_cap": info.get("marketCap"),
        "52w_high": info.get("fiftyTwoWeekHigh"),
        "52w_low": info.get("fiftyTwoWeekLow"),
        "dividend_yield": info.get("dividendYield"),
        "debt_to_equity": info.get("debtToEquity"),
        "free_cashflow": info.get("freeCashflow"),
        "revenue_growth": info.get("revenueGrowth"),
        "earnings_growth": info.get("earningsGrowth"),
        "hist": hist,
    }


def get_hk_stock(ticker: str) -> dict:
    """
    获取港股基本面数据
    ticker 格式：'00700'（腾讯）、'09988'（阿里）
    """
    # akshare 港股实时行情
    df = ak.stock_hk_spot_em()
    row = df[df["代码"] == ticker]
    if row.empty:
        raise ValueError(f"港股代码 {ticker} 未找到")
    row = row.iloc[0]

    # 港股历史价格（用 yfinance，格式加 .HK）
    yf_ticker = ticker.lstrip("0") + ".HK" if not ticker.endswith(".HK") else ticker
    hist = yf.Ticker(yf_ticker).history(period="1y")

    return {
        "ticker": ticker,
        "market": "HK",
        "name": row.get("名称", ticker),
        "price": float(row.get("最新价", 0)),
        "pe_ttm": float(row.get("市盈率", 0)) if row.get("市盈率") else None,
        "pb": float(row.get("市净率", 0)) if row.get("市净率") else None,
        "roe": None,  # akshare 港股实时不含 ROE，需单独拉财报
        "52w_high": hist["High"].max() if not hist.empty else None,
        "52w_low": hist["Low"].min() if not hist.empty else None,
        "hist": hist,
    }


def get_stock(ticker: str, market: str = "auto") -> dict:
    """
    统一入口
    market: 'US' | 'HK' | 'auto'（根据 ticker 格式自动判断）
    """
    if market == "auto":
        # 纯数字 5 位 → 港股；否则 → 美股
        market = "HK" if ticker.isdigit() else "US"

    if market == "HK":
        return get_hk_stock(ticker)
    else:
        return get_us_stock(ticker)


def _calc_roe(info: dict) -> Optional[float]:
    """从 yfinance info 计算 ROE = 净利润 / 股东权益"""
    net_income = info.get("netIncomeToCommon")
    equity = info.get("bookValue")
    shares = info.get("sharesOutstanding")
    if net_income and equity and shares and equity > 0:
        total_equity = equity * shares
        return round(net_income / total_equity * 100, 2)
    return info.get("returnOnEquity")  # fallback
