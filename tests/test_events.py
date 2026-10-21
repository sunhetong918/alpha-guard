from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from research import event_sources, events
from research.events import EventStore, InvestmentEvent, execute_events, stop_event_workers
from research.models import ResearchInstrument
from research.network import ContentAccessError, FetchedContent
from research.store import ResearchStore

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def event(**overrides):
    value = {
        "instrument": {"market": "US", "ticker": "AAPL"}, "event_type": "earnings",
        "source": "company-ir", "source_event_id": "fy2026-q4", "title": "第四季度财报",
        "source_url": "https://example.org/results", "event_date": "2026-10-30",
        "event_timezone": "America/New_York",
    }
    value.update(overrides)
    return value


def wait_complete(database):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = execute_events(database, "event_status", {})
        if not status["running"]:
            return status
        time.sleep(.01)
    raise AssertionError("event worker did not complete")


def test_old_database_is_backed_up_and_records_survive_reopen(tmp_path):
    database = tmp_path / "state.db"
    with ResearchStore(database):
        pass
    with EventStore(database) as store:
        assert store.backup_path and store.backup_path.exists()
        with sqlite3.connect(store.backup_path) as backup:
            assert backup.execute("SELECT 1 FROM sqlite_master WHERE name='research_event_heads'").fetchone() is None
        store.save_events([event()], now=NOW)
    with EventStore(database) as store:
        assert store.list_events({})["total"] == 1
        assert store.backup_path is None


def test_date_revision_keeps_identity_history_and_duplicate_clocks(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        first = store.save_events([event()], now=NOW)["events"][0]
        duplicate = store.save_events([event(observed_at=(NOW + timedelta(hours=1)).isoformat())], now=NOW + timedelta(hours=1))
        assert duplicate["changed"] == 0
        assert duplicate["events"][0]["observed_at"] == first["observed_at"]
        second = store.save_events([event(event_date="2026-11-02", status="confirmed", observed_at=(NOW + timedelta(hours=2)).isoformat())], now=NOW + timedelta(hours=2))["events"][0]
        assert second["event_id"] == first["event_id"] and second["version"] == 2
        assert second["event_at"] is None
        history = store.history(first["event_id"])
        assert [row["event_date"] for row in history["events"]] == ["2026-11-02", "2026-10-30"]
        assert history["events"][1]["observed_at"] == first["observed_at"]


def test_stale_revision_cannot_replace_newer_and_batch_is_atomic(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        store.save_events([event()], now=NOW)
        with pytest.raises(ValueError, match="older"):
            store.save_events([
                event(source_event_id="other"),
                event(status="cancelled", observed_at=(NOW - timedelta(hours=1)).isoformat()),
            ], now=NOW)
        result = store.list_events({})
        assert result["total"] == 1 and result["events"][0]["status"] == "expected"


@pytest.mark.parametrize("change", [
    {"event_at": "2026-10-30T15:00:00"},
    {"event_at": "2026-10-31T02:00:00Z", "event_timezone": "UTC"},
    {"source_url": "http://localhost/events"},
    {"source_url": "https://example.org/events?token=secret"},
    {"event_timezone": "Not/AZone"},
    {"event_type": "dividend"},
    {"event_date": 123},
    {"event_date": "2026-10-30T00:00:00Z"},
    {"event_date": "2300-10-30"},
    {"event_at": 1793350800},
    {"event_type": "dividend", "date_kind": "payment", "dividend_dates": {"payment": "2026-10-30", "record": "9999-10-30"}},
    {"event_type": "dividend", "date_kind": "payment", "dividend_dates": {"payment": "2026-10-30T00:00:00Z"}},
    {"amount": True, "currency": "USD"},
    {"amount": float("inf"), "currency": "USD"},
])
def test_invalid_event_semantics(change):
    with pytest.raises(ValueError):
        InvestmentEvent.model_validate(event(**change))


def test_exact_time_uses_exchange_date_and_dividend_dates_stay_distinct(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        result = store.save_events([event(event_at="2026-10-31T02:00:00Z")], now=NOW)
        assert result["events"][0]["event_date"] == "2026-10-30"
        dividend = event(event_type="dividend", date_kind="ex_dividend", dividend_dates={
            "declaration": "2026-10-01", "ex_dividend": "2026-10-30", "record": "2026-11-02", "payment": "2026-11-10",
        })
        saved = store.save_events([dividend], now=NOW)["events"][0]
        assert len(set(saved["dividend_dates"].values())) == 4
        with pytest.raises(ValueError, match="mismatch"):
            store.save_events([{**dividend, "date_kind": "payment"}], now=NOW)


def test_input_limits_future_clocks_and_reserved_automatic_source(tmp_path):
    db = tmp_path / "state.db"
    for fields in ({"offset": True}, {"limit": 21}, {"limit": 0}, {"date_from": "2026-12-01", "date_to": "2026-10-01"}):
        with pytest.raises(ValueError):
            execute_events(db, "event_list", fields)
    for values in ([], [event()] * 101, [event()] * 2, [event(source="yahoo_chart")]):
        with pytest.raises(ValueError):
            execute_events(db, "event_import", {"items": values})
    with pytest.raises(ValueError, match="future"):
        with EventStore(db) as store:
            store.save_events([event(observed_at=(NOW + timedelta(days=1)).isoformat())], now=NOW)
    with pytest.raises(ValueError, match="future"):
        with EventStore(db) as store:
            store.save_events([event(published_at=(NOW + timedelta(days=1)).isoformat())], now=NOW)
    with pytest.raises(ValueError, match="too_large"):
        execute_events(db, "event_import", {"items": " " * 120001})


def test_filters_pagination_and_reads_never_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "fetch_events", lambda *a, **k: pytest.fail("unexpected network"))
    db = tmp_path / "state.db"
    rows = [event(source_event_id=f"e{i}", event_date=f"2026-10-{i+1:02d}") for i in range(25)]
    execute_events(db, "event_import", {"items": json.dumps(rows)})
    one = execute_events(db, "event_list", {"instrument": "US:AAPL", "date_from": "2026-10-03", "event_type": "earnings", "limit": 10})
    two = execute_events(db, "event_list", {"instrument": "US:AAPL", "date_from": "2026-10-03", "event_type": "earnings", "limit": 10, "offset": 10})
    assert one["total"] == 23 and len(two["events"]) == 10
    assert not {v["event_id"] for v in one["events"]} & {v["event_id"] for v in two["events"]}
    assert execute_events(db, "event_list", {"instrument": "HK:00700"})["total"] == 0
    assert execute_events(db, "event_status", {})["sources"] == []


def test_refresh_returns_immediately_single_worker_and_failure_preserves_data(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    execute_events(database, "event_save", {"event": event()})
    entered, release = threading.Event(), threading.Event()
    def failing(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        raise ContentAccessError("rate_limited")
    monkeypatch.setattr(events, "fetch_events", failing)
    start = time.monotonic()
    response = execute_events(database, "event_refresh", {"instrument": "US:AAPL"})
    assert time.monotonic() - start < 1
    assert response["status"] == "running" and entered.wait(1)
    assert execute_events(database, "event_refresh", {"instrument": "US:MSFT"})["already_running"]
    release.set()
    state = wait_complete(database)["sources"][0]
    assert state["status"] == "failed" and state["detail"] == "rate_limited"
    assert state["last_success_at"] is None
    assert execute_events(database, "event_list", {})["total"] == 1


def test_refresh_empty_is_success_and_keeps_previous_events(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    execute_events(database, "event_save", {"event": event()})
    monkeypatch.setattr(events, "fetch_events", lambda *a, **k: [])
    execute_events(database, "event_refresh", {"instrument": "HK:00700"})
    state = wait_complete(database)["sources"][0]
    assert state["status"] == "empty" and state["count"] == 0 and state["last_success_at"]
    assert execute_events(database, "event_list", {})["total"] == 1


def test_cancel_after_source_returns_cannot_publish_rows(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    entered, release = threading.Event(), threading.Event()
    def fetch(*args, **kwargs):
        entered.set()
        release.wait(2)
        return [event(source="yahoo_chart")]
    monkeypatch.setattr(events, "fetch_events", fetch)
    execute_events(database, "event_refresh", {"instrument": "US:AAPL"})
    assert entered.wait(1)
    stop_event_workers(database)
    release.set()
    assert wait_complete(database)["sources"][0]["status"] == "cancelled"
    assert execute_events(database, "event_list", {})["total"] == 0


def test_expired_source_lease_is_interrupted_and_can_retry(tmp_path):
    database = tmp_path / "state.db"
    with EventStore(database) as store:
        state = store.claim_refresh(ResearchInstrument(market="US", ticker="AAPL"))
        state["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with store._connection:
            store._connection.execute("UPDATE research_event_sources SET payload_json=?", (json.dumps(state),))
        assert store.source_status()["sources"][0]["status"] == "interrupted"
        assert store.claim_refresh(ResearchInstrument(market="US", ticker="AAPL"))["run_id"] != state["run_id"]


def chart(actions=None, **meta):
    return {"chart": {"error": None, "result": [{"meta": {"symbol": "AAPL", "exchangeTimezoneName": "America/New_York", "currency": "USD", **meta}, "events": actions or {}}]}}


def test_public_source_bounded_date_only_and_dividend_ex_date(monkeypatch):
    stamp = int(datetime(2026, 10, 1, 4, tzinfo=UTC).timestamp())
    payload = chart({"dividends": {"provider-stable-1": {"date": stamp, "amount": .25}}, "splits": {"provider-stable-2": {"date": stamp, "numerator": 2, "denominator": 1}}})
    calls = []
    def read(url, **kwargs):
        calls.append((url, kwargs))
        return FetchedContent(url, json.dumps(payload), "application/json")
    monkeypatch.setattr(event_sources, "safe_fetch", read)
    rows = event_sources.fetch_events(ResearchInstrument(market="US", ticker="AAPL"), now=NOW)
    assert len(calls) == 1 and calls[0][1]["timeout"] == 10 and calls[0][1]["max_bytes"] == 1_000_000
    assert rows[0]["event_at"] is None and rows[0]["event_date"] == "2026-10-01"
    assert rows[0]["date_kind"] == "ex_dividend" and rows[0]["published_at"] is None
    assert rows[0]["source_event_id"] == "provider-stable-1"
    assert rows[1]["event_type"] == "split"


@pytest.mark.parametrize("payload,code", [
    ({"chart": {"error": {"description": "not found"}}}, "event_source_rejected"),
    (chart(symbol="MSFT"), "identity_mismatch"),
    (chart(exchangeTimezoneName=None), "timezone_unknown"),
    (chart({"dividends": {"x": {"date": 9999999999, "amount": 1}}}), "outside_history"),
])
def test_public_source_errors_are_not_empty(monkeypatch, payload, code):
    monkeypatch.setattr(event_sources, "safe_fetch", lambda url, **kw: FetchedContent(url, json.dumps(payload)))
    with pytest.raises(ContentAccessError, match=code):
        event_sources.fetch_events(ResearchInstrument(market="US", ticker="AAPL"), now=NOW)


def test_unsupported_cn_refresh_never_fetches(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "fetch_events", lambda *a, **k: pytest.fail("unexpected network"))
    with pytest.raises(ValueError, match="unsupported"):
        execute_events(tmp_path / "state.db", "event_refresh", {"instrument": "CN:600519"})


def test_successful_source_save_and_subsequent_failure_preserve_last_success(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    automatic = event(event_type="dividend", source="yahoo_chart", source_event_id="original-provider-id",
                      event_date="2026-10-01", date_kind="ex_dividend",
                      dividend_dates={"ex_dividend": "2026-10-01"}, status="confirmed")
    monkeypatch.setattr(events, "fetch_events", lambda *a, **k: [automatic])
    execute_events(database, "event_refresh", {"instrument": "US:AAPL"})
    first = wait_complete(database)["sources"][0]
    assert first["status"] == "success" and first["count"] == 1 and first["last_success_at"]
    saved = execute_events(database, "event_list", {})["events"][0]
    assert saved["provenance"] == "automatic" and saved["version"] == 1
    def failed(*args, **kwargs):
        raise ContentAccessError("request_timeout")
    monkeypatch.setattr(events, "fetch_events", failed)
    execute_events(database, "event_refresh", {"instrument": "US:AAPL"})
    last = wait_complete(database)["sources"][0]
    assert last["status"] == "failed" and last["last_success_at"] == first["last_success_at"]
    assert last["count"] == 1
    assert execute_events(database, "event_list", {})["events"] == [saved]


def test_identical_later_observation_blocks_late_changed_revision(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        original = store.save_events([event()], now=NOW - timedelta(days=14))["events"][0]
        duplicate = store.save_events([event()], now=NOW - timedelta(days=5))
        assert duplicate["changed"] == 0 and duplicate["events"] == [original]
        with pytest.raises(ValueError, match="older"):
            store.save_events([event(event_date="2026-11-02", observed_at=(NOW - timedelta(days=10)).isoformat())], now=NOW)
        assert store.history(original["event_id"])["total"] == 1
        assert store.list_events({})["events"] == [original]


def test_hk_identity_normalization_is_shared_by_save_list_and_refresh(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    short = {"market": "HK", "ticker": "700", "name": "Tencent", "exchange": "HKEX"}
    long = {**short, "ticker": "00700"}
    first = execute_events(database, "event_save", {"event": event(instrument=short)})["events"][0]
    duplicate = execute_events(database, "event_save", {"event": event(instrument=long)})
    assert duplicate["unchanged"] == 1 and duplicate["events"] == [first]
    assert first["instrument"] == long
    result = execute_events(database, "event_list", {"instrument": "HK:700"})
    assert result["total"] == 1 and result["filters"]["instrument"] == "HK:00700"
    seen = []
    def fetch(instrument, **kwargs):
        seen.append(instrument)
        return []
    monkeypatch.setattr(events, "fetch_events", fetch)
    execute_events(database, "event_refresh", {"instrument": short})
    status = wait_complete(database)
    assert seen[0].model_dump() == long
    assert status["sources"][0]["instrument"] == "HK:00700"


@pytest.mark.parametrize("instrument", ["HK:100000", "HK:AAPL", "CN:123", "CN:ABCDEF"])
def test_invalid_market_identity_is_rejected_on_every_boundary(tmp_path, instrument):
    database = tmp_path / "state.db"
    for command, fields in (("event_save", {"event": event(instrument=instrument)}),
                            ("event_list", {"instrument": instrument}),
                            ("event_refresh", {"instrument": instrument})):
        with pytest.raises(ValueError):
            execute_events(database, command, fields)
