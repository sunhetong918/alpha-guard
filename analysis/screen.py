"""Universe construction for full-market research screening."""

from __future__ import annotations

from typing import Any

# A curated liquid-US list: yfinance exposes no free full-market screener, so
# US screening runs over an explicit large-cap pool instead of pretending to
# cover every listing.
US_DEFAULT_UNIVERSE: tuple[str, ...] = (
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO",
    "BRK-B", "JPM", "V", "UNH", "XOM", "MA", "COST", "HD", "PG",
    "JNJ", "NFLX", "AMD", "CRM", "BAC", "ORCL", "CVX", "WMT",
    "PFE", "KO", "PEP", "TSM", "ASML",
)

# Fallback HK pool mirroring the HSI heavyweights; used when AKShare is not
# installed or its snapshot is unavailable.
HK_FALLBACK_UNIVERSE: tuple[str, ...] = (
    "00700", "09988", "03690", "00939", "02318", "00388", "00005",
    "01299", "00386", "01398", "03988", "00941", "01024", "01810",
    "02313", "00002", "02628", "01109", "02007", "02688",
)


def _to_finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number else None  # NaN guard


def hk_universe(top_n: int) -> list[str]:
    """Top-N HK codes by market capitalization from the AKShare snapshot.

    Falls back to the curated pool when AKShare is missing or the snapshot is
    unusable; screening must never silently pretend broader coverage than it has.
    """

    if top_n <= 0:
        return []
    try:
        import akshare as ak
    except ModuleNotFoundError:
        return list(HK_FALLBACK_UNIVERSE[:top_n])
    try:
        frame = ak.stock_hk_spot_em()
        rows = frame.to_dict("records")
    except Exception:  # noqa: BLE001 - network/shape failures fall back
        return list(HK_FALLBACK_UNIVERSE[:top_n])

    ranked = sorted(
        (
            row
            for row in rows
            if _to_finite_float(row.get("总市值")) is not None
            and str(row.get("代码", "")).strip().isdigit()
        ),
        key=lambda row: float(row["总市值"]),
        reverse=True,
    )
    codes = [str(row["代码"]).strip() for row in ranked[:top_n]]
    return codes if codes else list(HK_FALLBACK_UNIVERSE[:top_n])


def us_universe(top_n: int) -> list[str]:
    """The curated US large-cap pool (documented, not full market)."""

    if top_n <= 0:
        return []
    return list(US_DEFAULT_UNIVERSE[:top_n])
