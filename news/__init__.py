"""Validated news ingestion and optional AI labelling."""

from .filter import AIScoreResult, ai_score_article, filter_news, match_keywords
from .sources import (
    fetch_akshare_news,
    fetch_all_news,
    fetch_finnhub_company_news,
    fetch_finnhub_general_news,
    fetch_newsapi,
)

__all__ = [
    "AIScoreResult",
    "ai_score_article",
    "fetch_akshare_news",
    "fetch_all_news",
    "fetch_finnhub_company_news",
    "fetch_finnhub_general_news",
    "fetch_newsapi",
    "filter_news",
    "match_keywords",
]
