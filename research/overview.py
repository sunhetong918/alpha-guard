"""Bounded, read-only security overview; scan metadata is written only by scans."""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from zoneinfo import ZoneInfo

import config
import yaml

from .event_sources import COVERAGE, SOURCE
from .sec_filings import SOURCE as SEC_SOURCE
from .models import Horizon, ResearchInstrument, ResearchStatus, aware, parse_time
from .store import _settings
from .watchlist import _instrument, _rules

_SCAN_SCHEMA = """CREATE TABLE IF NOT EXISTS instrument_scan_overview (
 instrument TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL
);"""
_TIMEZONES = {"US": "America/New_York", "HK": "Asia/Hong_Kong", "CN": "Asia/Shanghai"}
_SOURCE_STATUSES = {"running", "success", "empty", "failed", "cancelled", "interrupted"}
_SOURCE_DETAILS = {"permission_required", "request_timeout", "request_failed", "cancelled",
                   "refresh_interrupted", "event_source_invalid", "event_source_rejected",
                   "event_source_identity_mismatch", "event_source_timezone_unknown",
                   "event_source_outside_history", "event_source_market_unsupported"}


def _key(value: Any) -> ResearchInstrument:
    if not isinstance(value, str) or value.count(":") != 1 or len(value) > 64:
        raise ValueError("invalid_overview_instrument")
    market, ticker = value.split(":", 1)
    return _instrument(market, ticker)


def _clock(value: Any) -> datetime:
    clock = parse_time(value)
    if clock is None:
        raise ValueError("invalid_overview_time")
    return clock


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("invalid_overview_text")
    return value


def _json(value: Any, maximum: int = 256_000) -> dict[str, Any]:
    if not isinstance(value, str) or len(value.encode()) > maximum:
        raise ValueError("invalid_overview_record")
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("invalid_overview_record")
    return result


def _section(action: Callable[[], dict[str, Any]], failed: dict[str, Any]) -> dict[str, Any]:
    try:
        return action()
    except (sqlite3.Error, ValueError, TypeError, KeyError, OSError, yaml.YAMLError):
        return {**failed, "status": "failed", "detail": "local_data_unavailable"}


def _report_filter(instrument: ResearchInstrument) -> tuple[str, list[str]]:
    # Older reports may retain an unpadded Hong Kong code. Match only equivalent
    # canonical identities, while retaining the market/ticker expression index.
    tickers = [instrument.ticker]
    if instrument.market == "HK":
        bare = str(int(instrument.ticker))
        tickers = [bare.zfill(width) for width in range(len(bare), 6)]
    return ("json_extract(r.payload_json,'$.instrument.market')=? AND "
            "json_extract(r.payload_json,'$.instrument.ticker') IN (" +
            ",".join("?" for _ in tickers) + ")", [instrument.market, *tickers])


def _reports(connection: sqlite3.Connection, tables: set[str], instrument: ResearchInstrument,
             now: datetime) -> dict[str, Any]:
    if "research_reports" not in tables:
        return {"status": "not_collected", "total": 0, "items": []}
    where, args = _report_filter(instrument)
    total = connection.execute("SELECT count(*) FROM research_reports r WHERE " + where, args).fetchone()[0]
    rows = connection.execute("""SELECT r.id,r.evidence_id,r.created_at,
        json_extract(r.payload_json,'$.horizon') AS horizon,
        json_extract(r.payload_json,'$.action') AS action,
        json_extract(r.payload_json,'$.status') AS status,
        json_extract(r.payload_json,'$.valid_until') AS valid_until,
        substr(json_extract(r.payload_json,'$.summary'),1,300) AS summary
        FROM research_reports r WHERE """ + where +
        " ORDER BY julianday(r.created_at) DESC,r.id LIMIT 5", args).fetchall()
    conflicts: set[str] = set()
    if {"research_evidence", "research_source_heads"} <= tables:
        conflicts = {row[0] for row in connection.execute("""SELECT json_extract(r.payload_json,'$.horizon')
            FROM research_reports r JOIN research_evidence e ON e.id=r.evidence_id
            LEFT JOIN research_source_heads h ON h.source_identity=e.source_identity WHERE """ + where + """
            AND e.id=COALESCE(h.evidence_id,(SELECT p.id FROM research_evidence p
                WHERE p.source_identity=e.source_identity ORDER BY p.discovered_at DESC,p.rowid DESC LIMIT 1))
            AND json_extract(r.payload_json,'$.status')='ready'
            AND json_extract(r.payload_json,'$.action') IN ('buy_candidate','reduce_review')
            AND julianday(r.created_at)<=julianday(?)
            AND julianday(json_extract(r.payload_json,'$.valid_until'))>julianday(?)
            GROUP BY json_extract(r.payload_json,'$.horizon')
            HAVING count(DISTINCT json_extract(r.payload_json,'$.action'))>1""",
            [*args, now.isoformat(), now.isoformat()])}
    items = []
    for row in rows:
        identity = _text(row["id"], 128)
        if row["horizon"] not in get_args(Horizon) or row["status"] not in get_args(ResearchStatus):
            raise ValueError("invalid_overview_report")
        created, expires = _clock(row["created_at"]), _clock(row["valid_until"])
        if created > now or expires <= created or not isinstance(row["summary"], str):
            raise ValueError("invalid_overview_report")
        superseded = False
        if {"research_evidence", "research_source_heads"} <= tables:
            source = connection.execute("""SELECT e.source_cluster,
                EXISTS(SELECT 1 FROM json_each(e.payload_json,'$.quality_issues')
                    WHERE value='original_publication_time_unknown') AS time_unknown,
                COALESCE(h.evidence_id,(SELECT p.id FROM research_evidence p
                    WHERE p.source_identity=e.source_identity ORDER BY p.discovered_at DESC,p.rowid DESC LIMIT 1)) AS head
                FROM research_evidence e LEFT JOIN research_source_heads h ON h.source_identity=e.source_identity
                WHERE e.id=?""", (row["evidence_id"],)).fetchone()
            if source is not None:
                superseded = source["head"] != row["evidence_id"]
                if row["action"] in {"buy_candidate", "reduce_review", "hold_review"} and not source["time_unknown"]:
                    # Match ResearchStore._cluster_time's oldest publication
                    # boundary without loading article bodies or cluster rows.
                    clocks = connection.execute("""SELECT count(*) AS total,
                        count(julianday(json_extract(payload_json,'$.published_at'))) AS known
                        FROM research_evidence WHERE source_cluster=?""", (source["source_cluster"],)).fetchone()
                    if clocks["total"] and clocks["total"] == clocks["known"]:
                        original = connection.execute("""SELECT json_extract(payload_json,'$.published_at')
                            FROM research_evidence WHERE source_cluster=?
                            ORDER BY julianday(json_extract(payload_json,'$.published_at')) LIMIT 1""",
                            (source["source_cluster"],)).fetchone()
                        age_limit = {"short": timedelta(hours=6), "swing": timedelta(days=14), "long": timedelta(days=180)}[row["horizon"]]
                        expires = min(expires, _clock(original[0]) + age_limit)
        current_status = "expired" if now >= expires else row["status"]
        if current_status != "expired" and (superseded or row["horizon"] in conflicts):
            current_status = "insufficient_evidence"
        items.append({"id": identity, "horizon": row["horizon"], "status": current_status,
                      "original_status": row["status"], "summary": row["summary"],
                      "created_at": created.isoformat(), "valid_until": expires.isoformat()})
    return {"status": "ready" if total else "empty", "total": total, "items": items}


def _reviews(connection: sqlite3.Connection, tables: set[str], instrument: ResearchInstrument) -> dict[str, Any]:
    if "research_reports" not in tables:
        return {"status": "not_collected", "pending_count": 0, "next_review_at": None}
    where, args = _report_filter(instrument)
    if "research_report_reviews" not in tables:
        total = connection.execute("SELECT count(*) FROM research_reports r WHERE " + where, args).fetchone()[0]
        return {"status": "ready" if total else "empty", "pending_count": total, "next_review_at": None}
    row = connection.execute("""SELECT count(*) AS total,
        sum(CASE WHEN COALESCE(v.status,'pending')='pending' THEN 1 ELSE 0 END) AS pending,
        sum(CASE WHEN v.status NOT IN ('pending','verified','deferred') THEN 1 ELSE 0 END) AS invalid
        FROM research_reports r LEFT JOIN research_report_reviews v ON v.report_id=r.id
        WHERE """ + where, args).fetchone()
    if row["invalid"]:
        raise ValueError("invalid_overview_review")
    due = connection.execute("""SELECT v.next_review_at FROM research_reports r
        JOIN research_report_reviews v ON v.report_id=r.id WHERE """ + where +
        " AND v.next_review_at IS NOT NULL ORDER BY julianday(v.next_review_at),r.id LIMIT 1", args).fetchone()
    return {"status": "ready" if row["total"] else "empty", "pending_count": row["pending"] or 0,
            "next_review_at": _clock(due[0]).isoformat() if due is not None else None}


def _events(connection: sqlite3.Connection, tables: set[str], instrument: ResearchInstrument,
            now: datetime) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "not_collected", "total": 0, "items": [],
                              "sources": [], "coverage": COVERAGE}
    if "research_event_sources" in tables:
        for provider in (SOURCE, SEC_SOURCE):
            row = connection.execute("SELECT payload_json FROM research_event_sources WHERE source_key=?",
                                     (f"{provider}:{instrument.key}",)).fetchone()
            if row is not None:
                source = _json(row[0])
                if source["instrument"] != instrument.key or source["source"] != provider or source["status"] not in _SOURCE_STATUSES:
                    raise ValueError("invalid_overview_source")
                status = source["status"]
                detail = source.get("detail", "")
                detail = detail if detail in _SOURCE_DETAILS or detail == "" else "source_failed"
                if status == "running" and _clock(source["expires_at"]) <= now:
                    status, detail = "interrupted", "refresh_interrupted"
                result["sources"].append({"source": provider, "status": status, "detail": detail,
                                      **{key: _clock(source[key]).isoformat() if source.get(key) else None
                                         for key in ("last_success_at", "finished_at")}})
    if bool("research_event_heads" in tables) != bool("research_event_versions" in tables):
        raise ValueError("incomplete_overview_event_schema")
    if "research_event_heads" not in tables:
        return result
    if connection.execute("""SELECT 1 FROM research_event_heads h LEFT JOIN research_event_versions v
        ON h.event_id=v.event_id AND h.version=v.version WHERE h.instrument=? AND v.event_id IS NULL LIMIT 1""",
        (instrument.key,)).fetchone():
        raise ValueError("missing_overview_event_version")
    if connection.execute("""SELECT 1 FROM research_event_heads h JOIN research_event_versions v
        ON h.event_id=v.event_id AND h.version=v.version WHERE h.instrument=? AND (
        json_extract(v.payload_json,'$.status') IS NULL
        OR json_extract(v.payload_json,'$.status') NOT IN ('expected','confirmed','cancelled')
        OR julianday(h.event_date) IS NULL
        OR json_extract(v.payload_json,'$.event_date') IS NOT h.event_date
        OR (json_extract(v.payload_json,'$.event_at') IS NOT NULL
            AND julianday(json_extract(v.payload_json,'$.event_at')) IS NULL)) LIMIT 1""",
        (instrument.key,)).fetchone():
        raise ValueError("invalid_overview_event_metadata")
    # Today is determined by the security's market, not UTC midnight. Explicit
    # times that have already passed are excluded below; date-only plans remain.
    today = now.astimezone(ZoneInfo(_TIMEZONES[instrument.market])).date().isoformat()
    where = """h.instrument=? AND h.event_date>=?
        AND json_extract(v.payload_json,'$.status')!='cancelled'
        AND (json_extract(v.payload_json,'$.event_at') IS NULL
             OR julianday(json_extract(v.payload_json,'$.event_at'))>=julianday(?))"""
    join = " FROM research_event_heads h JOIN research_event_versions v ON h.event_id=v.event_id AND h.version=v.version WHERE "
    args = (instrument.key, today, now.isoformat())
    total = connection.execute("SELECT count(*)" + join + where, args).fetchone()[0]
    rows = connection.execute("SELECT v.payload_json" + join + where +
                              " ORDER BY h.event_date,h.event_id LIMIT 5", args).fetchall()
    for row in rows:
        event = _json(row[0])
        if _instrument(event["instrument"]["market"], event["instrument"]["ticker"]).key != instrument.key:
            raise ValueError("invalid_overview_event_identity")
        if event["status"] not in {"expected", "confirmed"} or event["event_type"] not in {"earnings", "announcement", "dividend", "split"}:
            raise ValueError("invalid_overview_event")
        date.fromisoformat(event["event_date"])
        result["items"].append({"event_id": _text(event["event_id"], 64),
                                "title": _text(event["title"], 300), "event_type": event["event_type"],
                                "event_date": event["event_date"], "status": event["status"],
                                "source": _text(event["source"], 80),
                                **{key: _clock(event[key]).isoformat() if event.get(key) else None
                                   for key in ("event_at", "observed_at")}})
    result.update(status="ready" if total else "empty", total=total)
    return result


def _scan(connection: sqlite3.Connection, tables: set[str], instrument: ResearchInstrument) -> dict[str, Any]:
    missing = {"status": "not_collected", "observed_at": None, "detail": "per_instrument_scan_not_recorded"}
    if "instrument_scan_overview" not in tables:
        return missing
    row = connection.execute("SELECT observed_at,status,detail FROM instrument_scan_overview WHERE instrument=?",
                             (instrument.key,)).fetchone()
    if row is None:
        return missing
    if row["status"] not in {"ready", "failed"} or row["detail"] not in {"evaluated", "data_unavailable", "evaluation_failed", "unknown_evidence"}:
        raise ValueError("invalid_overview_scan")
    return {"status": row["status"], "observed_at": _clock(row["observed_at"]).isoformat(), "detail": row["detail"]}


def execute_overview(database: Path, fields: Mapping[str, Any], *, rules_path: Path | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """No store constructor, migration, provider, token or other external call."""
    if not isinstance(fields, Mapping) or set(fields) != {"instrument"}:
        raise ValueError("invalid_overview_fields")
    instrument = _key(fields["instrument"])
    timestamp = aware(now or datetime.now(UTC))
    result: dict[str, Any] = {"instrument": {"key": instrument.key, "market": instrument.market,
                                            "ticker": instrument.ticker, "name": instrument.name},
        "as_of": timestamp.isoformat(),
        "membership": {"status": "not_collected", "listed": False, "research_enabled": False, "research_market_enabled": False},
        "rules": {"status": "not_collected", "configured": False, "monitoring_enabled": False, "rule_count": 0},
        "scan": {"status": "not_collected", "observed_at": None, "detail": "per_instrument_scan_not_recorded"},
        "reports": {"status": "not_collected", "total": 0, "items": []},
        "reviews": {"status": "not_collected", "pending_count": 0, "next_review_at": None},
        "events": {"status": "not_collected", "total": 0, "items": [], "sources": [], "coverage": COVERAGE}}

    def rules() -> dict[str, Any]:
        _, _, parsed = _rules(rules_path or config.RULES_CONFIG_PATH)
        match = next((item for ticker, item in parsed.watchlist.items()
                      if _instrument(item.market, ticker).key == instrument.key), None)
        if match is not None:
            result["instrument"]["name"] = match.name[:120]
            result["membership"].update(status="ready", listed=True)
        return {"status": "ready" if match else "empty", "configured": match is not None,
                "monitoring_enabled": match.enabled if match else False,
                "rule_count": len(match.buy_rules) + len(match.sell_rules) if match else 0}

    result["rules"] = _section(rules, {"configured": None, "monitoring_enabled": None, "rule_count": None})
    if not database.exists():
        return result
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        deadline = time.monotonic() + 1.5
        steps = 0

        def bounded_query() -> int:
            nonlocal steps
            steps += 1000
            return int(steps > 2_000_000 or time.monotonic() > deadline)

        connection.set_progress_handler(bounded_query, 1000)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

            def membership() -> dict[str, Any]:
                value = dict(result["membership"])
                if "research_watchlist" in tables:
                    row = connection.execute("SELECT payload_json FROM research_watchlist WHERE instrument_key=?", (instrument.key,)).fetchone()
                    if row is not None:
                        entry = _json(row[0])
                        saved = _instrument(entry["market"], entry["ticker"], entry.get("name", ""))
                        if saved.key != instrument.key:
                            raise ValueError("invalid_overview_membership")
                        result["instrument"]["name"] = saved.name[:120]
                        value.update(status="ready", listed=True)
                if "research_settings" in tables:
                    row = connection.execute("SELECT payload_json FROM research_settings WHERE id=1").fetchone()
                    settings = _settings(_json(row[0]) if row is not None else {})
                    selected = next((item for item in settings["instruments"]
                                     if _instrument(item["market"], item["ticker"]).key == instrument.key), None)
                    value.update(status="ready", research_enabled=selected is not None,
                                 research_market_enabled=instrument.market in settings["markets"])
                    if selected is not None:
                        value["listed"] = True
                        result["instrument"]["name"] = selected.get("name", "")[:120]
                return value

            result["membership"] = _section(membership, {"listed": None, "research_enabled": None, "research_market_enabled": None})
            result["scan"] = _section(lambda: _scan(connection, tables, instrument), {"observed_at": None})
            result["reports"] = _section(lambda: _reports(connection, tables, instrument, timestamp), {"total": None, "items": []})
            result["reviews"] = _section(lambda: _reviews(connection, tables, instrument), {"pending_count": None, "next_review_at": None})
            result["events"] = _section(lambda: _events(connection, tables, instrument, timestamp),
                                         {"total": None, "items": [], "sources": [], "coverage": COVERAGE})
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        if connection is not None:
            connection.close()
        for name, count_key in (("membership", None), ("scan", None), ("reports", "total"), ("reviews", "pending_count"), ("events", "total")):
            result[name].update(status="failed", detail="local_data_unavailable")
            if count_key:
                result[name][count_key] = None
            if name == "membership":
                result[name].update(listed=None, research_enabled=None, research_market_enabled=None)
    return result


def record_instrument_scans(database: Path, observations: Sequence[Mapping[str, Any]], *, observed_at: datetime) -> None:
    """Write only sanitized per-security scan metadata; callers isolate failures."""
    if not observations:
        return
    stamp = aware(observed_at).isoformat()
    rows = []
    for item in observations:
        instrument = _instrument(item["market"], item["ticker"])
        if item["status"] not in {"ready", "failed"} or item["detail"] not in {"evaluated", "data_unavailable", "evaluation_failed", "unknown_evidence"}:
            raise ValueError("invalid_scan_overview_observation")
        rows.append((instrument.key, stamp, item["status"], item["detail"]))
    with closing(sqlite3.connect(database, timeout=1)) as connection, connection:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='instrument_scan_overview'").fetchone()
        if exists is None:
            backup = Path(f"{database}.pre-overview-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.bak")
            with closing(sqlite3.connect(backup)) as target:
                connection.backup(target)
            connection.executescript(_SCAN_SCHEMA)
        connection.executemany("""INSERT INTO instrument_scan_overview VALUES (?,?,?,?)
            ON CONFLICT(instrument) DO UPDATE SET observed_at=excluded.observed_at,
            status=excluded.status,detail=excluded.detail
            WHERE julianday(excluded.observed_at)>=julianday(instrument_scan_overview.observed_at)""", rows)
