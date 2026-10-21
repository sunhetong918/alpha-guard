from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from research import events
from research.event_coverage import MAX_CHECKS, history_range, query_coverage, record_range
from research.events import EventStore, execute_events
from research.models import ResearchInstrument
from test_events import event, wait_complete

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
INSTRUMENT = ResearchInstrument(market="US", ticker="AAPL")


def record(store, identity, first="2026-10-13", last="2026-10-15", status="success", **extra):
    values = {"run_id": identity, "instrument": "US:AAPL", "event_type": "dividend",
              "date_from": date.fromisoformat(first), "date_to": date.fromisoformat(last),
              "status": status, "checked_at": NOW - timedelta(hours=1),
              "count": 0 if status == "empty" else 1 if status == "success" else None, **extra}
    with store._connection:
        record_range(store._connection, **values)


def query(store, **extra):
    fields = {"instrument": INSTRUMENT, "date_from": "2026-10-13", "date_to": "2026-10-15",
              "event_type": "dividend", "now": NOW, **extra}
    return query_coverage(store._connection, **fields)


def test_unknown_unsupported_and_missing_selection_are_not_empty(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        assert query(store)["status"] == "not_queried"
        assert query(store, event_type="earnings")["status"] == "unsupported"
        assert query(store, event_type="announcement")["status"] == "unsupported"
        assert query(store, instrument=ResearchInstrument(market="CN", ticker="600519"))["status"] == "unsupported"
        assert query(store, instrument=None)["status"] == "selection_required"
        future = query(store, date_from="2026-10-17", date_to="2026-10-18")
        assert future["status"] == "unsupported"


def test_partial_days_keep_failed_range_and_previous_success(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        record(store, "previous", checked_at=NOW - timedelta(hours=3))
        record(store, "first", "2026-10-13", "2026-10-13")
        record(store, "failed", "2026-10-14", "2026-10-14", "failed", detail="rate_limited")
        record(store, "last", "2026-10-15", "2026-10-15", "empty")
        result = query(store)
        assert result["status"] == "partial"
        assert [r["status"] for r in result["ranges"]] == ["success", "failed", "empty"]
        middle = result["ranges"][1]
        assert middle["date_from"] == middle["date_to"] == "2026-10-14"
        assert middle["last_success_at"] == (NOW - timedelta(hours=3)).isoformat()
        assert middle["detail"] == "rate_limited"
        assert query(store, event_type="split")["status"] == "not_queried"
        assert query(store, instrument=ResearchInstrument(market="US", ticker="MSFT"))["status"] == "not_queried"


def test_cache_read_does_not_rejuvenate_original_clock(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        original = NOW - timedelta(days=2)
        record(store, "cached", status="empty", cache_source_at=original)
        result = query(store)
        assert result["status"] == "stale"
        row = result["ranges"][0]
        assert row["last_success_at"] == row["cache_source_at"] == original.isoformat()
        before = store._connection.total_changes
        assert query(store) == result
        assert store._connection.total_changes == before


def test_history_and_future_segments_are_not_merged(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        record(store, "past", last="2026-10-16", status="empty")
        result = query(store, date_to="2026-10-18")
        assert [row["status"] for row in result["ranges"]] == ["empty", "unsupported"]
        assert result["ranges"][1]["date_from"] == "2026-10-17"


def test_abandoned_running_range_is_failure_not_empty(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        record(store, "run", status="running", checked_at=NOW - timedelta(seconds=20))
        assert query(store)["status"] == "running"
        later = query(store, now=NOW + timedelta(seconds=20))
        assert later["status"] == "failed"
        assert later["ranges"][0]["detail"] == "refresh_interrupted"


def test_legacy_success_without_range_does_not_invent_coverage(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        store._connection.execute("INSERT INTO research_event_sources VALUES (?,?)", (
            "yahoo_chart:US:AAPL", json.dumps({"status": "success", "last_success_at": NOW.isoformat()})))
        store._connection.commit()
        assert query(store)["status"] == "not_queried"


def test_bounded_history_can_confirm_recent_ranges_but_not_hidden_gaps(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        record(store, "older", "2026-10-13", "2026-10-13", checked_at=NOW - timedelta(hours=2))
        for index in range(MAX_CHECKS + 1):
            record(store, str(index), "2026-10-14", "2026-10-15",
                   checked_at=NOW - timedelta(seconds=index + 1))
        rows = query(store)["ranges"]
        assert [row["status"] for row in rows] == ["unknown", "success"]
        assert rows[0]["detail"] == "coverage_query_limit"
        assert all(row["history_truncated"] for row in rows)


@pytest.mark.parametrize("changes", [
    {"count": None}, {"count": True}, {"status": "empty", "count": 1},
    {"date_from": date(2026, 10, 16), "date_to": date(2026, 10, 15)},
    {"cache_source_at": NOW + timedelta(days=1)}, {"event_type": "earnings"},
])
def test_unproven_coverage_cannot_be_saved(tmp_path, changes):
    with EventStore(tmp_path / "state.db") as store:
        with pytest.raises(ValueError):
            record(store, "invalid", **changes)
        assert query(store)["status"] == "not_queried"


def test_corrupt_coverage_fails_closed(tmp_path):
    with EventStore(tmp_path / "state.db") as store:
        record(store, "valid")
        store._connection.execute("UPDATE research_event_coverage SET fresh_until='invalid'")
        store._connection.commit()
        result = query(store)
        assert result["status"] == "unknown"


def test_worker_records_empty_types_separately_and_never_cancels_missing_events(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    today = history_range(INSTRUMENT, datetime.now(UTC))[1].isoformat()
    row = event(event_type="dividend", source="yahoo_chart", event_date=today,
                date_kind="ex_dividend", dividend_dates={"ex_dividend": today})
    monkeypatch.setattr(events, "fetch_events", lambda *a, **kw: [row])
    execute_events(database, "event_refresh", {"instrument": "US:AAPL"})
    wait_complete(database)
    fields = {"instrument": "US:AAPL", "date_from": today, "date_to": today}
    first = execute_events(database, "event_list", fields)
    assert first["total"] == 1
    by_type = {r["event_type"]: r["status"] for r in first["query_coverage"]["ranges"]}
    assert by_type == {"dividend": "success", "split": "empty", "earnings": "unsupported", "announcement": "unsupported"}
    monkeypatch.setattr(events, "fetch_events", lambda *a, **kw: [])
    execute_events(database, "event_refresh", {"instrument": "US:AAPL"})
    wait_complete(database)
    second = execute_events(database, "event_list", {**fields, "event_type": "dividend"})
    assert second["total"] == 1 and second["events"][0]["status"] == "expected"
    assert second["query_coverage"]["status"] == "empty"


def test_source_status_and_coverage_finish_in_one_transaction(tmp_path, monkeypatch):
    with EventStore(tmp_path / "state.db") as store:
        state = store.claim_refresh(INSTRUMENT)
        original = events.record_range
        def fail_second(connection, **fields):
            if fields["event_type"] == "split":
                raise RuntimeError("simulated write failure")
            original(connection, **fields)
        monkeypatch.setattr(events, "record_range", fail_second)
        with pytest.raises(RuntimeError):
            store.finish_refresh(state, status="empty", count=0, type_counts={"dividend": 0, "split": 0})
        assert store.source_status()["sources"][0]["status"] == "running"
        assert {r[0] for r in store._connection.execute("SELECT status FROM research_event_coverage")} == {"running"}
