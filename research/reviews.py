"""Local human reviews and price follow-up; original research stays immutable."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any
import uuid

from .models import ResearchInstrument, ResearchReport, parse_time
from .replay import PriceObservation, track_reports
from .store import ResearchStore

MAX_IMPORT_BYTES = 256_000
MAX_SAMPLES = 500
DETAIL_CHARS = 20_000
REVIEW_STATUSES = {"pending", "verified", "deferred"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_report_reviews (
 report_id TEXT PRIMARY KEY REFERENCES research_reports(id),
 status TEXT NOT NULL CHECK(status IN ('pending','verified','deferred')),
 notes TEXT NOT NULL, next_review_at TEXT, updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision > 0)
);
CREATE TABLE IF NOT EXISTS research_review_samples (
 id TEXT PRIMARY KEY, source_label TEXT NOT NULL, imported_at TEXT NOT NULL,
 sample_count INTEGER NOT NULL, content_sha256 TEXT NOT NULL,
 payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_review_replays (
 id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES research_reports(id),
 sample_set_id TEXT NOT NULL REFERENCES research_review_samples(id),
 created_at TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_review_replay_report
 ON research_review_replays(report_id, created_at DESC);
"""


class ReviewConflictError(ValueError):
    """Another window changed this review; a fresh revision is required."""


def _integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("review_integer_out_of_range")
    return value


def _text(value: Any, *, limit: int, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("invalid_review_text")
    return value


def _timestamp(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError("review_time_requires_timezone")
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError("review_time_requires_timezone")
    return parsed.astimezone(UTC).isoformat()


def _instrument(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 100 or ":" not in value:
        raise ValueError("invalid_review_instrument")
    market, ticker = value.split(":", 1)
    return ResearchInstrument.model_validate({"market": market, "ticker": ticker}).key


def _review_row(row: Mapping[str, Any] | None, report_id: str) -> dict[str, Any]:
    if row is None:
        return {"report_id": report_id, "status": "pending", "notes": "",
                "next_review_at": None, "updated_at": None, "revision": 0}
    return {key: row[key] for key in
            ("report_id", "status", "notes", "next_review_at", "updated_at", "revision")}


class ReviewStore(ResearchStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        self.ensure_feature_schema(_SCHEMA, ("research_report_reviews", "research_review_samples", "research_review_replays"))
        self._followup_report_id: str | None = None

    def __enter__(self) -> ReviewStore:
        return self

    def original_report(self, report_id: str) -> ResearchReport:
        _text(report_id, limit=128)
        row = self._connection.execute("SELECT payload_json FROM research_reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            raise ValueError("review_report_not_found")
        return ResearchReport.model_validate_json(row[0])

    def historical_reports(self, *, limit: int = 1000, offset: int = 0) -> list[ResearchReport]:
        if self._followup_report_id is None:
            return super().historical_reports(limit=limit, offset=offset)
        if offset or not limit:
            return []
        return [self.original_report(self._followup_report_id)]

    def save_review(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        report_id = _text(fields["report_id"], limit=128)
        status = fields["status"]
        if not isinstance(status, str) or status not in REVIEW_STATUSES:
            raise ValueError("invalid_review_status")
        notes = _text(fields["notes"], limit=4000, empty=True)
        next_review = _timestamp(fields["next_review_at"], optional=True)
        expected = _integer(fields["revision"], 0, 2**31 - 2)
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self.original_report(report_id)
            previous = self._connection.execute(
                "SELECT revision FROM research_report_reviews WHERE report_id=?", (report_id,)).fetchone()
            revision = previous[0] if previous else 0
            if revision != expected:
                raise ReviewConflictError("review_conflict")
            updated = datetime.now(UTC).isoformat()
            self._connection.execute(
                "INSERT INTO research_report_reviews VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(report_id) DO UPDATE SET status=excluded.status, notes=excluded.notes, "
                "next_review_at=excluded.next_review_at, updated_at=excluded.updated_at, revision=excluded.revision",
                (report_id, status, notes, next_review, updated, revision + 1))
        return {"report_id": report_id, "status": status, "notes": notes,
                "next_review_at": next_review, "updated_at": updated, "revision": revision + 1}

    def list_reviews(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        offset = _integer(fields.get("offset", 0), 0, 1_000_000)
        limit = _integer(fields.get("limit", 20), 1, 50)
        instrument = _instrument(fields.get("instrument"))
        status = fields.get("status")
        if status not in (None, "") and (not isinstance(status, str) or status not in REVIEW_STATUSES):
            raise ValueError("invalid_review_status")
        start = _timestamp(fields.get("created_from"), optional=True)
        end = _timestamp(fields.get("created_to"), optional=True)
        if start and end and start >= end:
            raise ValueError("invalid_review_time_range")
        conditions, parameters = ["1=1"], []
        if instrument:
            market, ticker = instrument.split(":", 1)
            conditions.append("json_extract(r.payload_json,'$.instrument.market')=? AND json_extract(r.payload_json,'$.instrument.ticker')=?")
            parameters.extend([market, ticker])
        if status:
            conditions.append("COALESCE(v.status,'pending')=?")
            parameters.append(status)
        if start:
            conditions.append("julianday(r.created_at)>=julianday(?)")
            parameters.append(start)
        if end:
            conditions.append("julianday(r.created_at)<julianday(?)")
            parameters.append(end)
        base = " FROM research_reports r LEFT JOIN research_report_reviews v ON v.report_id=r.id WHERE " + " AND ".join(conditions)
        with self._lock:
            total = self._connection.execute("SELECT count(*)" + base, parameters).fetchone()[0]
            rows = self._connection.execute("SELECT r.payload_json" + base +
                                            " ORDER BY julianday(r.created_at) DESC,r.id LIMIT ? OFFSET ?",
                                            [*parameters, limit, offset]).fetchall()
            items: list[dict[str, Any]] = []
            payload_bytes = 0
            for row in rows:
                report = ResearchReport.model_validate_json(row[0])
                review = self._connection.execute("SELECT * FROM research_report_reviews WHERE report_id=?", (report.id,)).fetchone()
                last = self._connection.execute("SELECT payload_json FROM research_review_replays WHERE report_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (report.id,)).fetchone()
                item = {"report_id": report.id, "instrument": report.instrument.model_dump(mode="json"),
                              "instrument_key": report.instrument.key, "horizon": report.horizon,
                              "created_at": report.created_at.isoformat(), "valid_until": report.valid_until.isoformat(),
                              "original_action": report.action, "original_status": report.status,
                              "summary": report.summary[:2000], "review": _review_row(review, report.id),
                              "last_followup": json.loads(last[0]) if last else None}
                item_bytes = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                if items and payload_bytes + item_bytes > 500_000:
                    break
                items.append(item)
                payload_bytes += item_bytes
        return {"items": items, "offset": offset, "limit": limit, "total": total,
                "has_more": offset + len(items) < total,
                "filters": {"instrument": instrument, "status": status or "", "created_from": start, "created_to": end}}

    def import_samples(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        label = _text(fields["source_label"], limit=160)
        content = _text(fields["json_text"], limit=MAX_IMPORT_BYTES)
        if len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ValueError("review_samples_too_large")
        try:
            raw = json.loads(content)
        except (ValueError, RecursionError):
            raise ValueError("invalid_review_samples_json") from None
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SAMPLES:
            raise ValueError("review_samples_require_bounded_array")
        samples = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("invalid_review_price_sample")
            value = dict(item)
            for key in ("source_as_of", "observed_at"):
                normalized = _timestamp(value.get(key))
                value[key] = parse_time(normalized)
            try:
                sample = PriceObservation.model_validate(value)
            except ValueError:
                raise ValueError("invalid_review_price_sample") from None
            samples.append(sample.model_dump(mode="json"))
        payload = json.dumps(samples, ensure_ascii=False, sort_keys=True, allow_nan=False)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        identity = uuid.uuid4().hex
        imported = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            self._connection.execute("INSERT INTO research_review_samples VALUES (?,?,?,?,?,?)",
                                     (identity, label, imported, len(samples), fingerprint, payload))
        return {"id": identity, "source_label": label, "imported_at": imported,
                "sample_count": len(samples), "content_sha256": fingerprint}

    def list_samples(self) -> dict[str, Any]:
        rows = self._connection.execute("SELECT id,source_label,imported_at,sample_count,content_sha256 "
                                        "FROM research_review_samples ORDER BY imported_at DESC,id DESC LIMIT 51").fetchall()
        return {"items": [dict(row) for row in rows[:50]], "truncated": len(rows) > 50}

    def run_followup(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        report_id = _text(fields["report_id"], limit=128)
        set_id = _text(fields["sample_set_id"], limit=64)
        cutoff = parse_time(_timestamp(fields["as_of"]))
        if cutoff is None or cutoff > datetime.now(UTC):
            raise ValueError("review_cutoff_must_not_be_future")
        report = self.original_report(report_id)
        sample_set = self._connection.execute("SELECT * FROM research_review_samples WHERE id=?", (set_id,)).fetchone()
        if sample_set is None:
            raise ValueError("review_samples_not_found")
        self._followup_report_id = report_id
        try:
            result = track_reports(self, json.loads(sample_set["payload_json"]), as_of=cutoff, report_limit=1)
        finally:
            self._followup_report_id = None
        if not result["rows"]:
            raise ValueError("review_cutoff_precedes_report")
        row = result["rows"][0]
        row["baseline_source_as_of"] = report.market_context.get("source_as_of")
        row["baseline_observed_at"] = report.market_context.get("observed_at")
        row["baseline_provider"] = report.market_context.get("provider")
        row["reference_price"] = report.reference_price
        payload = {"id": uuid.uuid4().hex, "report_id": report_id, "sample_set_id": set_id,
                   "source_label": sample_set["source_label"], "sample_count": sample_set["sample_count"],
                   "content_sha256": sample_set["content_sha256"], "imported_at": sample_set["imported_at"],
                   "created_at": datetime.now(UTC).isoformat(), "as_of": cutoff.isoformat(),
                   "result": row, "limitations": result["limitations"]}
        with self._lock, self._connection:
            self._connection.execute("INSERT INTO research_review_replays VALUES (?,?,?,?,?)",
                                     (payload["id"], report_id, set_id, payload["created_at"],
                                      json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        return payload


SHAPES: dict[str, tuple[set[str], set[str]]] = {
    "review_list": (set(), {"instrument", "status", "created_from", "created_to", "offset", "limit"}),
    "review_save": ({"report_id", "status", "notes", "next_review_at", "revision"}, set()),
    "review_detail": ({"report_id"}, {"offset"}),
    "replay_import": ({"source_label", "json_text"}, set()),
    "replay_run": ({"report_id", "sample_set_id", "as_of"}, set()),
    "replay_list": (set(), set()),
}


def execute_review(database: Path, command: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(command, str) or command not in SHAPES or not isinstance(fields, Mapping):
        raise ValueError("unsupported_review_command")
    required, optional = SHAPES[command]
    if not required <= set(fields) or set(fields) - required - optional:
        raise ValueError("invalid_review_command_fields")
    with ReviewStore(database) as store:
        if command == "review_list":
            return store.list_reviews(fields)
        if command == "review_save":
            return store.save_review(fields)
        if command == "replay_import":
            return store.import_samples(fields)
        if command == "replay_run":
            return store.run_followup(fields)
        if command == "replay_list":
            return store.list_samples()
        identity = _text(fields["report_id"], limit=128)
        offset = _integer(fields.get("offset", 0), 0, 10_000_000)
        report = store.original_report(identity)
        content = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
        review = store._connection.execute("SELECT * FROM research_report_reviews WHERE report_id=?", (identity,)).fetchone()
        last = store._connection.execute("SELECT payload_json FROM research_review_replays WHERE report_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (identity,)).fetchone()
        return {"report_id": identity, "original": True, "content": content[offset:offset + DETAIL_CHARS],
                "generation_context": report.generation_context.model_dump(mode="json") if report.generation_context else None,
                "offset": offset, "next_offset": offset + DETAIL_CHARS if offset + DETAIL_CHARS < len(content) else None,
                "instrument": report.instrument.model_dump(mode="json"), "review": _review_row(review, identity),
                "last_followup": json.loads(last[0]) if last else None}
