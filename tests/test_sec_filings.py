from datetime import UTC, datetime
import json
import time

import pytest

from research import sec_filings as sec
from research.events import execute_events
from research.models import ResearchInstrument
from research.network import ContentAccessError, FetchedContent

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
STOCK = ResearchInstrument(market="US", ticker="SPCX")


def columns():
    return {"accessionNumber": ["0001181412-26-000001", "0001181412-26-000002"],
            "filingDate": ["2026-09-01", "2026-09-02"], "reportDate": ["2026-06-30"] * 2,
            "acceptanceDateTime": ["2026-09-01T12:00:00Z", "2026-09-02T12:00:00"],
            "form": ["10-Q", "10-Q/A"], "primaryDocument": ["report.htm", "amend.htm"]}


def payload():
    return {"cik": "0001181412", "tickers": ["SPCX"],
            "filings": {"recent": columns(), "files": []}}


def mock_source(monkeypatch, data):
    monkeypatch.setattr(sec, "safe_fetch", lambda url, **kw: FetchedContent(url, json.dumps(data)))


def test_amendment_identity_and_unknown_timezone(monkeypatch):
    mock_source(monkeypatch, payload())
    rows = sec.fetch_filings(STOCK, now=NOW)
    assert len(rows) == 2 and rows[0]["source_event_id"] != rows[1]["source_event_id"]
    assert rows[1]["filing"]["amended"]
    assert rows[1]["published_at"] is None
    assert rows[1]["filing"]["acceptance_precision"] == "unknown_timezone"
    assert rows[0]["filing"]["report_date"] != rows[0]["event_date"]


@pytest.mark.parametrize("field,value", [("primaryDocument", "../bad.htm"),
    ("accessionNumber", "bad"), ("reportDate", "2026-02-30"),
    ("acceptanceDateTime", "2026-09-01T25:00:00Z")])
def test_invalid_rows_fail_complete_query(monkeypatch, field, value):
    data = payload()
    data["filings"]["recent"][field][0] = value
    mock_source(monkeypatch, data)
    with pytest.raises(ContentAccessError, match="sec_source_invalid"):
        sec.fetch_filings(STOCK, now=NOW)


@pytest.mark.parametrize("change", [{"cik": "123"}, {"tickers": ["OTHER"]}])
def test_identity_mismatch(monkeypatch, change):
    data = payload()
    data.update(change)
    mock_source(monkeypatch, data)
    with pytest.raises(ContentAccessError, match="identity_mismatch"):
        sec.fetch_filings(STOCK, now=NOW)


def test_history_budget_and_cancel(monkeypatch):
    data = payload()
    data["filings"]["files"] = [
        {"name": f"CIK0001181412-submissions-{i:03d}.json",
         "filingFrom": "2026-01-01", "filingTo": "2026-02-01"} for i in range(3)]
    mock_source(monkeypatch, data)
    with pytest.raises(ContentAccessError, match="sec_history_limit"):
        sec.fetch_filings(STOCK, now=NOW)
    with pytest.raises(ContentAccessError, match="cancelled"):
        sec.fetch_filings(STOCK, now=NOW, cancelled=lambda: True)


def test_history_merge_and_empty(monkeypatch):
    data = payload()
    data["filings"]["files"] = [{"name": "CIK0001181412-submissions-001.json",
        "filingFrom": "2026-01-01", "filingTo": "2026-09-01"}]
    def fetch(url, **kw):
        return FetchedContent(url, json.dumps(columns() if "-submissions-" in url else data))
    monkeypatch.setattr(sec, "safe_fetch", fetch)
    assert len(sec.fetch_filings(STOCK, now=NOW)) == 2
    empty = payload()
    empty["filings"]["recent"] = {k: [] for k in columns()}
    mock_source(monkeypatch, empty)
    assert sec.fetch_filings(STOCK, now=NOW) == []


def test_product_refresh_repeat_and_failure_preserves_records(tmp_path, monkeypatch):
    mock_source(monkeypatch, payload())
    database = tmp_path / "state.db"
    def refresh():
        execute_events(database, "event_refresh", {"instrument": "US:SPCX", "source": sec.SOURCE})
        until = time.monotonic() + 3
        while time.monotonic() < until:
            status = execute_events(database, "event_status", {})
            if not status["running"]:
                return status
            time.sleep(.01)
        pytest.fail("worker timeout")
    refresh()
    first = execute_events(database, "event_list", {"instrument": "US:SPCX"})
    assert first["total"] == 2
    refresh()
    again = execute_events(database, "event_list", {"instrument": "US:SPCX"})
    assert [(r["event_id"], r["version"]) for r in first["events"]] == [
        (r["event_id"], r["version"]) for r in again["events"]]
    def denied(*args, **kw):
        raise ContentAccessError("rate_limited")
    monkeypatch.setattr(sec, "safe_fetch", denied)
    assert refresh()["sources"][0]["status"] == "failed"
    assert execute_events(database, "event_list", {})["total"] == 2


def test_acceptance_offset():
    assert sec.acceptance("2026-09-01T12:00:00-04:00")[0] == "2026-09-01T16:00:00+00:00"


def test_coverage_is_source_specific_and_sec_expires_after_six_hours(tmp_path):
    from datetime import timedelta
    from research.event_coverage import query_coverage, record_range
    from research.events import EventStore
    with EventStore(tmp_path / "state.db") as store:
        record_range(store._connection, run_id="sec-run", instrument="US:SPCX",
                     event_type="announcement", date_from=NOW.date(), date_to=NOW.date(),
                     status="success", checked_at=NOW, count=2, source=sec.SOURCE)
        fields = dict(event_type="announcement", date_from=NOW.date().isoformat(),
                      date_to=NOW.date().isoformat(), now=NOW + timedelta(hours=7))
        assert query_coverage(store._connection, STOCK, source=sec.SOURCE, **fields)["ranges"][0]["status"] == "stale"
        assert query_coverage(store._connection, STOCK, **fields)["ranges"][0]["status"] == "unsupported"


def test_manual_import_cannot_impersonate_sec(tmp_path, monkeypatch):
    from research.events import EventStore
    mock_source(monkeypatch, payload())
    rows = sec.fetch_filings(STOCK, now=NOW)
    with EventStore(tmp_path / "state.db") as store:
        with pytest.raises(ValueError):
            store.save_events(rows, now=NOW)


def test_misaligned_columns_and_record_limit(monkeypatch):
    data = payload()
    data["filings"]["recent"]["form"].pop()
    mock_source(monkeypatch, data)
    with pytest.raises(ContentAccessError, match="sec_source_invalid"):
        sec.fetch_filings(STOCK, now=NOW)
    data = payload()
    cols = data["filings"]["recent"]
    for key in cols:
        cols[key] = [cols[key][0]] * 101
    cols["accessionNumber"] = [f"0001181412-26-{i:06d}" for i in range(101)]
    mock_source(monkeypatch, data)
    with pytest.raises(ContentAccessError, match="sec_record_limit"):
        sec.fetch_filings(STOCK, now=NOW)
