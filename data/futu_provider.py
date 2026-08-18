"""Optional Futu OpenAPI quote provider.

Futu quotes arrive through the local OpenD gateway (``OpenQuoteContext``).
The integration is fully opt-in: without ``futu-api`` installed and
``FUTU_ENABLED=true`` in the environment, importing this module stays cheap
and every helper reports "unavailable" so the fetcher can fall back to
AKShare/yfinance exactly as before.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from reliability import ProviderAttempt, ProviderKey, ProviderRuntime

try:  # Keep helpers importable without the optional futu extra.
    import futu as futu_api
except ModuleNotFoundError:  # pragma: no cover - exercised via quote_available
    futu_api = None

_HK_TZ = ZoneInfo("Asia/Hong_Kong")


@dataclass(frozen=True)
class FutuQuote:
    """One normalized HK quote with provenance."""

    price: float
    name: str
    source_as_of: str | None
    observed_at: str


def quote_available() -> bool:
    """True only when the SDK is installed and the feature is enabled."""

    if futu_api is None:
        return False
    from config import get_settings

    return get_settings().futu_enabled


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _futu_as_of(value: Any) -> str | None:
    """Convert a Futu ``update_timestamp`` (HK local time) to UTC ISO."""

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
        local = local.replace(tzinfo=_HK_TZ)
    return local.astimezone(UTC).isoformat()


def _futu_code(local_code: str) -> str:
    """Map the canonical five-digit code (``00700``) to ``HK.00700``."""

    return f"HK.{local_code}"


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

    if futu_api is None:
        return None, ()
    key = ProviderKey(provider="futu", operation="hk_snapshot", market="HK")
    cache_identity = f"{host}:{port}:{local_code}"

    def observed_call() -> tuple[Any, str]:
        quote_context = futu_api.OpenQuoteContext(host, port)
        try:
            ret, frame = quote_context.get_market_snapshot([_futu_code(local_code)])
        finally:
            quote_context.close()
        if ret != 0:
            raise RuntimeError(f"futu snapshot failed: {ret}")
        return frame, _utc_now_iso()

    try:
        if runtime is None:
            frame, observed_at = observed_call()
            attempts: tuple[ProviderAttempt, ...] = ()
        else:
            result = runtime.execute(
                key, observed_call, idempotent=True, cache_identity=cache_identity
            )
            frame, observed_at = result.value
            attempts = result.attempts
    except Exception:  # noqa: BLE001 - price source boundary, fall back quietly
        return None, ()

    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else []
    for row in rows:
        price = _to_finite_float(row.get("last_price"))
        if price is not None and price > 0:
            return (
                FutuQuote(
                    price=price,
                    name=str(row.get("stock_name") or "").strip(),
                    source_as_of=_futu_as_of(row.get("update_timestamp")),
                    observed_at=observed_at,
                ),
                attempts,
            )
    return None, attempts


def _to_finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None
