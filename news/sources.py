"""
新闻数据源模块
- Finnhub：美股公司新闻 + 市场新闻
- NewsAPI：全球新闻（财经/政治/军事）
- akshare：中文财经新闻（港股/A股相关）
"""
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests
import akshare as ak
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
NEWSAPI_KEY = os.getenv("NEWSAPI_API_KEY", "")


def fetch_finnhub_company_news(
    ticker: str, lookback_hours: int = 6
) -> list[dict]:
    """
    Finnhub 公司新闻
    返回 [{"title", "summary", "source", "url", "datetime", "origin"}]
    """
    if not FINNHUB_KEY:
        logger.warning("FINNHUB_API_KEY 未设置，跳过 Finnhub")
        return []

    now = datetime.now()
    date_from = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    url = "https://finnhub.io/api/v1/company-news"
    params = {
        "symbol": ticker,
        "from": date_from,
        "to": date_to,
        "token": FINNHUB_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        articles = resp.json()
        return [
            {
                "title": a.get("headline", ""),
                "summary": a.get("summary", ""),
                "source": a.get("source", "finnhub"),
                "url": a.get("url", ""),
                "datetime": datetime.fromtimestamp(a["datetime"]).isoformat()
                if a.get("datetime")
                else "",
                "origin": "finnhub",
            }
            for a in articles[:20]
        ]
    except Exception as e:
        logger.error(f"Finnhub 请求失败 ({ticker}): {e}")
        return []


def fetch_finnhub_general_news(category: str = "general") -> list[dict]:
    """
    Finnhub 市场大类新闻
    category: general | forex | crypto | merger
    """
    if not FINNHUB_KEY:
        return []

    url = "https://finnhub.io/api/v1/news"
    params = {"category": category, "token": FINNHUB_KEY}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        articles = resp.json()
        return [
            {
                "title": a.get("headline", ""),
                "summary": a.get("summary", ""),
                "source": a.get("source", "finnhub"),
                "url": a.get("url", ""),
                "datetime": datetime.fromtimestamp(a["datetime"]).isoformat()
                if a.get("datetime")
                else "",
                "origin": "finnhub",
            }
            for a in articles[:30]
        ]
    except Exception as e:
        logger.error(f"Finnhub general news 请求失败: {e}")
        return []


def fetch_newsapi(
    query: str,
    lookback_hours: int = 12,
    language: str = "en",
    page_size: int = 20,
) -> list[dict]:
    """
    NewsAPI 按关键词搜索新闻
    免费版限制：100 次/天，仅返回最近 24h
    """
    if not NEWSAPI_KEY:
        logger.warning("NEWSAPI_API_KEY 未设置，跳过 NewsAPI")
        return []

    now = datetime.utcnow()
    date_from = (now - timedelta(hours=lookback_hours)).isoformat() + "Z"

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "from": date_from,
        "language": language,
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": NEWSAPI_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("articles", [])
        return [
            {
                "title": a.get("title", ""),
                "summary": a.get("description", ""),
                "source": a.get("source", {}).get("name", "newsapi"),
                "url": a.get("url", ""),
                "datetime": a.get("publishedAt", ""),
                "origin": "newsapi",
            }
            for a in articles
        ]
    except Exception as e:
        logger.error(f"NewsAPI 请求失败 (query={query}): {e}")
        return []


def fetch_akshare_news() -> list[dict]:
    """
    akshare 中文财经新闻（东方财富快讯）
    无需 API Key，但数据偏 A 股 / 港股
    """
    try:
        df = ak.stock_info_global_em()
        articles = []
        for _, row in df.head(40).iterrows():
            articles.append({
                "title": str(row.get("标题", "")),
                "summary": str(row.get("内容", row.get("标题", ""))),
                "source": "东方财富",
                "url": str(row.get("链接", "")),
                "datetime": str(row.get("发布时间", "")),
                "origin": "akshare",
            })
        return articles
    except Exception as e:
        logger.error(f"akshare 新闻获取失败: {e}")
        return []


def fetch_all_news(
    tickers: list[str],
    macro_queries: list[str],
    config: Optional[dict] = None,
) -> list[dict]:
    """
    一次性拉取所有来源的新闻，去重后返回

    参数:
        tickers: 美股代码列表（港股暂不支持 Finnhub 公司新闻）
        macro_queries: 宏观搜索关键词列表
        config: sources 配置（来自 config.yaml）
    """
    config = config or {}
    all_articles = []
    seen_titles = set()

    # 1) Finnhub 个股新闻
    if config.get("finnhub", {}).get("enabled", True):
        lookback = config.get("finnhub", {}).get("lookback_hours", 6)
        for ticker in tickers:
            if not ticker.isdigit():  # 只拉美股
                articles = fetch_finnhub_company_news(ticker, lookback)
                for a in articles:
                    a["related_ticker"] = ticker
                all_articles.extend(articles)
        # Finnhub 市场大类
        all_articles.extend(fetch_finnhub_general_news())

    # 2) NewsAPI 宏观 / 自定义搜索
    if config.get("newsapi", {}).get("enabled", True):
        lookback = config.get("newsapi", {}).get("lookback_hours", 12)
        queries = list(set(
            macro_queries + config.get("newsapi", {}).get("extra_queries", [])
        ))
        for q in queries:
            all_articles.extend(fetch_newsapi(q, lookback))

    # 3) akshare 中文财经
    if config.get("akshare", {}).get("enabled", True):
        all_articles.extend(fetch_akshare_news())

    # 去重（按标题）
    deduped = []
    for a in all_articles:
        title = a.get("title", "").strip()
        if title and title not in seen_titles:
            seen_titles.add(title)
            deduped.append(a)

    logger.info(f"共获取 {len(deduped)} 条去重新闻（原始 {len(all_articles)} 条）")
    return deduped
