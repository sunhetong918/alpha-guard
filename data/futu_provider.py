"""Optional Futu OpenAPI quote provider.

Futu quotes arrive through the local OpenD gateway (``OpenQuoteContext``).
The integration is fully opt-in: without ``futu-api`` installed and
``FUTU_ENABLED=true`` in the environment, importing this module stays cheap
and every helper reports "unavailable" so the fetcher can fall back to
AKShare/yfinance exactly as before.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import Any
from zoneinfo import ZoneInfo

from reliability import (
    CacheState,
    ProviderAttempt,
    ProviderKey,
    ProviderRuntime,
    ProviderUnavailableError,
)

futu_api: Any | None = None
_futu_import_failed = False

_MARKET_TZ = {
    "HK": ZoneInfo("Asia/Shanghai"),
    "US": ZoneInfo("America/New_York"),
}
_OPEND_CONNECT_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class FutuQuote:
    """One normalized US/HK quote with provenance."""

    price: float
    name: str
    source_as_of: str | None
    observed_at: str
    cache_state: CacheState = CacheState.NONE
    usable_for_signal: bool = True


def quote_available() -> bool:
    """True only when the SDK is installed and the feature is enabled."""

    from config import get_settings

    if not get_settings().futu_enabled:
        return False
    return _load_futu_api() is not None


def _load_futu_api() -> Any | None:
    """Import the large optional SDK only after Futu is explicitly enabled."""

    global _futu_import_failed, futu_api
    if futu_api is not None:
        return futu_api
    if _futu_import_failed:
        return None
    try:
        futu_api = import_module("futu")
    except (ImportError, OSError):
        _futu_import_failed = True
        return None
    return futu_api


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _futu_as_of(value: Any, *, market: str) -> str | None:
    """Convert Futu's timezone-less ``update_time`` to UTC ISO."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        local = datetime.fromisoformat(text)
    except ValueError:
        return None
    if local.tzinfo is None:
        local = local.replace(tzinfo=_MARKET_TZ[market])
    return local.astimezone(UTC).isoformat()


def _futu_code(ticker: str, *, market: str) -> str:
    """Map one canonical Alpha Guard ticker to a Futu market code."""

    if market == "HK":
        local = ticker.removesuffix(".HK")
        if not local.isdigit():
            raise ValueError("Futu HK ticker must be numeric")
        number = int(local)
        if not 0 < number <= 99_999:
            raise ValueError("Futu HK ticker is out of range")
        ticker = f"{number:05d}"
    return f"{market}.{ticker}"


def fetch_quote(
    ticker: str,
    *,
    market: str,
    host: str,
    port: int,
    runtime: ProviderRuntime | None = None,
) -> tuple[FutuQuote | None, tuple[ProviderAttempt, ...]]:
    """Fetch one read-only US/HK market snapshot through local OpenD."""

    normalized_ticker = _normalized_tickers([ticker])[0]
    quotes, attempts = fetch_quotes(
        [normalized_ticker],
        market=market,
        host=host,
        port=port,
        runtime=runtime,
    )
    return quotes.get(normalized_ticker), attempts


def fetch_quotes(
    tickers: Sequence[str],
    *,
    market: str,
    host: str,
    port: int,
    runtime: ProviderRuntime | None = None,
) -> tuple[dict[str, FutuQuote], tuple[ProviderAttempt, ...]]:
    """Fetch up to 400 same-market snapshots through one read-only context."""

    market_name = str(market).strip().upper()
    if market_name not in _MARKET_TZ:
        raise ValueError("Futu quote market must be US or HK")
    normalized_tickers = _normalized_tickers(tickers)
    if len(normalized_tickers) > 400:
        raise ValueError("Futu snapshot batch cannot exceed 400 tickers")
    api = _load_futu_api()
    if api is None:
        return {}, ()
    operation = f"{market_name.lower()}_snapshot"
    key = ProviderKey(provider="futu", operation=operation, market=market_name)
    cache_identity = f"{host}:{port}:{market_name}:{','.join(normalized_tickers)}"
    wanted_codes = [
        _futu_code(ticker, market=market_name) for ticker in normalized_tickers
    ]
    ticker_by_code = dict(zip(wanted_codes, normalized_tickers, strict=True))

    def observed_call() -> tuple[list[dict[str, Any]], str]:
        # Futu's default synchronous constructor retries forever when OpenD is
        # absent.  Async connect plus the SDK's public query-connect timeout
        # gives the Guardian a bounded failure that can safely fall back.
        quote_context = api.OpenQuoteContext(
            host=host,
            port=port,
            is_async_connect=True,
        )
        try:
            quote_context.set_sync_query_connect_timeout(
                _OPEND_CONNECT_TIMEOUT_SECONDS
            )
            ret, frame = quote_context.get_market_snapshot(wanted_codes)
            connection_status = getattr(quote_context, "status", None)
        finally:
            quote_context.close()
        if ret != 0:
            if connection_status != "READY":
                raise ConnectionError("Futu OpenD is unavailable")
            raise ValueError("Futu snapshot response is unavailable")
        rows = _normalized_snapshot_rows(frame, market=market_name)
        if (
            len(rows) != len(wanted_codes)
            or {row["code"] for row in rows} != set(wanted_codes)
        ):
            raise ValueError("Futu snapshot response is incomplete")
        return rows, _utc_now_iso()

    try:
        if runtime is None:
            rows, observed_at = observed_call()
            attempts: tuple[ProviderAttempt, ...] = ()
            cache_state = CacheState.NONE
            usable_for_signal = True
        else:
            result = runtime.execute(
                key, observed_call, idempotent=True, cache_identity=cache_identity
            )
            rows, observed_at = result.value
            attempts = result.attempts
            cache_state = result.cache_state
            usable_for_signal = result.usable_for_signal
    except ProviderUnavailableError as exc:
        return {}, exc.attempts
    except Exception:  # noqa: BLE001 - price source boundary, fall back quietly
        return {}, ()

    quotes: dict[str, FutuQuote] = {}
    for row in rows:
        code = row["code"]
        ticker = ticker_by_code.get(code)
        if ticker is None:
            continue
        quotes[ticker] = FutuQuote(
            price=row["price"],
            name=row["name"],
            source_as_of=row["source_as_of"],
            observed_at=observed_at,
            cache_state=cache_state,
            usable_for_signal=usable_for_signal,
        )
    return quotes, attempts


def _normalized_snapshot_rows(
    frame: Any, *, market: str
) -> list[dict[str, Any]]:
    """Reduce a provider frame to JSON-stable, read-only quote evidence."""

    try:
        raw_rows = frame.to_dict("records")
    except Exception as exc:  # noqa: BLE001 - untrusted SDK response object
        raise ValueError("Futu snapshot response is invalid") from exc
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise ValueError("Futu snapshot response is invalid")
    normalized: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            continue
        try:
            code = str(raw.get("code") or "").strip().upper()
            name = str(raw.get("name") or raw.get("stock_name") or "").strip()
        except Exception:  # noqa: BLE001 - malformed scalar; skip this row
            continue
        price = _to_finite_float(raw.get("last_price"))
        source_as_of = _futu_as_of(raw.get("update_time"), market=market)
        if not code or price is None or price <= 0 or source_as_of is None:
            continue
        normalized.append(
            {
                "code": code,
                "price": price,
                "name": name,
                "source_as_of": source_as_of,
            }
        )
    return normalized


def _normalized_tickers(tickers: Sequence[str]) -> list[str]:
    if isinstance(tickers, (str, bytes)):
        raise ValueError("Futu tickers must be a sequence")
    normalized = sorted({str(ticker).strip().upper() for ticker in tickers})
    if not normalized or any(not ticker for ticker in normalized):
        raise ValueError("Futu quote ticker cannot be empty")
    return normalized


def fetch_hk_quote(
    local_code: str,
    *,
    host: str,
    port: int,
    runtime: ProviderRuntime | None = None,
) -> tuple[FutuQuote | None, tuple[ProviderAttempt, ...]]:
    """Fetch one HK quote via OpenD; return ``None`` on any boundary failure.

    Failures never raise past this module: the caller falls back to the next
    price source and records the attempts for the reliability report.
    """

    return fetch_quote(
        local_code,
        market="HK",
        host=host,
        port=port,
        runtime=runtime,
    )


def _to_finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None
