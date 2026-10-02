"""Versioned research records in the existing local SQLite database."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import (
    EvidenceRecord, ExtractionResult, ResearchInstrument, ResearchReport,
    Subscription, aware, digest, evidence_from_item, parse_time,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_evidence (
 id TEXT PRIMARY KEY, source_identity TEXT NOT NULL, content_hash TEXT NOT NULL,
 source_cluster TEXT NOT NULL, payload_json TEXT NOT NULL, discovered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_evidence_source ON research_evidence(source_identity);
CREATE INDEX IF NOT EXISTS research_evidence_hash ON research_evidence(content_hash);
CREATE INDEX IF NOT EXISTS research_evidence_cluster ON research_evidence(source_cluster);
CREATE TABLE IF NOT EXISTS research_source_heads (
 source_identity TEXT PRIMARY KEY, evidence_id TEXT NOT NULL,
 FOREIGN KEY(evidence_id) REFERENCES research_evidence(id)
);
CREATE TABLE IF NOT EXISTS research_subscriptions (
 id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_reports (
 id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL, FOREIGN KEY(evidence_id) REFERENCES research_evidence(id)
);
CREATE INDEX IF NOT EXISTS research_reports_time ON research_reports(created_at DESC);
CREATE TABLE IF NOT EXISTS research_extractions (
 cache_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL, detail TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT NOT NULL, stats_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_settings (
 id INTEGER PRIMARY KEY CHECK (id = 1), payload_json TEXT NOT NULL
);
"""
_DEFAULT_SETTINGS: dict[str, Any] = {
    "markets": ["US", "HK"], "horizons": ["swing", "long"], "instruments": [],
    "interval_minutes": 15, "ai_enabled": False, "news_enabled": True,
    "news_sources": [], "max_items": 20, "max_ai_calls": 10,
    "media_enabled": False, "max_media_items": 2, "max_media_seconds": 600,
}


def _settings(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) - set(_DEFAULT_SETTINGS):
        raise ValueError("unsupported research settings")
    result = {**_DEFAULT_SETTINGS, **value}
    if (not isinstance(result["markets"], list) or not result["markets"]
            or not set(result["markets"]).issubset({"US", "HK", "CN"})):
        raise ValueError("select at least one supported market")
    if (not isinstance(result["horizons"], list) or not result["horizons"]
            or not set(result["horizons"]).issubset({"short", "swing", "long"})):
        raise ValueError("select at least one supported horizon")
    interval = result["interval_minutes"]
    if type(interval) is not int or not 1 <= interval <= 1440:
        raise ValueError("interval_minutes must be an integer in [1, 1440]")
    for key in ("ai_enabled", "news_enabled", "media_enabled"):
        if type(result[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    if not isinstance(result["instruments"], list) or len(result["instruments"]) > 100:
        raise ValueError("instruments must be a list of at most 100 securities")
    if (not isinstance(result["news_sources"], list)
            or not set(result["news_sources"]).issubset({"finnhub", "newsapi", "akshare"})):
        raise ValueError("unsupported news sources")
    for key, low, high in (("max_items", 1, 100), ("max_ai_calls", 0, 50),
                           ("max_media_items", 1, 5), ("max_media_seconds", 60, 600)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{key} is out of range")
    instruments = [ResearchInstrument.model_validate(i) for i in result["instruments"]]
    if len({i.key for i in instruments}) != len(instruments):
        raise ValueError("duplicate research instrument")
    result["instruments"] = [i.model_dump(mode="json") for i in instruments]
    result["markets"] = list(dict.fromkeys(result["markets"]))
    result["horizons"] = list(dict.fromkeys(result["horizons"]))
    return result


class ResearchStore:
    """Shared-file domain tables with atomic writes and pre-migration backup."""

    def __init__(self, path: str | Path = "alpha_guard.db") -> None:
        self.path = str(Path(path).expanduser()) if str(path) != ":memory:" else ":memory:"
        existed = self.path != ":memory:" and Path(self.path).is_file()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self.backup_path: Path | None = None
        initialized = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_evidence'"
        ).fetchone()
        heads_initialized = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_source_heads'"
        ).fetchone()
        if existed and (not initialized or not heads_initialized):
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            self.backup_path = Path(f"{self.path}.pre-research-{stamp}.bak")
            with sqlite3.connect(self.backup_path) as backup:
                self._connection.backup(backup)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> ResearchStore:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def upsert_evidence(self, item: Mapping[str, Any] | EvidenceRecord, *,
                        now: datetime | None = None) -> EvidenceRecord:
        evidence = item if isinstance(item, EvidenceRecord) else evidence_from_item(item, now=now)
        with self._lock, self._connection:
            # Existing databases used source+body IDs. Reuse an identical
            # legacy header's immutable record instead of renewing its clocks
            # simply because the versioned-header schema was introduced.
            legacy_id = digest([evidence.source_identity, evidence.content_hash])
            if legacy_id != evidence.id:
                legacy_row = self._connection.execute(
                    "SELECT payload_json FROM research_evidence WHERE id=?", (legacy_id,),
                ).fetchone()
                if legacy_row is not None:
                    legacy = EvidenceRecord.model_validate_json(legacy_row[0])
                    if (legacy.title == evidence.title and legacy.author == evidence.author
                            and legacy.published_at == evidence.published_at):
                        evidence = legacy
            inserted = self._connection.execute(
                "INSERT OR IGNORE INTO research_evidence VALUES (?, ?, ?, ?, ?, ?)",
                (evidence.id, evidence.source_identity, evidence.content_hash,
                 evidence.source_cluster, evidence.model_dump_json(),
                 evidence.discovered_at.isoformat()),
            )
            # Raw source observations advance the head. Analysis re-upserts
            # existing EvidenceRecords to retain history; that is not a fresh
            # observation and must not undo a later metadata revision barrier.
            if not isinstance(item, EvidenceRecord) or inserted.rowcount == 1:
                self._connection.execute(
                    "INSERT INTO research_source_heads (source_identity, evidence_id) VALUES (?, ?) "
                    "ON CONFLICT(source_identity) DO UPDATE SET evidence_id=excluded.evidence_id",
                    (evidence.source_identity, evidence.id),
                )
            row = self._connection.execute(
                "SELECT payload_json FROM research_evidence WHERE id=?", (evidence.id,)
            ).fetchone()
        return self._cluster_time(EvidenceRecord.model_validate_json(row[0]))

    def _cluster_time(self, evidence: EvidenceRecord) -> EvidenceRecord:
        # Only retrieve clocks, not every duplicated article body.
        with self._lock:
            rows = self._connection.execute(
                "SELECT json_extract(payload_json, '$.published_at') AS published_at, discovered_at "
                "FROM research_evidence WHERE source_cluster=?",
                (evidence.source_cluster,),
            ).fetchall()
        timestamps = [clock for row in rows if (clock := parse_time(row["published_at"])) is not None]
        first_seen = [clock for row in rows if (clock := parse_time(row["discovered_at"])) is not None]
        issues = list(evidence.quality_issues)
        if len(rows) > 1:
            issues.append("repeated_source_content")
        if len(timestamps) != len(rows):
            issues.append("original_publication_time_unknown")
        return evidence.model_copy(update={
            "origin_published_at": min(timestamps) if timestamps else None,
            "cluster_first_seen_at": min(first_seen) if first_seen else evidence.discovered_at,
            "quality_issues": list(dict.fromkeys(issues)),
        })

    def get_evidence(self, identifier: str) -> EvidenceRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM research_evidence WHERE id=?", (identifier,)
            ).fetchone()
        return self._cluster_time(EvidenceRecord.model_validate_json(row[0])) if row else None

    def latest_reusable_transcript(self, item: Mapping[str, Any]) -> EvidenceRecord | None:
        """Read the last observed source version without refreshing its clocks.

        A metadata/partial head is a revision barrier. A later true re-read can
        point back to an identical historic version without changing that
        record's original acquisition or publication times. Legacy databases
        without a head fall back to their most recently acquired version.
        """
        incoming = evidence_from_item(item)
        with self._lock:
            row = self._connection.execute(
                "SELECT e.payload_json FROM research_source_heads h "
                "JOIN research_evidence e ON e.id=h.evidence_id WHERE h.source_identity=?",
                (incoming.source_identity,),
            ).fetchone()
            if row is None:
                row = self._connection.execute(
                    "SELECT payload_json FROM research_evidence WHERE source_identity=? "
                    "ORDER BY discovered_at DESC, rowid DESC LIMIT 1",
                    (incoming.source_identity,),
                ).fetchone()
        if row is None:
            return None
        latest = EvidenceRecord.model_validate_json(row[0])
        if (latest.content_kind != "transcript" or latest.completeness != "full"
                or "local_media_transcription" not in latest.quality_issues
                or "\ufffd" in latest.content_text
                or any("\ufffd" in segment.text for segment in latest.segments)
                or not latest.content_text.strip() or not latest.segments
                or any(segment.end_seconds is None for segment in latest.segments)
                or latest.title != incoming.title or latest.author != incoming.author
                or latest.published_at != incoming.published_at):
            return None
        return self._cluster_time(latest)

    def list_evidence(self, limit: int = 100, *, offset: int = 0) -> list[EvidenceRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM research_evidence ORDER BY discovered_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (_limit(limit), _offset(offset)),
            ).fetchall()
        return [self._cluster_time(EvidenceRecord.model_validate_json(row[0])) for row in rows]

    def list_current_evidence(self, limit: int = 100, *, offset: int = 0) -> list[EvidenceRecord]:
        """Page current source versions before applying the scan's item budget."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT e.payload_json FROM research_evidence e
                   LEFT JOIN research_source_heads h ON h.source_identity=e.source_identity
                   WHERE e.id=COALESCE(h.evidence_id, (
                       SELECT previous.id FROM research_evidence previous
                       WHERE previous.source_identity=e.source_identity
                       ORDER BY previous.discovered_at DESC, previous.rowid DESC LIMIT 1))
                   ORDER BY e.discovered_at DESC, e.rowid DESC LIMIT ? OFFSET ?""",
                (_limit(limit), _offset(offset)),
            ).fetchall()
        return [self._cluster_time(EvidenceRecord.model_validate_json(row[0])) for row in rows]

    def upsert_subscription(self, value: Subscription | Mapping[str, Any]) -> Subscription:
        sub = value if isinstance(value, Subscription) else Subscription.model_validate(value)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO research_subscriptions VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json",
                (sub.id, sub.model_dump_json()),
            )
        return sub

    def list_subscriptions(self, enabled_only: bool = False, *, limit: int | None = None,
                           offset: int = 0) -> list[Subscription]:
        query = "SELECT payload_json FROM research_subscriptions"
        if enabled_only:
            query += " WHERE json_extract(payload_json, '$.enabled') = 1"
        query += " ORDER BY rowid LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._connection.execute(
                query, (-1 if limit is None else _limit(limit), _offset(offset))
            ).fetchall()
        return [Subscription.model_validate_json(row[0]) for row in rows]

    def get_subscription(self, identifier: str) -> Subscription | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM research_subscriptions WHERE id=?", (identifier,)
            ).fetchone()
        return Subscription.model_validate_json(row[0]) if row else None

    def delete_subscription(self, identifier: str) -> bool:
        with self._lock, self._connection:
            return self._connection.execute(
                "DELETE FROM research_subscriptions WHERE id=?", (identifier,)
            ).rowcount > 0

    def save_report(self, report: ResearchReport) -> ResearchReport:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO research_reports VALUES (?, ?, ?, ?)",
                (report.id, report.evidence_id, report.model_dump_json(), report.created_at.isoformat()),
            )
            row = self._connection.execute(
                "SELECT payload_json FROM research_reports WHERE id=?", (report.id,)
            ).fetchone()
        return ResearchReport.model_validate_json(row[0])

    def _report_view(self, report: ResearchReport, now: datetime) -> ResearchReport:
        report = report.at_time(now)
        evidence = self.get_evidence(report.evidence_id)
        if report.action in {"buy_candidate", "reduce_review", "hold_review"}:
            if evidence is None or evidence.event_at is None:
                report = report.model_copy(update={
                    "action": "watch", "summary": "原始事件时间不能核实，当前仅供观察。",
                    "quality_issues": list(dict.fromkeys([*report.quality_issues, "original_publication_time_unknown"])),
                })
            else:
                age_limit = {"short": timedelta(hours=6), "swing": timedelta(days=14), "long": timedelta(days=180)}[report.horizon]
                deadline = min(report.valid_until, evidence.event_at + age_limit)
                report = report.model_copy(update={"valid_until": deadline}).at_time(now)
        if evidence is not None:
            with self._lock:
                head = self._connection.execute(
                    "SELECT evidence_id FROM research_source_heads WHERE source_identity=?",
                    (evidence.source_identity,),
                ).fetchone()
                if head is None:
                    # Migrated databases retain their historical acquisition
                    # order until the next real observation establishes a head.
                    head = self._connection.execute(
                        "SELECT id FROM research_evidence WHERE source_identity=? "
                        "ORDER BY discovered_at DESC, rowid DESC LIMIT 1",
                        (evidence.source_identity,),
                    ).fetchone()
            if head is not None and head[0] != report.evidence_id:
                report = report.model_copy(update={
                    "action": "watch",
                    "status": "expired" if report.status == "expired" else "insufficient_evidence",
                    "summary": "原始来源已有新版本，本报告依据的旧材料已被替代；请核对最新正文或补齐材料后重新分析。",
                    "quality_issues": list(dict.fromkeys([*report.quality_issues, "source_evidence_superseded"])),
                })
        return report

    def get_report(self, identifier: str, *, now: datetime | None = None) -> ResearchReport | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM research_reports WHERE id=?", (identifier,)
            ).fetchone()
        return self._report_view(ResearchReport.model_validate_json(row[0]), now or datetime.now(UTC)) if row else None

    def list_reports(self, limit: int = 100, *, now: datetime | None = None, offset: int = 0) -> list[ResearchReport]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM research_reports ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (_limit(limit), _offset(offset)),
            ).fetchall()
        timestamp = now or datetime.now(UTC)
        return [self._report_view(ResearchReport.model_validate_json(row[0]), timestamp) for row in rows]

    def historical_reports(self, *, limit: int = 1000, offset: int = 0) -> list[ResearchReport]:
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be nonnegative")
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM research_reports ORDER BY created_at, rowid LIMIT ? OFFSET ?",
                (_limit(limit), offset),
            ).fetchall()
        return [ResearchReport.model_validate_json(row[0]) for row in rows]

    def conflicting_profiles(self, *, now: datetime | None = None) -> set[tuple[str, str, str]]:
        timestamp = aware(now or datetime.now(UTC)).isoformat()
        with self._lock:
            rows = self._connection.execute(
                """SELECT json_extract(r.payload_json, '$.instrument.market') AS market,
                          json_extract(r.payload_json, '$.instrument.ticker') AS ticker,
                          json_extract(r.payload_json, '$.horizon') AS horizon
                   FROM research_reports r
                   JOIN research_evidence e ON e.id=r.evidence_id
                   LEFT JOIN research_source_heads h ON h.source_identity=e.source_identity
                   WHERE e.id=COALESCE(h.evidence_id, (
                       SELECT previous.id FROM research_evidence previous
                       WHERE previous.source_identity=e.source_identity
                       ORDER BY previous.discovered_at DESC, previous.rowid DESC LIMIT 1))
                     AND json_extract(r.payload_json, '$.status') = 'ready'
                     AND json_extract(r.payload_json, '$.action') IN ('buy_candidate', 'reduce_review')
                     AND julianday(json_extract(r.payload_json, '$.created_at')) <= julianday(?)
                     AND julianday(json_extract(r.payload_json, '$.valid_until')) > julianday(?)
                   GROUP BY market, ticker, horizon
                   HAVING count(DISTINCT json_extract(r.payload_json, '$.action')) > 1""",
                (timestamp, timestamp),
            ).fetchall()
        return {(row[0], row[1], row[2]) for row in rows}

    def get_cached_extraction(self, key: str) -> ExtractionResult | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM research_extractions WHERE cache_key=?", (key,)
            ).fetchone()
        return ExtractionResult.model_validate_json(row[0]) if row else None

    def save_extraction(self, key: str, result: ExtractionResult) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO research_extractions VALUES (?, ?)",
                (key, result.model_dump_json()),
            )

    def record_run(self, status: str, detail: str = "", *, started_at: datetime | None = None,
                   finished_at: datetime | None = None, stats: Mapping[str, Any] | None = None) -> int:
        if status not in {"success", "partial", "failed", "cancelled", "running", "disabled", "needs_configuration"}:
            raise ValueError("invalid research run status")
        stamp = aware(finished_at or datetime.now(UTC))
        supplied = stats or {}
        safe_stats: dict[str, Any] = {str(k)[:80]: v for k, v in supplied.items()
                      if isinstance(v, (int, float, bool)) and math.isfinite(v) and len(str(v)) < 30}
        def source_summary(value: Any, depth: int = 0) -> dict[str, Any]:
            if not isinstance(value, dict):
                return {}
            result: dict[str, Any] = {}
            for key in ("subscription_id", "label", "provider", "operation", "status", "detail", "error_code"):
                if isinstance(value.get(key), str):
                    result[key] = value[key][:200]
            for key in ("fetched", "attempted", "succeeded", "failed", "cancelled", "article_count"):
                item = value.get(key)
                if isinstance(item, (int, bool)):
                    result[key] = item
            if depth == 0 and isinstance(value.get("sources"), list):
                result["sources"] = [source_summary(item, 1) for item in value["sources"][:100]]
            return result
        if isinstance(supplied.get("sources"), list):
            safe_stats["sources"] = [source_summary(item) for item in supplied["sources"][:100]]
        if isinstance(supplied.get("analysis_statuses"), dict):
            safe_stats["analysis_statuses"] = {str(k)[:40]: v for k, v in supplied["analysis_statuses"].items()
                                               if type(v) is int and 0 <= v <= 100_000}

        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO research_runs(status, detail, started_at, finished_at, stats_json) VALUES (?, ?, ?, ?, ?)",
                (status, detail[:1000], aware(started_at or stamp).isoformat(),
                 stamp.isoformat(), json.dumps(safe_stats, allow_nan=False)),
            )
        return int(cursor.lastrowid or 0)

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM research_runs ORDER BY id DESC LIMIT ?", (_limit(limit),)
            ).fetchall()
        return [{**{k: row[k] for k in row.keys() if k != "stats_json"},
                 "stats": json.loads(row["stats_json"])} for row in rows]

    def load_settings(self) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT payload_json FROM research_settings WHERE id=1").fetchone()
        return _settings(json.loads(row[0]) if row else {})

    def save_settings(self, settings: Mapping[str, Any]) -> None:
        payload = json.dumps(_settings(settings), ensure_ascii=False, allow_nan=False)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO research_settings VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json",
                (payload,),
            )


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError("limit must be an integer in [1, 1000]")
    return value


def _offset(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("offset must be a nonnegative integer")
    return value
