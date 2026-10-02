"""Research-only US/HK/CN market contexts; no trading-engine expansion."""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
import math
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

from research.network import check_cancelled

PROFILE_VERSION = "research-markets-2026-09-30"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _datetime(value: Any) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def _cn_context(
    ticker: str, *, now: datetime, timeout_seconds: float, provider: Any = None,
    cancelled: Callable[[], bool] | None = None, history_fetcher: Callable | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"(?:60|68|00|30|43|83|87|88|92)\d{4}", ticker):
        raise ValueError("invalid_cn_ticker")
    local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    # Today's in-progress bar must never be treated as a completed daily close.
    end_date = local_now.date() if local_now.time() >= time(15, 10) else local_now.date() - timedelta(days=1)
    exchange = "SSE" if ticker.startswith("6") else "BSE" if ticker.startswith(("4", "8", "92")) else "SZSE"
    provider_name, volume_unit = "akshare_eastmoney_daily", "lots"
    issues = ["daily_data_not_intraday", "cn_short_term_not_validated", "fundamentals_unavailable"]
    runtime = _research_runtime(cancelled, timeout_seconds)
    from reliability import ProviderKey
    try:
        if provider is None:
            import akshare
            provider = akshare
        result = runtime.execute(
            ProviderKey(provider="akshare", operation="cn_daily", market="CN"),
            lambda: provider.stock_zh_a_hist(
                symbol=ticker, period="daily", adjust="",
                start_date=(end_date - timedelta(days=40)).strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"), timeout=timeout_seconds),
            idempotent=True,
        )
        frame = result.value
        if frame is None or frame.empty:
            raise ValueError("cn_daily_data_unavailable")
    except Exception:
        check_cancelled(cancelled)
        # A fallback is a separate public provider with its own provenance. It
        # does not turn daily bars into realtime quotes or claim BSE coverage.
        if exchange == "BSE":
            raise ValueError("cn_daily_data_unavailable") from None
        if history_fetcher is None:
            import yfinance
            symbol = ticker + (".SS" if exchange == "SSE" else ".SZ")
            def history_fetcher(**kwargs):
                return yfinance.Ticker(symbol).history(**kwargs)
        result = runtime.execute(
            ProviderKey(provider="yfinance", operation="cn_daily", market="CN"),
            lambda: history_fetcher(period="2mo", interval="1d", auto_adjust=False,
                                    timeout=timeout_seconds, raise_errors=True),
            idempotent=True,
        )
        history = result.value
        if history is None or history.empty:
            raise ValueError("cn_daily_data_unavailable")
        import pandas as pd
        rows = []
        for stamp, row in history.tail(60).iterrows():
            if hasattr(stamp, "to_pydatetime"):
                stamp = stamp.to_pydatetime()
            day = stamp.astimezone(ZoneInfo("Asia/Shanghai")).date() if stamp.tzinfo else stamp.date()
            rows.append({"日期": day.isoformat(), "收盘": row.get("Close"), "成交量": row.get("Volume")})
        frame = pd.DataFrame(rows)
        provider_name, volume_unit = "yfinance_cn_daily", "shares"
        issues.append("cn_primary_provider_unavailable")
    check_cancelled(cancelled)
    valid = frame[frame["日期"].astype(str) <= end_date.isoformat()].sort_values("日期")
    if valid.empty:
        raise ValueError("cn_completed_daily_data_unavailable")
    row = valid.iloc[-1]
    day = datetime.fromisoformat(str(row["日期"])).date()
    timestamp = datetime.combine(day, time(15), tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)
    price = _number(row.get("收盘"))
    return {"ticker": ticker, "market": "CN", "exchange": exchange, "currency": "CNY",
            "price": price if price is not None and price > 0 else None,
            "volume": _number(row.get("成交量")), "volume_unit": volume_unit,
            "source_as_of": timestamp.isoformat(), "as_of": timestamp.isoformat(),
            "source_time_basis": "daily_session_close",
            "volume_source_as_of": timestamp.isoformat(), "volume_provider": provider_name,
            "observed_at": now.isoformat(), "provider": provider_name,
            "granularity": "daily", "short_term_allowed": False,
            "quality_issues": issues,
            "constraints": ["普通A股当日买入通常不能当日卖出（T+1）。",
                            "涨跌幅与可成交条件取决于板块、上市阶段和证券状态，未逐股验证，不推断统一比例。"],
            "rule_profile_version": PROFILE_VERSION}


def _research_runtime(cancelled: Callable[[], bool] | None, timeout_seconds: float):
    from reliability import ProviderRuntime, ProviderRuntimeConfig

    class CancelAwareRuntime(ProviderRuntime):
        def execute(self, key, call, *, idempotent, cache_identity=None):
            check_cancelled(cancelled)
            result = super().execute(key, call, idempotent=idempotent, cache_identity=cache_identity)
            check_cancelled(cancelled)
            return result

    # One attempt per boundary avoids hidden retries after a user stops. Legacy
    # fetcher's Futu/AKShare/info/history fallbacks each pass through this gate.
    return CancelAwareRuntime(ProviderRuntimeConfig(
        request_timeout_seconds=timeout_seconds, max_attempts=1,
        fresh_cache_seconds=0, stale_if_error_seconds=0,
    ))


def fetch_market_context(
    instrument: dict[str, Any], *, now: datetime | None = None,
    cancelled: Callable[[], bool] | None = None, timeout_seconds: float = 10,
    stock_fetcher: Callable | None = None, cn_provider: Any = None,
    cn_history_fetcher: Callable | None = None,
) -> dict[str, Any]:
    """Return JSON-safe source timestamps; observed time cannot make data fresh."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("aware_clock_required")
    now = now.astimezone(UTC)
    if not 0 < timeout_seconds <= 60:
        raise ValueError("invalid_market_timeout")
    market, ticker = str(instrument.get("market", "")).upper(), str(instrument.get("ticker", "")).strip().upper()
    if market not in {"US", "HK", "CN"} or not ticker or len(ticker) > 32:
        raise ValueError("invalid_instrument")
    base: dict[str, Any] = {"ticker": ticker, "market": market, "name": str(instrument.get("name") or ticker),
            "exchange": instrument.get("exchange"), "currency": {"US": "USD", "HK": "HKD", "CN": "CNY"}[market],
            "price": None, "volume": None, "source_as_of": None, "as_of": None,
            "observed_at": None, "provider": None, "granularity": "unknown",
            "short_term_allowed": False, "quality_issues": [], "constraints": [],
            "rule_profile_version": PROFILE_VERSION}
    try:
        check_cancelled(cancelled)
        if market == "CN":
            base.update(_cn_context(ticker, now=now, timeout_seconds=timeout_seconds, provider=cn_provider,
                                    cancelled=cancelled, history_fetcher=cn_history_fetcher))
            check_cancelled(cancelled)
            return base
        fetch_kwargs: dict[str, Any] = {"timeout_seconds": timeout_seconds}
        if stock_fetcher is None:
            from data.fetcher import get_stock
            stock_fetcher = get_stock
            fetch_kwargs["provider_runtime"] = _research_runtime(cancelled, timeout_seconds)
        raw = stock_fetcher(ticker, market, **fetch_kwargs)
        check_cancelled(cancelled)
        metadata = raw.get("field_metadata", {})
        price_meta = metadata.get("price", {})
        source_time = _datetime(price_meta.get("source_as_of") or raw.get("as_of"))
        observed_time = _datetime(price_meta.get("observed_at") or raw.get("retrieved_at"))
        volume_time = _datetime(raw.get("volume_source_as_of"))
        pe_meta = metadata.get("pe_ttm", {})
        fundamentals_observed = _datetime(pe_meta.get("observed_at"))
        numbers = {field: _number(raw.get(field)) for field in (
            "price", "volume", "pe_ttm", "pb", "roe", "free_cashflow", "revenue_growth",
            "earnings_growth", "debt_to_equity", "market_cap", "dividend_yield"
        )}
        base.update(numbers)
        if base["price"] is not None and base["price"] <= 0:
            base["price"] = None
        base.update(source_as_of=source_time.isoformat() if source_time else None,
                    as_of=source_time.isoformat() if source_time else None,
                    observed_at=observed_time.isoformat() if observed_time else None,
                    fundamentals_observed_at=fundamentals_observed.isoformat() if fundamentals_observed else None,
                    volume_source_as_of=volume_time.isoformat() if volume_time else None,
                    volume_provider=raw.get("volume_provider"),
                    provider=raw.get("provider"), name=raw.get("name", base["name"]),
                    exchange=raw.get("exchange") or instrument.get("exchange") or ("HKEX" if market == "HK" else "US_UNVERIFIED"),
                    granularity="delayed", quality_issues=list(raw.get("quality_issues", [])))
        currency = str(raw.get("currency") or "").strip().upper()
        if currency:
            expected = base["currency"]
            base["currency"] = currency
            if currency != expected:
                base["quality_issues"].append("market_currency_mismatch")
                # The price unit is part of the security's identity. Preserve
                # observed currency but never form candidates using that price.
                base["price"] = None
        else:
            base["quality_issues"].append("market_currency_unverified")
        age = (now - source_time).total_seconds() if source_time else None
        if age is None:
            base["quality_issues"].append("quote_source_time_unknown")
        elif age < -60:
            base["quality_issues"].append("quote_source_time_future")
        elif age > 900:
            base["quality_issues"].append("quote_not_fresh_for_short_term")
        if base["volume"] is None:
            base["quality_issues"].append("volume_unavailable")
        volume_age = (now - volume_time).total_seconds() if volume_time else None
        if volume_age is None:
            base["quality_issues"].append("volume_source_time_unknown")
        elif not 0 <= volume_age <= 900:
            base["quality_issues"].append("volume_not_fresh_for_short_term")
        base["short_term_allowed"] = bool(age is not None and 0 <= age <= 900 and
                                           base["price"] is not None and base["volume"] is not None and
                                           base["volume"] > 0 and volume_age is not None and
                                           0 <= volume_age <= 900)
        return base
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "market_data_unavailable"
        known = {"cancelled", "invalid_cn_ticker", "cn_data_dependency_unavailable",
                 "cn_daily_data_unavailable", "cn_completed_daily_data_unavailable"}
        base["quality_issues"] = [reason if reason in known else "market_data_unavailable"]
        return base
