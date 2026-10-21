"""Explicit, bounded public corporate-action reads; no broker or notification API.

Yahoo chart only provides historical dividend/split rows here. Earnings and
announcements remain manual/JSON inputs; this is not a future event feed.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from .models import ResearchInstrument
from .network import ContentAccessError, check_cancelled, safe_fetch

SOURCE = "yahoo_chart"
COVERAGE = "公开历史分红/拆股（近一年）；不含未来财报、公告或未来分红，不保证市场完整覆盖。"


def yahoo_symbol(instrument: ResearchInstrument) -> str:
    if instrument.market == "US":
        return instrument.ticker.replace(".", "-")
    if instrument.market == "HK" and instrument.ticker.isdigit():
        return instrument.ticker.zfill(4).lstrip("0").zfill(4) + ".HK"
    raise ValueError("event_source_market_unsupported")


def fetch_events(instrument: ResearchInstrument, *, now: datetime | None = None,
                 cancelled: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    """One request, ten-second total deadline, 1 MB, at most 100 actions.

    The provider's opaque event-map key is preserved. A revision can be matched
    only if that upstream key is unchanged; Yahoo may itself use a timestamp as
    that key. We never derive local identity from the mutable event_date field.
    """
    now = now or datetime.now(UTC)
    symbol = yahoo_symbol(instrument)
    zone = ZoneInfo("America/New_York" if instrument.market == "US" else "Asia/Hong_Kong")
    first_day = now.astimezone(zone).date() - timedelta(days=365)
    first_instant = datetime.combine(first_day, datetime.min.time(), tzinfo=zone)
    query = urlencode({"period1": int(first_instant.timestamp()), "period2": int(now.timestamp()),
                       "interval": "1d", "events": "div,splits"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}?{query}"
    check_cancelled(cancelled)
    response = safe_fetch(url, timeout=10, max_bytes=1_000_000, cancelled=cancelled)
    try:
        payload = json.loads(response.text)
        chart = payload["chart"]
        if chart.get("error"):
            raise ContentAccessError("event_source_rejected")
        results = chart.get("result")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise ContentAccessError("event_source_invalid")
        data = results[0]
        metadata = data.get("meta", {})
        if str(metadata.get("symbol", "")).upper() != symbol.upper():
            raise ContentAccessError("event_source_identity_mismatch")
        zone_name = metadata.get("exchangeTimezoneName")
        if not isinstance(zone_name, str):
            raise ContentAccessError("event_source_timezone_unknown")
        zone = ZoneInfo(zone_name)
        actions = data.get("events", {})
        if not isinstance(actions, dict):
            raise ContentAccessError("event_source_invalid")
        entries: list[dict[str, Any]] = []
        for kind, event_type in (("dividends", "dividend"), ("splits", "split")):
            rows = actions.get(kind, {})
            if not isinstance(rows, dict) or len(rows) + len(entries) > 100:
                raise ContentAccessError("event_source_invalid")
            for upstream_id, item in rows.items():
                check_cancelled(cancelled)
                if not isinstance(item, dict) or not isinstance(upstream_id, str):
                    raise ContentAccessError("event_source_invalid")
                stamp = item["date"]
                if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                    raise ContentAccessError("event_source_invalid")
                local_day = datetime.fromtimestamp(stamp, UTC).astimezone(zone).date()
                if local_day > now.astimezone(zone).date() or local_day < (now - timedelta(days=370)).date():
                    raise ContentAccessError("event_source_outside_history")
                entry: dict[str, Any] = {
                    "instrument": instrument.model_dump(mode="json"), "event_type": event_type,
                    "source": SOURCE, "source_event_id": upstream_id,
                    "source_url": f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}/history/",
                    "event_date": local_day.isoformat(), "event_timezone": zone_name,
                    # Daily action stamps locate a date, not an exact event instant.
                    "event_at": None, "published_at": None, "observed_at": now.isoformat(),
                    "status": "confirmed", "title": "历史分红" if kind == "dividends" else "历史拆股",
                    "notes": "来源仅提供历史行动日期，发布时间和精确时刻未知；来源键变化时不能自动关联改期。",
                }
                if kind == "dividends":
                    amount = item.get("amount")
                    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
                        raise ContentAccessError("event_source_invalid")
                    entry.update(date_kind="ex_dividend", dividend_dates={"ex_dividend": local_day.isoformat()},
                                 amount=float(amount), currency=metadata.get("currency"))
                else:
                    numerator, denominator = item.get("numerator"), item.get("denominator")
                    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
                           for v in (numerator, denominator)):
                        raise ContentAccessError("event_source_invalid")
                    entry["notes"] += f" 拆股比例 {numerator}:{denominator}。"
                entries.append(entry)
        return entries
    except ContentAccessError:
        raise
    except (KeyError, ValueError, TypeError, OverflowError):
        raise ContentAccessError("event_source_invalid") from None
