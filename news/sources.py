"""Bounded and deterministic adapters for public news providers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from functools import partial
from typing import Any, Literal
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


@dataclass(frozen=True)
class ProviderFetchResult:
    """One bounded request, with no raw provider error or credential text."""

    provider: str
    operation: str
    status: Literal["disabled", "success", "failed"]
    articles: list[dict[str, Any]]
    error_code: str | None = None

    def to_summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "operation": self.operation,
            "status": self.status,
            "article_count": len(self.articles),
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class NewsFetchReport:
    """Distinguish an empty successful fetch from disabled or failed sources."""

    articles: list[dict[str, Any]]
    sources: tuple[ProviderFetchResult, ...]

    @property
    def status(self) -> Literal["disabled", "success", "partial", "failed"]:
        attempted = [source for source in self.sources if source.status != "disabled"]
        if self.cancelled:
            return (
                "partial"
                if any(source.status == "success" for source in attempted)
                else "failed"
            )
        if not attempted:
            return "disabled"
        succeeded = sum(source.status == "success" for source in attempted)
        if succeeded == len(attempted):
            return "success"
        return "partial" if succeeded else "failed"

    @property
    def failed_sources(self) -> tuple[ProviderFetchResult, ...]:
        return tuple(source for source in self.sources if source.status == "failed")

    @property
    def cancelled(self) -> bool:
        return any(source.error_code == "cancelled" for source in self.sources)

    def to_summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "fetched": len(self.articles),
            "attempted": sum(source.status != "disabled" for source in self.sources),
            "succeeded": sum(source.status == "success" for source in self.sources),
            "failed": len(self.failed_sources),
            "cancelled": self.cancelled,
            "sources": [source.to_summary() for source in self.sources],
        }


def _fetch_finnhub_company_news_result(
    ticker: str,
    lookback_hours: int = 6,
    *,
    settings: Settings | None = None,
    http_get: HttpGet | None = None,
) -> ProviderFetchResult:
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
        return _failure("finnhub", "company_news", "missing_credentials")

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
        if not isinstance(payload, list) or not all(
            isinstance(item, Mapping) for item in payload[:20]
        ):
            raise TypeError("unexpected response shape")
        articles = [_normalise_finnhub_article(item) for item in payload[:20]]
        return ProviderFetchResult("finnhub", "company_news", "success", articles)
    except Exception as exc:  # noqa: BLE001 - provider boundary must degrade
        _log_provider_failure("Finnhub", "company news", exc)
        return _failure("finnhub", "company_news", _provider_error_code(exc))


def _fetch_finnhub_general_news_result(
    category: str = "general",
    *,
    settings: Settings | None = None,
    http_get: HttpGet | None = None,
) -> ProviderFetchResult:
    """Fetch a bounded Finnhub market-news category."""

    if category not in FINNHUB_GENERAL_CATEGORIES:
        allowed = ", ".join(sorted(FINNHUB_GENERAL_CATEGORIES))
        raise ValueError(f"unsupported Finnhub category; expected one of: {allowed}")

    runtime = settings or get_settings()
    api_key = _secret_value(runtime.finnhub_api_key)
    if not api_key:
        logger.warning("Finnhub disabled for this request: FINNHUB_API_KEY is missing")
        return _failure("finnhub", "general_news", "missing_credentials")

    try:
        response = (http_get or requests.get)(
            "https://finnhub.io/api/v1/news",
            params={"category": category},
            headers={"X-Finnhub-Token": api_key},
            timeout=runtime.http_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not all(
            isinstance(item, Mapping) for item in payload[:30]
        ):
            raise TypeError("unexpected response shape")
        articles = [_normalise_finnhub_article(item) for item in payload[:30]]
        return ProviderFetchResult("finnhub", "general_news", "success", articles)
    except Exception as exc:  # noqa: BLE001 - provider boundary must degrade
        _log_provider_failure("Finnhub", "general news", exc)
        return _failure("finnhub", "general_news", _provider_error_code(exc))


def _fetch_newsapi_result(
    query: str,
    lookback_hours: int = 12,
    language: str = "en",
    page_size: int = 20,
    *,
    settings: Settings | None = None,
    http_get: HttpGet | None = None,
) -> ProviderFetchResult:
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
        return _failure("newsapi", "article_search", "missing_credentials")

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
        if payload.get("status") == "error":
            return _failure("newsapi", "article_search", "provider_error")
        raw_articles = payload.get("articles")
        if not isinstance(raw_articles, list) or not all(
            isinstance(item, Mapping) for item in raw_articles[:MAX_PROVIDER_ARTICLES]
        ):
            raise TypeError("unexpected articles shape")
        articles = [
            _normalise_newsapi_article(item)
            for item in raw_articles[:MAX_PROVIDER_ARTICLES]
        ]
        return ProviderFetchResult("newsapi", "article_search", "success", articles)
    except Exception as exc:  # noqa: BLE001 - provider boundary must degrade
        # Deliberately exclude query, exception text and response URL: any of
        # those may contain secrets or untrusted provider text.
        _log_provider_failure("NewsAPI", "article search", exc)
        return _failure("newsapi", "article_search", _provider_error_code(exc))


def _fetch_akshare_news_result(
    *, settings: Settings | None = None, fetch: Callable[..., Any] | None = None,
) -> ProviderFetchResult:
    """Fetch Eastmoney's public endpoint with explicit byte/time bounds.

    AKShare's stock_info_global_em uses this endpoint but omits a timeout.
    Keep its normalized provider identity for backwards-compatible source config.
    """
    import json
    from research.network import ContentAccessError, safe_fetch

    runtime = settings or get_settings()
    try:
        response = (fetch or safe_fetch)(
            "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
            "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=40",
            timeout=min(runtime.http_timeout_seconds, 60), max_bytes=2_000_000,
        )
        payload = json.loads(response.text)
        rows = payload.get("data", {}).get("fastNewsList")
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows[:40]):
            raise TypeError("unexpected response shape")
        articles: list[dict[str, Any]] = []
        for row in rows[:40]:
            code = str(row.get("code", ""))
            articles.append({
                "title": _bounded_text(row.get("title", ""), TITLE_MAX_CHARS),
                "summary": _bounded_text(row.get("summary", ""), SUMMARY_MAX_CHARS),
                "source": "东方财富",
                "url": f"https://finance.eastmoney.com/a/{code}.html" if code.isdigit() else "",
                "datetime": _normalise_datetime(row.get("showTime", ""), assume_timezone=ZoneInfo("Asia/Shanghai")),
                "origin": "akshare",
            })
        return ProviderFetchResult("akshare", "global_news", "success", articles)
    except Exception as exc:  # noqa: BLE001 - provider boundary
        _log_provider_failure("Eastmoney", "global news", exc)
        error_code = str(exc) if isinstance(exc, ContentAccessError) else _provider_error_code(exc)
        return _failure("akshare", "global_news", error_code)


def fetch_finnhub_company_news(
    ticker: str,
    lookback_hours: int = 6,
    *,
    settings: Settings | None = None,
    http_get: HttpGet | None = None,
) -> list[dict[str, Any]]:
    """Compatibility API; use the aggregate report for collection health."""

    return _fetch_finnhub_company_news_result(
        ticker, lookback_hours, settings=settings, http_get=http_get
    ).articles


def fetch_finnhub_general_news(
    category: str = "general",
    *,
    settings: Settings | None = None,
    http_get: HttpGet | None = None,
) -> list[dict[str, Any]]:
    return _fetch_finnhub_general_news_result(
        category, settings=settings, http_get=http_get
    ).articles


def fetch_newsapi(
    query: str,
    lookback_hours: int = 12,
    language: str = "en",
    page_size: int = 20,
    *,
    settings: Settings | None = None,
    http_get: HttpGet | None = None,
) -> list[dict[str, Any]]:
    return _fetch_newsapi_result(
        query,
        lookback_hours,
        language,
        page_size,
        settings=settings,
        http_get=http_get,
    ).articles


def fetch_akshare_news() -> list[dict[str, Any]]:
    return _fetch_akshare_news_result().articles


def fetch_all_news(
    tickers: Iterable[str],
    macro_queries: Iterable[str],
    config: NewsSourcesConfig | NewsConfig | Mapping[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Legacy list API. New callers should inspect ``fetch_all_news_report``."""

    return fetch_all_news_report(
        tickers,
        macro_queries,
        config,
        settings=settings,
        should_continue=should_continue,
    ).articles


def fetch_all_news_report(
    tickers: Iterable[str],
    macro_queries: Iterable[str],
    config: NewsSourcesConfig | NewsConfig | Mapping[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> NewsFetchReport:
    """Fetch in stable order while retaining health of every provider request.

    Missing credentials on an enabled source are failures. A successful empty
    response stays successful. Cancellation is checked before each request;
    the request already in progress remains bounded by its provider timeout.
    """

    source_config = _coerce_sources_config(config)
    runtime = settings or get_settings()
    all_articles: list[dict[str, Any]] = []
    results: list[ProviderFetchResult] = []

    def collect(
        provider: str,
        operation: str,
        request: Callable[[], ProviderFetchResult],
    ) -> ProviderFetchResult:
        if should_continue is not None and not should_continue():
            result = ProviderFetchResult(
                provider, operation, "disabled", [], "cancelled"
            )
        else:
            result = request()
        results.append(result)
        all_articles.extend(result.articles)
        return result

    if source_config.finnhub.enabled:
        for ticker in _stable_unique_strings(tickers):
            if ticker.isdigit():
                continue
            result = collect(
                "finnhub",
                "company_news",
                partial(
                    _fetch_finnhub_company_news_result,
                    ticker,
                    source_config.finnhub.lookback_hours,
                    settings=runtime,
                ),
            )
            for article in result.articles:
                article["related_ticker"] = ticker
        collect(
            "finnhub",
            "general_news",
            lambda: _fetch_finnhub_general_news_result(settings=runtime),
        )
    else:
        results.append(ProviderFetchResult("finnhub", "all", "disabled", []))

    if source_config.newsapi.enabled:
        queries = _stable_unique_strings(
            [*macro_queries, *source_config.newsapi.extra_queries]
        )
        if not queries:
            results.append(
                ProviderFetchResult(
                    "newsapi", "article_search", "disabled", [], "no_queries"
                )
            )
        for query in queries:
            collect(
                "newsapi",
                "article_search",
                partial(
                    _fetch_newsapi_result,
                    query,
                    source_config.newsapi.lookback_hours,
                    source_config.newsapi.language,
                    source_config.newsapi.page_size,
                    settings=runtime,
                ),
            )
    else:
        results.append(ProviderFetchResult("newsapi", "all", "disabled", []))

    if source_config.akshare.enabled:
        collect("akshare", "global_news", partial(_fetch_akshare_news_result, settings=runtime))
    else:
        results.append(ProviderFetchResult("akshare", "all", "disabled", []))

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
    return NewsFetchReport(deduplicated, tuple(results))


def _failure(provider: str, operation: str, code: str) -> ProviderFetchResult:
    return ProviderFetchResult(provider, operation, "failed", [], code)


def _provider_error_code(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError):
        return "http_error"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_response"
    return "provider_error"


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
    "NewsFetchReport",
    "ProviderFetchResult",
    "fetch_akshare_news",
    "fetch_all_news",
    "fetch_all_news_report",
    "fetch_finnhub_company_news",
    "fetch_finnhub_general_news",
    "fetch_newsapi",
]
