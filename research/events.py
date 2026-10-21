"""Local, versioned investment events and explicitly requested background refreshes."""
from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .event_sources import COVERAGE, SOURCE, fetch_events, yahoo_symbol
from .sec_filings import SOURCE as SEC_SOURCE, COVERAGE as SEC_COVERAGE, FilingMetadata, fetch_filings
from .event_coverage import SCHEMA as COVERAGE_SCHEMA, history_range, query_coverage, record_range
from .models import ResearchInstrument, aware, digest
from .network import ContentAccessError, check_cancelled, validate_public_url
from .store import ResearchStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_event_heads (
 event_id TEXT PRIMARY KEY, instrument TEXT NOT NULL, event_type TEXT NOT NULL,
 event_date TEXT NOT NULL, version INTEGER NOT NULL, fingerprint TEXT NOT NULL,
 observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_event_lookup
 ON research_event_heads(instrument, event_date, event_type);
CREATE TABLE IF NOT EXISTS research_event_versions (
 event_id TEXT NOT NULL, version INTEGER NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(event_id, version),
 FOREIGN KEY(event_id) REFERENCES research_event_heads(event_id)
);
CREATE TABLE IF NOT EXISTS research_event_sources (
 source_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL
);
"""
_TABLES = ("research_event_heads", "research_event_versions", "research_event_sources")
_MAX_IMPORT_BYTES = 120_000
_MAX_EVENTS = 100


def _calendar_date(value: Any) -> Any:
    if value is None:
        return value
    if type(value) is not date:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("event_date_requires_calendar_date")
        value = date.fromisoformat(value)
    if not date(1900, 1, 1) <= value <= date(2200, 12, 31):
        raise ValueError("event_date_out_of_range")
    return value


def _instrument(value: Any) -> ResearchInstrument:
    if isinstance(value, str):
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("event_instrument_invalid")
        value = {"market": parts[0].strip().upper(), "ticker": parts[1].strip().upper()}
    instrument = ResearchInstrument.model_validate(value)
    ticker = instrument.ticker
    if instrument.market == "HK":
        if not re.fullmatch(r"[0-9]{1,5}", ticker):
            raise ValueError("event_hk_ticker_requires_1_to_5_digits")
        instrument = instrument.model_copy(update={"ticker": ticker.zfill(5)})
    elif instrument.market == "CN" and not re.fullmatch(r"[0-9]{6}", ticker):
        raise ValueError("event_cn_ticker_requires_6_digits")
    return instrument


class DividendDates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    declaration: date | None = None
    ex_dividend: date | None = None
    record: date | None = None
    payment: date | None = None

    @field_validator("declaration", "ex_dividend", "record", "payment", mode="before")
    @classmethod
    def strict_dates(cls, value: Any) -> Any:
        return _calendar_date(value)


class InvestmentEvent(BaseModel):
    """Source identity stays stable when a scheduled date or status changes."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    instrument: ResearchInstrument
    event_type: Literal["earnings", "announcement", "dividend", "split"]
    source: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    source_event_id: str = Field(min_length=1, max_length=160)
    source_url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=300)
    event_date: date
    event_at: datetime | None = None
    event_timezone: str = Field(default="UTC", min_length=1, max_length=64)
    date_kind: Literal["event", "declaration", "ex_dividend", "record", "payment"] = "event"
    dividend_dates: DividendDates | None = None
    status: Literal["expected", "confirmed", "cancelled"] = "expected"
    published_at: datetime | None = None
    observed_at: datetime | None = None
    amount: float | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    notes: str = Field(default="", max_length=2000)
    filing: FilingMetadata | None = None

    @field_validator("instrument", mode="before")
    @classmethod
    def canonical_instrument(cls, value: Any) -> ResearchInstrument:
        return _instrument(value)

    @field_validator("source_event_id", "title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("event_text_invalid")
        return value.strip()

    @field_validator("event_date", mode="before")
    @classmethod
    def strict_date(cls, value: Any) -> Any:
        return _calendar_date(value)

    @field_validator("amount", mode="before")
    @classmethod
    def amount_number(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("event_amount_invalid")
        return value

    @field_validator("event_at", "published_at", "observed_at", mode="before")
    @classmethod
    def timestamp_input(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("event_time_requires_iso_timestamp")
        return value

    @field_validator("event_at", "published_at", "observed_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        return aware(value) if value is not None else None

    @field_validator("source_url")
    @classmethod
    def source_link(cls, value: str) -> str:
        canonical, _ = validate_public_url(value, resolve=False)
        if any(k.casefold() in {"token", "access_token", "api_key", "apikey", "key", "authorization", "secret", "password"}
               for k, _ in parse_qsl(urlsplit(canonical).query)):
            raise ValueError("event_source_url_contains_credentials")
        return canonical

    @model_validator(mode="after")
    def coherent_event(self) -> Self:
        try:
            zone = ZoneInfo(self.event_timezone)
        except (KeyError, ValueError):
            raise ValueError("event_timezone_invalid") from None
        if self.event_at is not None and self.event_at.astimezone(zone).date() != self.event_date:
            raise ValueError("event_date_and_time_disagree")
        if self.amount is not None and self.currency is None:
            raise ValueError("event_amount_requires_currency")
        if self.event_type == "dividend":
            if self.date_kind == "event" or self.dividend_dates is None:
                raise ValueError("dividend_date_kind_required")
            if getattr(self.dividend_dates, self.date_kind) != self.event_date:
                raise ValueError("dividend_selected_date_mismatch")
        elif self.dividend_dates is not None or self.date_kind != "event" or self.amount is not None:
            raise ValueError("dividend_fields_on_non_dividend")
        if self.filing is not None and (self.source != SEC_SOURCE or self.event_type != "announcement"
                or self.source_event_id != self.filing.accession):
            raise ValueError("filing_identity_invalid")
        return self


def _page(fields: Mapping[str, Any]) -> tuple[int, int]:
    offset, limit = fields.get("offset", 0), fields.get("limit", 20)
    if type(offset) is not int or not 0 <= offset <= 100_000 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("event_pagination_invalid")
    return offset, limit


class EventStore(ResearchStore):
    def __init__(self, path: str | Path = "alpha_guard.db") -> None:
        super().__init__(path)
        self.ensure_feature_schema(_SCHEMA + COVERAGE_SCHEMA, (*_TABLES, "research_event_coverage"))

    def __enter__(self) -> Self:
        return self

    def save_events(self, values: Sequence[Mapping[str, Any]], *, provenance: str = "manual",
                    now: datetime | None = None) -> dict[str, Any]:
        now = aware(now or datetime.now(UTC))
        if not values or len(values) > _MAX_EVENTS:
            raise ValueError("event_batch_size_invalid")
        if len(json.dumps(values, ensure_ascii=False, default=str).encode()) > _MAX_IMPORT_BYTES:
            raise ValueError("event_import_too_large")
        parsed = [InvestmentEvent.model_validate(value) for value in values]
        records = []
        identities = set()
        for item in parsed:
            if provenance != "automatic" and item.source in {SOURCE, SEC_SOURCE}:
                raise ValueError("event_source_reserved")
            observed = item.observed_at or now
            if observed > now + timedelta(seconds=60) or (item.published_at is not None and item.published_at > observed + timedelta(seconds=60)):
                raise ValueError("event_observation_in_future")
            key = digest([item.source, item.source_event_id, item.instrument.key, item.event_type])
            if key in identities:
                raise ValueError("duplicate_event_identity_in_batch")
            identities.add(key)
            content = item.model_dump(mode="json", exclude={"observed_at"})
            if item.filing is None:
                content.pop("filing", None)
            records.append((key, item, observed, content, digest(content)))
        saved: list[dict[str, Any]] = []
        changed = 0
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for key, item, observed, content, fingerprint in records:
                    current = self._connection.execute(
                        "SELECT version, fingerprint, observed_at FROM research_event_heads WHERE event_id=?", (key,),
                    ).fetchone()
                    if current and current["fingerprint"] == fingerprint:
                        # Keep the version's first observation immutable, while
                        # advancing the head's last-seen watermark against late revisions.
                        if observed > datetime.fromisoformat(current["observed_at"]):
                            self._connection.execute(
                                "UPDATE research_event_heads SET observed_at=? WHERE event_id=?",
                                (observed.isoformat(), key),
                            )
                        row = self._connection.execute(
                            "SELECT payload_json FROM research_event_versions WHERE event_id=? AND version=?",
                            (key, current["version"]),
                        ).fetchone()
                        saved.append(json.loads(row[0]))
                        continue
                    if current and observed < datetime.fromisoformat(current["observed_at"]):
                        raise ValueError("event_revision_is_older_than_current")
                    version = int(current["version"]) + 1 if current else 1
                    payload = {**content, "observed_at": observed.isoformat(), "event_id": key,
                               "version": version, "provenance": provenance}
                    self._connection.execute(
                        "INSERT INTO research_event_heads VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(event_id) DO UPDATE SET event_date=excluded.event_date, "
                        "version=excluded.version, fingerprint=excluded.fingerprint, observed_at=excluded.observed_at",
                        (key, item.instrument.key, item.event_type, item.event_date.isoformat(), version, fingerprint, observed.isoformat()),
                    )
                    self._connection.execute("INSERT INTO research_event_versions VALUES (?, ?, ?)",
                                             (key, version, json.dumps(payload, ensure_ascii=False)))
                    saved.append(payload)
                    changed += 1
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return {"saved": len(saved), "changed": changed, "unchanged": len(saved) - changed,
                "events": saved[:20]}

    def list_events(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        offset, limit = _page(fields)
        clauses, args = [], []
        filters: dict[str, Any] = {}
        if fields.get("instrument"):
            key = _instrument(fields["instrument"]).key
            clauses.append("h.instrument=?")
            args.append(key)
            filters["instrument"] = key
        event_type = fields.get("event_type")
        if event_type:
            if event_type not in {"earnings", "announcement", "dividend", "split"}:
                raise ValueError("event_type_invalid")
            clauses.append("h.event_type=?")
            args.append(event_type)
            filters["event_type"] = event_type
        for key, operator in (("date_from", ">="), ("date_to", "<=")):
            if fields.get(key):
                value = date.fromisoformat(str(fields[key])).isoformat()
                clauses.append(f"h.event_date{operator}?")
                args.append(value)
                filters[key] = value
        if filters.get("date_from", "") > filters.get("date_to", "9999-12-31"):
            raise ValueError("event_date_range_invalid")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            total = self._connection.execute("SELECT count(*) FROM research_event_heads h" + where, args).fetchone()[0]
            rows = self._connection.execute(
                "SELECT v.payload_json FROM research_event_heads h JOIN research_event_versions v "
                "ON h.event_id=v.event_id AND h.version=v.version" + where +
                " ORDER BY h.event_date DESC, h.event_id LIMIT ? OFFSET ?", [*args, limit, offset],
            ).fetchall()
        coverage = query_coverage(self._connection, _instrument(filters["instrument"]) if filters.get("instrument") else None,
                                  event_type=filters.get("event_type"), date_from=filters.get("date_from"),
                                  date_to=filters.get("date_to"))
        return {"events": [json.loads(row[0]) for row in rows], "total": total,
                "offset": offset, "limit": limit, "filters": filters, "coverage": COVERAGE,
                "query_coverage": coverage,
                "filing_coverage": query_coverage(self._connection, _instrument(filters["instrument"]) if filters.get("instrument") else None,
                    event_type=filters.get("event_type") or "announcement", date_from=filters.get("date_from"),
                    date_to=filters.get("date_to"), source=SEC_SOURCE)}

    def history(self, event_id: str, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        if not isinstance(event_id, str) or len(event_id) != 64:
            raise ValueError("event_id_invalid")
        _page({"offset": offset, "limit": limit})
        with self._lock:
            total = self._connection.execute("SELECT count(*) FROM research_event_versions WHERE event_id=?", (event_id,)).fetchone()[0]
            rows = self._connection.execute(
                "SELECT payload_json FROM research_event_versions WHERE event_id=? ORDER BY version DESC LIMIT ? OFFSET ?",
                (event_id, limit, offset),
            ).fetchall()
        return {"event_id": event_id, "events": [json.loads(row[0]) for row in rows],
                "total": total, "offset": offset, "limit": limit}

    def source_status(self) -> dict[str, Any]:
        with self._lock:
            rows = self._connection.execute("SELECT payload_json FROM research_event_sources ORDER BY source_key LIMIT 200").fetchall()
        now = datetime.now(UTC)
        states = [json.loads(row[0]) for row in rows]
        for state in states:
            if state["status"] == "running" and datetime.fromisoformat(state["expires_at"]) < now:
                state.update(status="interrupted", detail="refresh_interrupted")
        return {"sources": states, "running": any(s["status"] == "running" for s in states), "coverage": COVERAGE}

    def claim_refresh(self, instrument: ResearchInstrument, *, source: str = SOURCE) -> dict[str, Any]:
        now = datetime.now(UTC)
        date_from, date_to = history_range(instrument, now)
        if source not in {SOURCE, SEC_SOURCE}:
            raise ValueError("event_source_invalid")
        kinds = ["announcement"] if source == SEC_SOURCE else ["dividend", "split"]
        key = f"{source}:{instrument.key}"
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                states = self.source_status()["sources"]
                running = next((state for state in states if state["status"] == "running"), None)
                if running:
                    self._connection.commit()
                    return {**running, "already_running": True}
                previous: dict[str, Any] = next((state for state in states if state["source_key"] == key), {})
                state: dict[str, Any] = {"source_key": key, "instrument": instrument.key, "source": source,
                         "run_id": uuid.uuid4().hex, "status": "running", "detail": "",
                         "started_at": now.isoformat(), "finished_at": None,
                         "expires_at": (now + timedelta(seconds=30)).isoformat(),
                         "last_success_at": previous.get("last_success_at"), "count": previous.get("count"),
                         "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                         "event_types": kinds,
                         "coverage": SEC_COVERAGE if source == SEC_SOURCE else COVERAGE}
                self._connection.execute("INSERT INTO research_event_sources VALUES (?, ?) "
                                         "ON CONFLICT(source_key) DO UPDATE SET payload_json=excluded.payload_json",
                                         (key, json.dumps(state, ensure_ascii=False)))
                for kind in state["event_types"]:
                    record_range(self._connection, run_id=state["run_id"], instrument=instrument.key,
                                 event_type=kind, date_from=date_from, date_to=date_to,
                                 status="running", checked_at=now, source=source)
                self._connection.commit()
                return state
            except Exception:
                self._connection.rollback()
                raise

    def finish_refresh(self, state: dict[str, Any], *, status: str, detail: str = "", count: int | None = None,
                       type_counts: Mapping[str, int] | None = None) -> None:
        now = datetime.now(UTC)
        with self._lock, self._connection:
            row = self._connection.execute("SELECT payload_json FROM research_event_sources WHERE source_key=?", (state["source_key"],)).fetchone()
            if not row or json.loads(row[0])["run_id"] != state["run_id"] or json.loads(row[0])["status"] != "running":
                return
            value = {**state, "status": status, "finished_at": now.isoformat(), "detail": detail}
            if status in {"success", "empty"}:
                value.update(last_success_at=now.isoformat(), count=count)
            self._connection.execute("UPDATE research_event_sources SET payload_json=? WHERE source_key=?",
                                     (json.dumps(value, ensure_ascii=False), state["source_key"]))
            if state.get("date_from") and state.get("date_to"):
                for kind in state["event_types"]:
                    # No counts means a legacy caller, not proof of complete type coverage.
                    if status in {"success", "empty"} and type_counts is None:
                        continue
                    kind_count = type_counts.get(kind) if type_counts is not None else None
                    record_range(self._connection, run_id=state["run_id"], instrument=state["instrument"],
                                 event_type=kind, date_from=date.fromisoformat(state["date_from"]),
                                 date_to=date.fromisoformat(state["date_to"]),
                                 status=("empty" if kind_count == 0 else "success") if status in {"success", "empty"} else status,
                                 checked_at=now, count=kind_count, detail=detail, source=state["source"])


_worker_lock = threading.RLock()
_workers: dict[str, tuple[threading.Thread, threading.Event]] = {}


def _database_key(path: Path) -> str:
    return str(path.expanduser().resolve())


def _refresh_worker(database: Path, instrument: ResearchInstrument, state: dict[str, Any], cancel: threading.Event) -> None:
    try:
        with EventStore(database) as store:
            try:
                fetch = fetch_filings if state["source"] == SEC_SOURCE else fetch_events
                rows = fetch(instrument, now=datetime.fromisoformat(state["started_at"]), cancelled=cancel.is_set)
                check_cancelled(cancel.is_set)
                # The returned records may include boundary-day rows; only this
                # declared query range can be marked as checked.
                rows = [row for row in rows if state["date_from"] <= str(row.get("event_date", "")) <= state["date_to"]]
                if rows:
                    store.save_events(rows, provenance="automatic")
                check_cancelled(cancel.is_set)
                store.finish_refresh(state, status="success" if rows else "empty", count=len(rows),
                                     type_counts={kind: sum(row.get("event_type") == kind for row in rows)
                                                  for kind in state["event_types"]})
            except Exception as exc:
                detail = str(exc) if isinstance(exc, ContentAccessError) and str(exc) in {
                    "cancelled", "request_timeout", "rate_limited", "permission_required", "http_error",
                    "event_source_invalid", "event_source_rejected", "event_source_identity_mismatch",
                    "event_source_timezone_unknown", "event_source_outside_history",
                } else "event_refresh_failed"
                store.finish_refresh(state, status="cancelled" if cancel.is_set() else "failed", detail=detail)
    finally:
        with _worker_lock:
            current = _workers.get(_database_key(database))
            if current and current[0] is threading.current_thread():
                _workers.pop(_database_key(database), None)


def stop_event_workers(database: str | Path) -> None:
    """Signal shutdown; a bounded source request cannot hold Guardian shutdown."""
    with _worker_lock:
        current = _workers.get(_database_key(Path(database)))
        if current:
            current[1].set()
    if current and current[0] is not threading.current_thread():
        current[0].join(timeout=0.2)


def execute_events(database: Path, command: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Local result DTO for IPC; only event_refresh can initiate network work."""
    shapes = {
        "event_list": (set(), {"instrument", "event_type", "date_from", "date_to", "offset", "limit"}),
        "event_history": ({"event_id"}, {"offset", "limit"}),
        "event_save": ({"event"}, set()), "event_import": ({"items"}, set()),
        "event_refresh": ({"instrument"}, {"source"}), "event_status": (set(), set()),
        "event_stop": (set(), set()),
    }
    if command not in shapes:
        raise ValueError("event_command_invalid")
    required, optional = shapes[command]
    if not required <= set(fields) or set(fields) - required - optional:
        raise ValueError("event_command_fields_invalid")
    if command == "event_stop":
        stop_event_workers(database)
        return {"status": "stopping"}
    with EventStore(database) as store:
        if command == "event_list":
            return store.list_events(fields)
        if command == "event_history":
            offset, limit = _page(fields)
            return store.history(fields["event_id"], offset=offset, limit=limit)
        if command == "event_status":
            return store.source_status()
        if command in {"event_save", "event_import"}:
            values = [fields["event"]] if command == "event_save" else fields["items"]
            if isinstance(values, str):
                if len(values.encode()) > _MAX_IMPORT_BYTES:
                    raise ValueError("event_import_too_large")
                values = json.loads(values)
            if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
                raise ValueError("event_import_requires_array")
            return store.save_events(values, provenance="manual" if command == "event_save" else "import")
        instrument = _instrument(fields["instrument"])
        source = fields.get("source", SOURCE)
        if source == SEC_SOURCE:
            if instrument.market != "US":
                raise ValueError("sec_market_unsupported")
        elif source == SOURCE:
            yahoo_symbol(instrument)
        else:
            raise ValueError("event_source_invalid")
        with _worker_lock:
            current = _workers.get(_database_key(database))
            if current and current[0].is_alive():
                return {"status": "running", "already_running": True}
            state = store.claim_refresh(instrument, source=source)
            if state.get("already_running"):
                return state
            cancel = threading.Event()
            worker = threading.Thread(target=_refresh_worker, args=(database, instrument, state, cancel),
                                      name="investment-event-refresh", daemon=True)
            _workers[_database_key(database)] = (worker, cancel)
            worker.start()
        return state
