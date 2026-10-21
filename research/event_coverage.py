"""Source query coverage is separate from event identity and revision history."""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .event_sources import SOURCE
from .sec_filings import SOURCE as SEC_SOURCE
from .models import ResearchInstrument, aware

FRESHNESS = timedelta(hours=24)
MAX_CHECKS = 200
_SOURCE_TYPES = {SOURCE: {"dividend", "split"}, SEC_SOURCE: {"announcement"}}
_FRESHNESS = {SOURCE: FRESHNESS, SEC_SOURCE: timedelta(hours=6)}
SCHEMA = """
CREATE TABLE IF NOT EXISTS research_event_coverage (
 run_id TEXT NOT NULL, source TEXT NOT NULL, instrument TEXT NOT NULL,
 event_type TEXT NOT NULL, date_from TEXT NOT NULL, date_to TEXT NOT NULL,
 status TEXT NOT NULL, checked_at TEXT NOT NULL, fetched_at TEXT,
 cache_source_at TEXT, fresh_until TEXT, count INTEGER, detail TEXT NOT NULL,
 PRIMARY KEY(run_id,event_type,date_from,date_to)
);
CREATE INDEX IF NOT EXISTS research_event_coverage_lookup
 ON research_event_coverage(instrument,event_type,source,checked_at DESC);
"""


def history_range(instrument: ResearchInstrument, now: datetime) -> tuple[date, date]:
    zone = ZoneInfo("America/New_York" if instrument.market == "US" else "Asia/Hong_Kong")
    end = aware(now).astimezone(zone).date()
    return end - timedelta(days=365), end


def record_range(connection: sqlite3.Connection, *, run_id: str, instrument: str,
                 event_type: str, date_from: date, date_to: date, status: str,
                 checked_at: datetime, count: int | None = None, detail: str = "",
                 cache_source_at: datetime | None = None, source: str = SOURCE) -> None:
    """Called only by the source worker within its result transaction.

    A cached check preserves its original fetch clock, never the read clock.
    Separate ranges allow a future paginated adapter to commit partial coverage.
    """
    if source not in _SOURCE_TYPES or event_type not in _SOURCE_TYPES[source] or status not in {"success", "empty", "failed", "cancelled", "running"}:
        raise ValueError("event_coverage_status_invalid")
    if date_from > date_to or (date_to - date_from).days > 366:
        raise ValueError("event_coverage_range_invalid")
    stamp = aware(checked_at)
    fetched = aware(cache_source_at) if cache_source_at is not None else stamp
    if fetched > stamp or (count is not None and (type(count) is not int or not 0 <= count <= 100)):
        raise ValueError("event_coverage_clock_or_count_invalid")
    success = status in {"success", "empty"}
    if success and (count is None or (status == "empty") != (count == 0)):
        raise ValueError("event_coverage_count_required")
    connection.execute(
        "INSERT INTO research_event_coverage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(run_id,event_type,date_from,date_to) DO UPDATE SET "
        "status=excluded.status,checked_at=excluded.checked_at,fetched_at=excluded.fetched_at,"
        "cache_source_at=excluded.cache_source_at,fresh_until=excluded.fresh_until,count=excluded.count,detail=excluded.detail "
        "WHERE research_event_coverage.status='running'",
        (run_id, source, instrument, event_type, date_from.isoformat(), date_to.isoformat(),
         status, stamp.isoformat(), fetched.isoformat() if success else None,
         fetched.isoformat() if success and cache_source_at is not None else None,
         (fetched + _FRESHNESS[source]).isoformat() if success else None, count if success else None, detail[:80]),
    )


def query_coverage(connection: sqlite3.Connection, instrument: ResearchInstrument | None, *,
                   event_type: str | None = None, date_from: str | None = None,
                   date_to: str | None = None, now: datetime | None = None, source: str = SOURCE) -> dict[str, Any]:
    """Bounded local interval queries; a limit or malformed record fails closed."""
    if source not in _SOURCE_TYPES:
        raise ValueError("event_source_invalid")
    now = aware(now or datetime.now(UTC))
    if instrument is None:
        return {"status": "selection_required", "ranges": [], "source": source,
                "detail": "select_one_instrument"}
    first, last = history_range(instrument, now)
    start = date.fromisoformat(date_from) if date_from else min(first, date.fromisoformat(date_to)) if date_to else first
    end = date.fromisoformat(date_to) if date_to else max(last, start)
    if start > end or not date(1900, 1, 1) <= start <= end <= date(2200, 12, 31):
        raise ValueError("event_coverage_range_invalid")
    kinds = [event_type] if event_type else ["earnings", "announcement", "dividend", "split"]
    result: dict[str, Any] = {"source": source, "instrument": instrument.key,
                            "date_from": start.isoformat(), "date_to": end.isoformat(),
                            "freshness_seconds": int(_FRESHNESS[source].total_seconds()), "ranges": []}
    for kind in kinds:
        if kind not in {"earnings", "announcement", "dividend", "split"}:
            raise ValueError("event_type_invalid")
        if instrument.market not in ({"US"} if source == SEC_SOURCE else {"US", "HK"}) or kind not in _SOURCE_TYPES[source]:
            result["ranges"].append(_range(kind, start, end, "unsupported"))
            continue
        cursor = connection.execute(
            "SELECT date_from,date_to,status,checked_at,fetched_at,cache_source_at,fresh_until,detail "
            "FROM research_event_coverage WHERE source=? AND instrument=? AND event_type=? "
            "AND date_to>=? AND date_from<=? ORDER BY julianday(checked_at) DESC,run_id DESC LIMIT ?",
            (source, instrument.key, kind, start.isoformat(), end.isoformat(), MAX_CHECKS + 1),
        )
        records = [dict(zip([field[0] for field in cursor.description], row)) for row in cursor.fetchall()]
        truncated = len(records) > MAX_CHECKS
        records = records[:MAX_CHECKS]
        try:
            ranges = _segments(kind, start, end, first, last, records, now, freshness=_FRESHNESS[source])
            for row in ranges:
                if truncated:
                    row["history_truncated"] = True
                    if row["status"] == "not_queried":
                        row.update(status="unknown", detail="coverage_query_limit")
        except (ValueError, TypeError, KeyError):
            ranges = [_range(kind, start, end, "unknown", detail="coverage_record_invalid")]
        result["ranges"].extend(ranges)
    states = {row["status"] for row in result["ranges"]}
    result["status"] = next(iter(states)) if len(states) == 1 else "partial"
    return result


def _range(kind: str, start: date, end: date, status: str, **extra: Any) -> dict[str, Any]:
    return {"event_type": kind, "date_from": start.isoformat(), "date_to": end.isoformat(),
            "status": status, "checked_at": None, "last_success_at": None,
            "cache_source_at": None, "fresh_until": None, **extra}


def _segments(kind: str, start: date, end: date, first: date, last: date,
              records: list[dict[str, Any]], now: datetime, *, freshness: timedelta = FRESHNESS) -> list[dict[str, Any]]:
    stop = end + timedelta(days=1)
    boundaries = {start, stop}
    for boundary in (first, last + timedelta(days=1)):
        if start < boundary < stop:
            boundaries.add(boundary)
    for row in records:
        a, b = date.fromisoformat(row["date_from"]), date.fromisoformat(row["date_to"])
        if a > b:
            raise ValueError("invalid stored interval")
        row["start"], row["end"] = a, b
        row["clock"] = aware(datetime.fromisoformat(row["checked_at"]))
        if row["clock"] > now or row["status"] not in {"success", "empty", "failed", "cancelled", "running"}:
            raise ValueError("invalid stored result")
        if row["status"] in {"success", "empty"}:
            fetched = aware(datetime.fromisoformat(row["fetched_at"]))
            expiry = aware(datetime.fromisoformat(row["fresh_until"]))
            if fetched > row["clock"] or expiry != fetched + freshness:
                raise ValueError("invalid stored freshness")
        for boundary in (max(start, a), min(stop, b + timedelta(days=1))):
            boundaries.add(boundary)
    ordered = sorted(boundaries)
    output: list[dict[str, Any]] = []
    for a, next_day in zip(ordered, ordered[1:]):
        b = next_day - timedelta(days=1)
        if a < first or a > last:
            value = _range(kind, a, b, "unsupported")
        else:
            matches = [row for row in records if row["start"] <= a and row["end"] >= b]
            if not matches:
                value = _range(kind, a, b, "not_queried")
            else:
                latest = matches[0]
                successful = next((r for r in matches if r["status"] in {"success", "empty"}), None)
                status = "failed" if latest["status"] == "cancelled" else latest["status"]
                detail = latest["detail"]
                if status == "running" and now - latest["clock"] >= timedelta(seconds=30):
                    status, detail = "failed", "refresh_interrupted"
                if status in {"success", "empty"} and aware(datetime.fromisoformat(latest["fresh_until"])) <= now:
                    status = "stale"
                value = _range(kind, a, b, status, checked_at=latest["checked_at"], detail=detail,
                               last_success_at=successful["fetched_at"] if successful else None,
                               cache_source_at=successful["cache_source_at"] if successful else None,
                               fresh_until=successful["fresh_until"] if successful else None)
        # Merge only equal result semantics; do not erase failed-day boundaries.
        if output and {k: v for k, v in output[-1].items() if k not in {"date_from", "date_to"}} == {
                k: v for k, v in value.items() if k not in {"date_from", "date_to"}}:
            output[-1]["date_to"] = value["date_to"]
        else:
            output.append(value)
    return output
