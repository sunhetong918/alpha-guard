"""Bounded and deterministic adapters for public news providers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from config import NewsConfig, NewsSourcesConfig, Settings, get_settings

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 300
SUMMARY_MAX_CHARS = 2_000
SOURCE_MAX_CHARS = 120
URL_MAX_CHARS = 2_048
MAX_PROVIDER_ARTICLES = 100
FINNHUB_GENERAL_CATEGORIES = frozenset({"general", "forex", "crypto", "merger"})

HttpGet = Callable[..., Any]


def fetch_finnhub_company_news(
    ticker: str,
    lookback_hours: int = 6,
    *,
    settings: Settings | None = None,
    http_get: HttpGet | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent Finnhub company news without putting credentials in URLs."""

    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be greater than zero")
    ticker = _bounded_text(ticker, 32).strip()
    if not ticker:
        raise ValueError("ticker must not be empty")

    runtime = settings or get_settings()
    api_key = _secret_value(runtime.finnhub_api_key)
    if not api_key:
        logger.warning("Finnhub disabled for this request: FINNHUB_API_KEY is missing")
        return []

    now = datetime.now(UTC)
    params: dict[str, str | int] = {
        "symbol": ticker,
        "from": (now - timedelta(hours=lookback_hours)).date().isoformat(),
        "to": now.date().isoformat(),
    }
    headers = {"X-Finnhub-Token": api_key}

    try:
        response = (http_get or requests.get)(
            "https://finnhub.io/api/v1/company-news",
            params=params,
            headers=headers,
            timeout=runtime.http_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise TypeError("unexpected response shape")
        return [
            _normalise_finnhub_article(item)
            for item in payload[:20]
            if isinstance(item, Mapping)
        ]
    except Exception as exc:  # noqa: BLE001 - provider boundary must degrade
        _log_provider_failure("Finnhub", "company news", exc)
        return []


def fetch_finnhub_general_news(
    category: str = "general",
    *,
    settings: Settings | None = None,
    http_get: HttpGet | None = None,
) -> list[dict[str, Any]]:
    """Fetch a bounded Finnhub market-news category."""

    if category not in FINNHUB_GENERAL_CATEGORIES:
        allowed = ", ".join(sorted(FINNHUB_GENERAL_CATEGORIES))
        raise ValueError(f"unsupported Finnhub category; expected one of: {allowed}")

    runtime = settings or get_settings()
    api_key = _secret_value(runtime.finnhub_api_key)
    if not api_key:
        logger.warning("Finnhub disabled for this request: FINNHUB_API_KEY is missing")
        return []

    try:
        response = (http_get or requests.get)(
            "https://finnhub.io/api/v1/news",
            params={"category": category},
            headers={"X-Finnhub-Token": api_key},
            timeout=runtime.http_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise TypeError("unexpected response shape")
        return [
            _normalise_finnhub_article(item)
            for item in payload[:30]
            if isinstance(item, Mapping)
        ]
    except Exception as exc:  # noqa: BLE001 - provider boundary must degrade
        _log_provider_failure("Finnhub", "general news", exc)
        return []


def fetch_newsapi(
    query: str,
    lookback_hours: int = 12,
    language: str = "en",
    page_size: int = 20,
    *,
    settings: Settings | None = None,
    http_get: HttpGet | None = None,
) -> list[dict[str, Any]]:
    """Fetch NewsAPI articles with header-based authentication."""

    query = _bounded_text(query, 120).strip()
    if not query:
        raise ValueError("query must not be empty")
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be greater than zero")
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be in [1, 100]")

    runtime = settings or get_settings()
    api_key = _secret_value(runtime.newsapi_api_key)
    if not api_key:
        logger.warning("NewsAPI disabled for this request: NEWSAPI_API_KEY is missing")
        return []

    now = datetime.now(UTC)
    params: dict[str, str | int] = {
        "q": query,
        "from": (now - timedelta(hours=lookback_hours)).isoformat(),
        "language": language,
        "sortBy": "publishedAt",
        "pageSize": page_size,
    }

    try:
        response = (http_get or requests.get)(
            "https://newsapi.org/v2/everything",
            params=params,
            headers={"X-Api-Key": api_key},
            timeout=runtime.http_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise TypeError("unexpected response shape")
        raw_articles = payload.get("articles", [])
        if not isinstance(raw_articles, list):
            raise TypeError("unexpected articles shape")
        return [
            _normalise_newsapi_article(item)
            for item in raw_articles[:MAX_PROVIDER_ARTICLES]
            if isinstance(item, Mapping)
        ]
    except Exception as exc:  # noqa: BLE001 - provider boundary must degrade
        # Deliberately exclude query, exception text and response URL: any of
        # those may contain secrets or untrusted provider text.
        _log_provider_failure("NewsAPI", "article search", exc)
        return []


def fetch_akshare_news() -> list[dict[str, Any]]:
    """Fetch a bounded batch of Eastmoney headlines through AKShare."""

    try:
        # AKShare is optional at import time and relatively expensive to load.
        import akshare as ak

        frame = ak.stock_info_global_em()
        articles: list[dict[str, Any]] = []
        for _, row in frame.head(40).iterrows():
            published_at = _normalise_datetime(
                row.get("发布时间", ""), assume_timezone=ZoneInfo("Asia/Shanghai")
            )
            articles.append(
                {
                    "title": _bounded_text(row.get("标题", ""), TITLE_MAX_CHARS),
                    "summary": _bounded_text(
                        row.get("内容", row.get("标题", "")), SUMMARY_MAX_CHARS
                    ),
                    "source": "东方财富",
                    "url": _bounded_text(row.get("链接", ""), URL_MAX_CHARS),
                    "datetime": published_at,
                    "origin": "akshare",
                }
            )
        return articles
    except Exception as exc:  # noqa: BLE001 - optional provider boundary
        _log_provider_failure("AKShare", "global news", exc)
        return []


def fetch_all_news(
    tickers: Iterable[str],
    macro_queries: Iterable[str],
    config: NewsSourcesConfig | NewsConfig | Mapping[str, Any] | None = None,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Fetch enabled sources in stable order and de-duplicate by title."""

    source_config = _coerce_sources_config(config)
    runtime = settings or get_settings()
    all_articles: list[dict[str, Any]] = []

    if source_config.finnhub.enabled:
        for ticker in _stable_unique_strings(tickers):
            if ticker.isdigit():
                continue
            articles = fetch_finnhub_company_news(
                ticker,
                source_config.finnhub.lookback_hours,
                settings=runtime,
            )
            for article in articles:
                article["related_ticker"] = ticker
            all_articles.extend(articles)
        all_articles.extend(fetch_finnhub_general_news(settings=runtime))

    if source_config.newsapi.enabled:
        queries = _stable_unique_strings(
            [*macro_queries, *source_config.newsapi.extra_queries]
        )
        for query in queries:
            all_articles.extend(
                fetch_newsapi(
                    query,
                    source_config.newsapi.lookback_hours,
                    source_config.newsapi.language,
                    source_config.newsapi.page_size,
                    settings=runtime,
                )
            )

    if source_config.akshare.enabled:
        all_articles.extend(fetch_akshare_news())

    deduplicated: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for article in all_articles:
        title = _bounded_text(article.get("title", ""), TITLE_MAX_CHARS).strip()
        fingerprint = " ".join(title.casefold().split())
        if not fingerprint or fingerprint in seen_titles:
            continue
        seen_titles.add(fingerprint)
        article["title"] = title
        deduplicated.append(article)

    logger.info(
        "News fetch completed: %d unique articles from %d provider records",
        len(deduplicated),
        len(all_articles),
    )
    return deduplicated


def _coerce_sources_config(
    config: NewsSourcesConfig | NewsConfig | Mapping[str, Any] | None,
) -> NewsSourcesConfig:
    if config is None:
        return NewsSourcesConfig()
    if isinstance(config, NewsConfig):
        return config.sources
    if isinstance(config, NewsSourcesConfig):
        return config
    return NewsSourcesConfig.model_validate(config)


def _normalise_finnhub_article(article: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "title": _bounded_text(article.get("headline", ""), TITLE_MAX_CHARS),
        "summary": _bounded_text(article.get("summary", ""), SUMMARY_MAX_CHARS),
        "source": _bounded_text(article.get("source", "finnhub"), SOURCE_MAX_CHARS),
        "url": _bounded_text(article.get("url", ""), URL_MAX_CHARS),
        "datetime": _normalise_datetime(article.get("datetime")),
        "origin": "finnhub",
    }


def _normalise_newsapi_article(article: Mapping[str, Any]) -> dict[str, Any]:
    source = article.get("source")
    source_name = (
        source.get("name", "newsapi") if isinstance(source, Mapping) else "newsapi"
    )
    return {
        "title": _bounded_text(article.get("title", ""), TITLE_MAX_CHARS),
        "summary": _bounded_text(
            article.get("description", article.get("content", "")), SUMMARY_MAX_CHARS
        ),
        "source": _bounded_text(source_name, SOURCE_MAX_CHARS),
        "url": _bounded_text(article.get("url", ""), URL_MAX_CHARS),
        "datetime": _normalise_datetime(article.get("publishedAt")),
        "origin": "newsapi",
    }


def _normalise_datetime(
    value: Any,
    *,
    assume_timezone: timezone | ZoneInfo = UTC,
) -> str:
    """Return an aware UTC ISO timestamp, or an empty string if unparseable."""

    if value in (None, ""):
        return ""
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
        else:
            raw = _bounded_text(value, 80).strip()
            if raw.endswith("Z"):
                raw = f"{raw[:-1]}+00:00"
            parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=assume_timezone)
        return parsed.astimezone(UTC).isoformat()
    except (OverflowError, TypeError, ValueError):
        return ""


def _stable_unique_strings(values: Iterable[str]) -> list[str]:
    by_folded_value: dict[str, str] = {}
    for value in values:
        cleaned = _bounded_text(value, 120).strip()
        if cleaned:
            folded = cleaned.casefold()
            previous = by_folded_value.get(folded)
            if previous is None or cleaned < previous:
                by_folded_value[folded] = cleaned
    return sorted(by_folded_value.values(), key=lambda item: (item.casefold(), item))


def _bounded_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "")
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _secret_value(secret: Any) -> str:
    if secret is None:
        return ""
    getter = getattr(secret, "get_secret_value", None)
    value = getter() if callable(getter) else str(secret)
    return value.strip()


def _log_provider_failure(provider: str, operation: str, exc: Exception) -> None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    suffix = f", HTTP {status}" if isinstance(status, int) else ""
    logger.error(
        "%s %s failed (%s%s)",
        provider,
        operation,
        type(exc).__name__,
        suffix,
    )


__all__ = [
    "fetch_akshare_news",
    "fetch_all_news",
    "fetch_finnhub_company_news",
    "fetch_finnhub_general_news",
    "fetch_newsapi",
]
