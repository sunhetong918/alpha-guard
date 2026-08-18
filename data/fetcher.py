"""Fetch and normalize US/HK stock snapshots.

Quotes may come from market-specific providers, but fundamentals intentionally use
``yfinance.Ticker.info`` for both markets so that PE/PB/ROE units stay consistent.
Every returned snapshot carries basic provenance and turns unusable provider
numbers into ``None`` instead of silently substituting zero.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, TypeVar

from reliability import (
    CacheState,
    CircuitSnapshot,
    FreshnessContext,
    ProviderAttempt,
    ProviderKey,
    ProviderRuntime,
    ProviderUnavailableError,
    ReliabilityReport,
    evaluate_snapshot_reliability,
    gate_snapshot_for_decision,
)

try:  # Keep the pure normalization helpers importable in minimal test environments.
    import akshare as ak
except ModuleNotFoundError:  # pragma: no cover - exercised through _require_provider
    ak = None

try:
    import yfinance as yf
except ModuleNotFoundError:  # pragma: no cover - exercised through _require_provider
    yf = None

from data.futu_provider import FutuQuote


_MISSING_STRINGS = {"", "-", "--", "n/a", "na", "nan", "none", "null"}
_T = TypeVar("_T")


def _validated_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timeout_seconds must be a finite number") from exc
    if not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("timeout_seconds must be in (0, 120]")
    return timeout


def _provider_operation(
    key: ProviderKey,
    call: Callable[[], _T],
    *,
    runtime: ProviderRuntime | None,
    cache_identity: str,
) -> tuple[_T, str, CacheState, tuple[ProviderAttempt, ...]]:
    """Capture the true observation time inside the cacheable operation."""

    def observed_call() -> tuple[_T, str]:
        value = call()
        return value, _utc_now_iso()

    if runtime is None:
        value, observed_at = observed_call()
        return value, observed_at, CacheState.NONE, ()
    result = runtime.execute(
        key,
        observed_call,
        idempotent=True,
        cache_identity=cache_identity,
    )
    value, observed_at = result.value
    return value, observed_at, result.cache_state, result.attempts


def _to_finite_float(value: Any) -> float | None:
    """Convert common provider scalar formats to a finite float.

    Thousands separators and accounting parentheses are accepted. A percent sign
    is stripped while preserving percentage units (``"12.5%"`` becomes ``12.5``).
    Booleans are rejected because treating ``False`` as a market value of zero can
    mask provider/schema failures.
    """

    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, str):
        text = value.strip()
        if text.lower() in _MISSING_STRINGS:
            return None
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1]
        text = text.replace(",", "").replace("%", "").strip()
        try:
            number = float(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if negative:
            number = -number
    else:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None

    return number if math.isfinite(number) else None


def _add_issue(issues: list[str], issue: str) -> None:
    if issue not in issues:
        issues.append(issue)


def _normalize_number(
    value: Any,
    field: str,
    issues: list[str],
    *,
    positive: bool = False,
) -> float | None:
    number = _to_finite_float(value)
    if number is None:
        _add_issue(issues, f"{field}:missing_or_non_finite")
        return None
    if positive and number <= 0:
        _add_issue(issues, f"{field}:non_positive")
        return None
    return number


def _first_number(
    values: list[Any],
    field: str,
    issues: list[str],
    *,
    positive: bool = False,
) -> float | None:
    for value in values:
        number = _to_finite_float(value)
        if number is not None and (not positive or number > 0):
            return number
    return _normalize_number(None, field, issues, positive=positive)


def _require_provider(provider: Any, package: str) -> Any:
    if provider is None:
        raise RuntimeError(
            f"{package} is required for market data; install project dependencies"
        )
    return provider


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _as_of_from_info(info: Mapping[str, Any]) -> str | None:
    timestamp = _to_finite_float(info.get("regularMarketTime"))
    if timestamp is None or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _history_as_of(hist: Any) -> str | None:
    try:
        index = hist.index
        if len(index) == 0:
            return None
        value = index[-1]
    except (AttributeError, IndexError, KeyError, TypeError):
        return None

    if hasattr(value, "to_pydatetime"):
        try:
            value = value.to_pydatetime()
        except (TypeError, ValueError):
            pass
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001 - provider objects may fail in arbitrary ways
        return None
    return text or None


def _history_extreme(hist: Any, column: str, *, highest: bool) -> float | None:
    try:
        values = hist[column]
    except (KeyError, TypeError, AttributeError):
        return None

    try:
        iterator = values.tolist() if hasattr(values, "tolist") else values
        finite = [
            number
            for value in iterator
            if (number := _to_finite_float(value)) is not None and number > 0
        ]
    except (TypeError, ValueError):
        return None
    if not finite:
        return None
    return max(finite) if highest else min(finite)


def _fetch_info(
    ticker_obj: Any,
    *,
    symbol: str,
    market: str,
    runtime: ProviderRuntime | None,
) -> tuple[dict[str, Any], str, CacheState, tuple[ProviderAttempt, ...]]:
    key = ProviderKey(provider="yfinance", operation="info", market=market)
    try:
        info, observed_at, cache_state, attempts = _provider_operation(
            key,
            lambda: dict(ticker_obj.info or {}),
            runtime=runtime,
            cache_identity=symbol,
        )
    except Exception as exc:  # noqa: BLE001 - normalize provider boundary failures
        if isinstance(exc, ProviderUnavailableError):
            raise
        raise RuntimeError(
            f"yfinance info unavailable for {symbol}: {type(exc).__name__}"
        ) from exc
    return info, observed_at, cache_state, attempts


def _safe_history(
    ticker_obj: Any,
    issues: list[str],
    *,
    symbol: str,
    market: str,
    runtime: ProviderRuntime | None,
    timeout_seconds: float,
) -> tuple[Any, str | None, CacheState, tuple[ProviderAttempt, ...]]:
    key = ProviderKey(provider="yfinance", operation="history", market=market)
    try:
        history, observed_at, cache_state, attempts = _provider_operation(
            key,
            lambda: ticker_obj.history(period="1y", timeout=timeout_seconds),
            runtime=runtime,
            cache_identity=symbol,
        )
        return history, observed_at, cache_state, attempts
    except Exception as exc:  # noqa: BLE001 - optional provider capability
        failure_name: str
        if isinstance(exc, ProviderUnavailableError):
            attempts = exc.attempts
            failure_name = attempts[-1].failure_class if attempts else "unknown"
        else:
            attempts = ()
            failure_name = type(exc).__name__
        _add_issue(issues, f"history:provider_error:{failure_name}")
        return None, None, CacheState.NONE, attempts


def _history_is_empty(hist: Any) -> bool:
    if hist is None:
        return True
    try:
        return bool(hist.empty)
    except (AttributeError, TypeError, ValueError):
        try:
            return len(hist) == 0
        except (TypeError, AttributeError):
            return False


def _calc_roe(info: Mapping[str, Any]) -> float | None:
    """Return ROE in percentage points for every yfinance code path.

    ``returnOnEquity`` is documented by yfinance/Yahoo as a ratio, while the
    scorer and rule engine consume percentage points. Thus ``0.237`` is normalized
    to ``23.7``. The accounting fallback uses the same percentage convention.
    """

    roe: float | None
    net_income = _to_finite_float(info.get("netIncomeToCommon"))
    book_value_per_share = _to_finite_float(info.get("bookValue"))
    shares = _to_finite_float(info.get("sharesOutstanding"))
    if (
        net_income is not None
        and book_value_per_share is not None
        and shares is not None
        and book_value_per_share > 0
        and shares > 0
    ):
        total_equity = book_value_per_share * shares
        roe = net_income / total_equity * 100
        return round(roe, 2) if math.isfinite(roe) else None

    raw_roe = info.get("returnOnEquity")
    if isinstance(raw_roe, str) and raw_roe.strip().endswith("%"):
        roe = _to_finite_float(raw_roe)
    else:
        ratio = _to_finite_float(raw_roe)
        roe = ratio * 100 if ratio is not None else None
    return round(roe, 2) if roe is not None and math.isfinite(roe) else None


def _base_yfinance_fields(
    info: Mapping[str, Any], hist: Any, issues: list[str]
) -> dict[str, Any]:
    price = _first_number(
        [info.get("currentPrice"), info.get("regularMarketPrice")],
        "price",
        issues,
        positive=True,
    )
    high = _first_number(
        [info.get("fiftyTwoWeekHigh"), _history_extreme(hist, "High", highest=True)],
        "52w_high",
        issues,
        positive=True,
    )
    low = _first_number(
        [info.get("fiftyTwoWeekLow"), _history_extreme(hist, "Low", highest=False)],
        "52w_low",
        issues,
        positive=True,
    )
    roe = _calc_roe(info)
    if roe is None:
        _add_issue(issues, "roe:missing_or_non_finite")

    fields = {
        "price": price,
        "pe_ttm": _normalize_number(info.get("trailingPE"), "pe_ttm", issues),
        "pb": _normalize_number(info.get("priceToBook"), "pb", issues),
        "roe": roe,
        "market_cap": _normalize_number(
            info.get("marketCap"), "market_cap", issues, positive=True
        ),
        "52w_high": high,
        "52w_low": low,
        "dividend_yield": _normalize_number(
            info.get("dividendYield"), "dividend_yield", issues
        ),
        "debt_to_equity": _normalize_number(
            info.get("debtToEquity"), "debt_to_equity", issues
        ),
        "free_cashflow": _normalize_number(
            info.get("freeCashflow"), "free_cashflow", issues
        ),
        "revenue_growth": _normalize_number(
            info.get("revenueGrowth"), "revenue_growth", issues
        ),
        "earnings_growth": _normalize_number(
            info.get("earningsGrowth"), "earnings_growth", issues
        ),
        "hist": hist,
    }

    if fields["pe_ttm"] is not None and fields["pe_ttm"] <= 0:
        _add_issue(issues, "pe_ttm:non_positive")
    if fields["pb"] is not None and fields["pb"] <= 0:
        _add_issue(issues, "pb:non_positive")
    if _history_is_empty(hist):
        _add_issue(issues, "history:missing_or_empty")
    return fields


def _field_sources(provider: str) -> dict[str, str]:
    return {
        field: provider
        for field in (
            "name",
            "price",
            "pe_ttm",
            "pb",
            "roe",
            "market_cap",
            "52w_high",
            "52w_low",
            "dividend_yield",
            "debt_to_equity",
            "free_cashflow",
            "revenue_growth",
            "earnings_growth",
            "hist",
        )
    }


def _core_field_metadata(
    sources: Mapping[str, str],
    *,
    price_source_as_of: str | None,
    price_observed_at: str,
    fundamentals_observed_at: str,
    price_cache_state: CacheState,
    fundamentals_cache_state: CacheState,
) -> dict[str, dict[str, Any]]:
    price_basis = "source_event" if price_source_as_of else "observed_only"
    metadata: dict[str, dict[str, Any]] = {
        "price": {
            "provider": sources["price"],
            "source_as_of": price_source_as_of,
            "observed_at": price_observed_at,
            "time_basis": price_basis,
            "timestamp_confidence": (
                "provider_event" if price_source_as_of else "observed_only"
            ),
            "cache_state": price_cache_state.value,
        }
    }
    for field in ("pe_ttm", "pb", "roe"):
        metadata[field] = {
            "provider": sources[field],
            "source_as_of": None,
            "observed_at": fundamentals_observed_at,
            "time_basis": "observed_only",
            "timestamp_confidence": "observed_only",
            "cache_state": fundamentals_cache_state.value,
        }
    return metadata


def get_us_stock(
    ticker: str,
    *,
    provider_runtime: ProviderRuntime | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Fetch a normalized US stock snapshot, e.g. ``AAPL``."""

    yfinance = _require_provider(yf, "yfinance")
    symbol = str(ticker).strip().upper()
    if not symbol:
        raise ValueError("US ticker cannot be empty")
    timeout = _validated_timeout(timeout_seconds)

    issues: list[str] = []
    provider_attempts: list[ProviderAttempt] = []
    ticker_obj = yfinance.Ticker(symbol)
    info, info_observed_at, info_cache, info_attempts = _fetch_info(
        ticker_obj,
        symbol=symbol,
        market="US",
        runtime=provider_runtime,
    )
    provider_attempts.extend(info_attempts)
    hist, _history_observed_at, _history_cache, history_attempts = _safe_history(
        ticker_obj,
        issues,
        symbol=symbol,
        market="US",
        runtime=provider_runtime,
        timeout_seconds=timeout,
    )
    provider_attempts.extend(history_attempts)
    fields = _base_yfinance_fields(info, hist, issues)
    retrieved_at = _utc_now_iso()
    sources = _field_sources("yfinance")
    price_as_of = _as_of_from_info(info)
    field_metadata = _core_field_metadata(
        sources,
        price_source_as_of=price_as_of,
        price_observed_at=info_observed_at,
        fundamentals_observed_at=info_observed_at,
        price_cache_state=info_cache,
        fundamentals_cache_state=info_cache,
    )

    return {
        "ticker": symbol,
        "symbol": symbol,
        "market": "US",
        "name": str(info.get("longName") or info.get("shortName") or symbol),
        **fields,
        "provider": "yfinance",
        "source": sources,
        "sources": dict(sources),
        "field_metadata": field_metadata,
        "provider_attempts": [
            attempt.model_dump(mode="json") for attempt in provider_attempts
        ],
        "retrieved_at": retrieved_at,
        "as_of": price_as_of,
        "currency": str(info.get("currency") or "USD").upper(),
        "quality_issues": issues,
    }


def canonical_hk_symbol(ticker: str) -> str:
    """Map a HKEX code to Yahoo's canonical symbol (``00700`` -> ``0700.HK``)."""

    raw = str(ticker).strip().upper().removesuffix(".HK")
    if not raw.isdigit():
        raise ValueError(f"Invalid HK ticker: {ticker!r}")
    value = int(raw)
    if value <= 0 or value > 99999:
        raise ValueError(f"Invalid HK ticker: {ticker!r}")
    return f"{value:04d}.HK"


def _canonical_hk_code(ticker: str) -> str:
    symbol = canonical_hk_symbol(ticker)
    return f"{int(symbol[:-3]):05d}"


def _records(frame: Any) -> list[Mapping[str, Any]]:
    if frame is None:
        return []
    if isinstance(frame, list):
        return [row for row in frame if isinstance(row, Mapping)]
    try:
        rows = frame.to_dict("records")
    except (AttributeError, TypeError, ValueError):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _find_hk_quote(
    ticker: str,
    issues: list[str],
    *,
    runtime: ProviderRuntime | None,
    attempts: list[ProviderAttempt],
) -> tuple[Mapping[str, Any] | None, str | None, CacheState]:
    if ak is None:
        _add_issue(issues, "akshare:provider_unavailable")
        return None, None, CacheState.NONE
    key = ProviderKey(provider="akshare", operation="hk_quote", market="HK")
    try:
        frame, observed_at, cache_state, operation_attempts = _provider_operation(
            key,
            ak.stock_hk_spot_em,
            runtime=runtime,
            cache_identity="all_hk_quotes",
        )
        attempts.extend(operation_attempts)
        rows = _records(frame)
    except Exception as exc:  # noqa: BLE001 - normalize provider boundary failures
        failure_name: str
        if isinstance(exc, ProviderUnavailableError):
            attempts.extend(exc.attempts)
            failure_name = (
                exc.attempts[-1].failure_class if exc.attempts else "unknown"
            )
        else:
            failure_name = type(exc).__name__
        _add_issue(issues, f"akshare:provider_error:{failure_name}")
        return None, None, CacheState.NONE

    wanted = _canonical_hk_code(ticker)
    for row in rows:
        code = row.get("代码")
        try:
            if _canonical_hk_code(str(code)) == wanted:
                return row, observed_at, cache_state
        except ValueError:
            continue
    _add_issue(issues, "akshare:quote_not_found")
    return None, observed_at, cache_state


def _ak_as_of(row: Mapping[str, Any] | None) -> str | None:
    if not row:
        return None
    for key in ("更新时间", "时间", "日期"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _find_futu_quote(
    local_code: str,
    issues: list[str],
    *,
    runtime: ProviderRuntime | None,
    attempts: list[ProviderAttempt],
) -> "FutuQuote | None":
    """Try the opt-in Futu realtime source; never raises, records issues."""

    from data import futu_provider

    if not futu_provider.quote_available():
        return None
    from config import get_settings

    settings = get_settings()
    quote, futu_attempts = futu_provider.fetch_hk_quote(
        local_code,
        host=settings.futu_opend_host,
        port=settings.futu_opend_quote_port,
        runtime=runtime,
    )
    attempts.extend(futu_attempts)
    if quote is None:
        _add_issue(issues, "futu:quote_not_available")
    return quote


def get_hk_stock(
    ticker: str,
    *,
    provider_runtime: ProviderRuntime | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Fetch a normalized HK stock snapshot.

    Futu (opt-in, via OpenD) is the preferred realtime price source; AKShare
    remains the offline-safe fallback. Fundamentals and the one-year history
    always come from the canonical yfinance symbol.
    """

    yfinance = _require_provider(yf, "yfinance")
    local_code = _canonical_hk_code(ticker)
    yf_symbol = canonical_hk_symbol(ticker)
    timeout = _validated_timeout(timeout_seconds)
    issues: list[str] = []
    provider_attempts: list[ProviderAttempt] = []

    ticker_obj = yfinance.Ticker(yf_symbol)
    info, info_observed_at, info_cache, info_attempts = _fetch_info(
        ticker_obj,
        symbol=yf_symbol,
        market="HK",
        runtime=provider_runtime,
    )
    provider_attempts.extend(info_attempts)
    hist, _history_observed_at, _history_cache, history_attempts = _safe_history(
        ticker_obj,
        issues,
        symbol=yf_symbol,
        market="HK",
        runtime=provider_runtime,
        timeout_seconds=timeout,
    )
    provider_attempts.extend(history_attempts)
    fields = _base_yfinance_fields(info, hist, issues)

    futu_quote = _find_futu_quote(
        local_code,
        issues,
        runtime=provider_runtime,
        attempts=provider_attempts,
    )

    if futu_quote is not None:
        fields["price"] = futu_quote.price
        price_source = "futu"
        price_as_of = futu_quote.source_as_of
        price_observed_at = futu_quote.observed_at
        price_cache = CacheState.NONE
        issues[:] = [issue for issue in issues if not issue.startswith("price:")]
        futu_name = futu_quote.name
    else:
        futu_name = ""

    if futu_quote is None:
        quote, quote_observed_at, quote_cache = _find_hk_quote(
            local_code,
            issues,
            runtime=provider_runtime,
            attempts=provider_attempts,
        )
        ak_price = _to_finite_float(quote.get("最新价")) if quote else None
        if ak_price is not None and ak_price > 0:
            fields["price"] = ak_price
            price_source = "akshare"
            # Remove a yfinance price issue when AKShare supplied a usable quote.
            issues[:] = [issue for issue in issues if not issue.startswith("price:")]
        else:
            price_source = "yfinance"
            if quote and ak_price is not None and ak_price <= 0:
                _add_issue(issues, "akshare_price:non_positive")
            elif quote:
                _add_issue(issues, "akshare_price:missing_or_non_finite")

        ak_name = str(quote.get("名称") or "").strip() if quote else ""
        price_as_of = (
            _ak_as_of(quote)
            if price_source == "akshare"
            else _as_of_from_info(info)
        )
        price_observed_at = (
            quote_observed_at
            if price_source == "akshare" and quote_observed_at is not None
            else info_observed_at
        )
        price_cache = quote_cache if price_source == "akshare" else info_cache
    else:
        ak_name = ""

    name = (
        futu_name
        or ak_name
        or str(info.get("longName") or info.get("shortName") or local_code)
    )
    name_source = (
        "futu"
        if futu_name
        else ("akshare" if ak_name else "yfinance")
    )
    sources = _field_sources("yfinance")
    sources.update({"name": name_source, "price": price_source})
    if futu_quote is not None:
        provider = "futu+yfinance"
    else:
        provider = "akshare+yfinance" if price_source == "akshare" else "yfinance"
    retrieved_at = _utc_now_iso()
    field_metadata = _core_field_metadata(
        sources,
        price_source_as_of=price_as_of,
        price_observed_at=price_observed_at,
        fundamentals_observed_at=info_observed_at,
        price_cache_state=price_cache,
        fundamentals_cache_state=info_cache,
    )

    return {
        "ticker": local_code,
        "symbol": yf_symbol,
        "market": "HK",
        "name": name,
        **fields,
        "provider": provider,
        "source": sources,
        "sources": dict(sources),
        "field_metadata": field_metadata,
        "provider_attempts": [
            attempt.model_dump(mode="json") for attempt in provider_attempts
        ],
        "retrieved_at": retrieved_at,
        "as_of": price_as_of,
        "currency": str(info.get("currency") or "HKD").upper(),
        "quality_issues": issues,
    }


def _snapshot_attempts(snapshot: Mapping[str, Any]) -> tuple[ProviderAttempt, ...]:
    raw = snapshot.get("provider_attempts") or ()
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, Mapping)):
        return ()
    attempts: list[ProviderAttempt] = []
    for item in raw:
        try:
            attempts.append(ProviderAttempt.model_validate(item))
        except (TypeError, ValueError):
            continue
    return tuple(attempts)


def _blind_provider_report(
    *,
    ticker: str,
    market: str,
    required_fields: tuple[str, ...],
    freshness_policies: Mapping[str, Any],
    freshness_context: FreshnessContext,
    future_tolerance_seconds: float,
    attempts: tuple[ProviderAttempt, ...],
) -> ReliabilityReport:
    placeholder: dict[str, Any] = {
        "ticker": ticker,
        "market": market,
        "field_metadata": {},
        **dict.fromkeys(required_fields),
    }
    return evaluate_snapshot_reliability(
        placeholder,
        required_fields,
        freshness_policies,
        freshness_context,
        future_tolerance_seconds=future_tolerance_seconds,
        provider_attempts=attempts,
    )


def get_stock(
    ticker: str,
    market: str = "auto",
    *,
    required_fields: Iterable[str] | None = None,
    freshness_policies: Mapping[str, Any] | None = None,
    freshness_context: FreshnessContext | None = None,
    future_tolerance_seconds: float = 300.0,
    provider_runtime: ProviderRuntime | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Unified market-data entry point.

    ``market`` accepts ``US``, ``HK`` or ``auto``. Numeric/HK-suffixed symbols are
    detected as Hong Kong listings; unsupported market values fail explicitly.
    """

    reliability_args = (
        required_fields is not None,
        freshness_policies is not None,
        freshness_context is not None,
    )
    if any(reliability_args) and not all(reliability_args):
        raise ValueError(
            "required_fields, freshness_policies and freshness_context "
            "must be provided together"
        )
    required = tuple(sorted(set(required_fields or ())))
    market_name = str(market).strip().upper()
    ticker_text = str(ticker).strip()
    if market_name == "AUTO":
        upper_ticker = ticker_text.upper()
        market_name = (
            "HK" if ticker_text.isdigit() or upper_ticker.endswith(".HK") else "US"
        )
    if market_name not in {"HK", "US"}:
        raise ValueError(f"Unsupported market: {market!r}")

    canonical_ticker = (
        _canonical_hk_code(ticker_text)
        if market_name == "HK"
        else ticker_text.upper()
    )
    try:
        if market_name == "HK":
            snapshot = get_hk_stock(
                ticker_text,
                provider_runtime=provider_runtime,
                timeout_seconds=timeout_seconds,
            )
        else:
            snapshot = get_us_stock(
                ticker_text,
                provider_runtime=provider_runtime,
                timeout_seconds=timeout_seconds,
            )
    except ProviderUnavailableError as exc:
        if not all(reliability_args):
            raise
        assert freshness_policies is not None
        assert freshness_context is not None
        report = _blind_provider_report(
            ticker=canonical_ticker,
            market=market_name,
            required_fields=required,
            freshness_policies=freshness_policies,
            freshness_context=freshness_context,
            future_tolerance_seconds=future_tolerance_seconds,
            attempts=exc.attempts,
        )
        raise ProviderUnavailableError(
            exc.key, exc.attempts, exc.circuit, report=report
        ) from exc
    except RuntimeError as exc:
        if not all(reliability_args):
            raise
        assert freshness_policies is not None
        assert freshness_context is not None
        key = ProviderKey(
            provider="yfinance", operation="info", market=market_name
        )
        report = _blind_provider_report(
            ticker=canonical_ticker,
            market=market_name,
            required_fields=required,
            freshness_policies=freshness_policies,
            freshness_context=freshness_context,
            future_tolerance_seconds=future_tolerance_seconds,
            attempts=(),
        )
        circuit = (
            provider_runtime.circuit_for(key)
            if provider_runtime is not None
            else CircuitSnapshot()
        )
        raise ProviderUnavailableError(key, (), circuit, report=report) from exc

    if not all(reliability_args):
        return snapshot
    assert freshness_policies is not None
    assert freshness_context is not None
    attempts = _snapshot_attempts(snapshot)
    report = evaluate_snapshot_reliability(
        snapshot,
        required,
        freshness_policies,
        freshness_context,
        future_tolerance_seconds=future_tolerance_seconds,
        provider_attempts=attempts,
    )
    hard_no_data = required and (
        all(snapshot.get(field) is None for field in required)
        or ("price" in required and snapshot.get("price") is None)
    )
    if hard_no_data:
        key = ProviderKey(
            provider="yfinance", operation="info", market=market_name
        )
        circuit = (
            provider_runtime.circuit_for(key)
            if provider_runtime is not None
            else CircuitSnapshot()
        )
        raise ProviderUnavailableError(key, attempts, circuit, report=report)
    return gate_snapshot_for_decision(snapshot, report, required)
