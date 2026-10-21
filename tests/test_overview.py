from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from config import RulesConfig, Settings
from guardian.application import GuardianApplication
from guardian.backend import AlphaGuardBackend
from guardian.preferences import PreferencesStore
from guardian.protocol import RpcErrorResponse, RpcRequest, RpcSuccessResponse, serialize_response
from research.events import EventStore
from research.models import ResearchInstrument, ResearchReport
from research.overview import execute_overview, record_instrument_scans
from research.reviews import execute_review
from research.store import ResearchStore
from research.watchlist import WatchlistStore

NOW = datetime(2026, 10, 15, 14, tzinfo=UTC)
TOKEN = "o" * 43


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("overview must not access the network")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    database, rules = tmp_path / "state.db", tmp_path / "rules.yaml"
    rules.write_text(yaml.safe_dump({"watchlist": {
        "AAPL": {"name": "Apple", "market": "US", "currency": "USD", "enabled": False,
                 "buy_rules": [{"id": "aapl-low", "type": "price_below", "value": 140.0}]},
        "00700": {"name": "Tencent", "market": "HK", "currency": "HKD", "enabled": True,
                  "buy_rules": [{"id": "hk-low", "type": "price_below", "value": 280.0}]},
    }}))
    monkeypatch.setattr("config.RULES_CONFIG_PATH", rules)
    return database, rules


def overview(workspace, key="US:AAPL"):
    database, rules = workspace
    return execute_overview(database, {"instrument": key}, rules_path=rules, now=NOW)


def add_report(database, identity="r1", *, ticker="AAPL", market="US", age=0, expired=False,
               action="watch", published_age=timedelta(hours=1)):
    created = NOW - timedelta(hours=2) + timedelta(minutes=age)
    with ResearchStore(database) as store:
        evidence = store.upsert_evidence({"source_url": f"https://example.org/{identity}",
            "title": "Source", "published_at": (created - published_age).isoformat(),
            "content_text": "Revenue increased in the quarter.", "content_kind": "article", "completeness": "full"}, now=created)
        report = ResearchReport(id=identity, evidence_id=evidence.id, evidence_hash=evidence.content_hash,
            instrument=ResearchInstrument(market=market, ticker=ticker), horizon="swing", action=action,
            status="ready", extraction_status="analyzed", summary="Original report " + "x" * 2000,
            created_at=created, valid_until=created + timedelta(hours=1 if expired else 24), analyzer_version="test")
        return store.save_report(report)


def add_event(store, identity, *, key="AAPL", date_value="2026-10-20", status="expected", **kwargs):
    return store.save_events([{"instrument": {"market": "US", "ticker": key},
        "event_type": "earnings", "source": "company-ir", "source_event_id": identity,
        "source_url": "https://example.org/event", "title": "Company earnings",
        "event_date": date_value, "event_timezone": "America/New_York", "status": status, **kwargs}], now=NOW)


def test_missing_database_and_legacy_tables_stay_unmodified(workspace):
    database, _ = workspace
    first = overview(workspace)
    assert not database.exists()
    assert first["membership"]["listed"] is True
    assert first["rules"] == {"status": "ready", "configured": True, "monitoring_enabled": False, "rule_count": 1}
    assert first["reports"]["status"] == "not_collected"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy (id INTEGER)")
    before = database.read_bytes()
    siblings = set(database.parent.iterdir())
    assert overview(workspace)["events"]["status"] == "not_collected"
    assert database.read_bytes() == before and set(database.parent.iterdir()) == siblings
    with sqlite3.connect(database) as connection:
        assert [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ["legacy"]


@pytest.mark.parametrize("fields", [{}, {"instrument": "US:AAPL", "unknown": 1}, {"instrument": True},
                                   {"instrument": "US:AAPL:extra"}, {"instrument": "HK:bad"}])
def test_invalid_fields_never_create_database(workspace, fields):
    database, rules = workspace
    with pytest.raises(ValueError):
        execute_overview(database, fields, rules_path=rules, now=NOW)
    assert not database.exists()


def test_security_identity_and_research_rule_switches_are_independent(workspace):
    database, _ = workspace
    with WatchlistStore(database) as store:
        store.save_identity_and_settings(ResearchInstrument(market="US", ticker="AAPL", name="Apple research"),
                                        {"instruments": [{"market": "US", "ticker": "AAPL"}], "markets": ["HK"]})
    result = overview(workspace)
    assert result["membership"]["research_enabled"] is True
    assert result["membership"]["research_market_enabled"] is False
    assert result["rules"]["monitoring_enabled"] is False
    hk = overview(workspace, "hk:700")
    assert hk["instrument"]["key"] == "HK:00700"
    assert hk["rules"]["monitoring_enabled"] is True
    assert hk["membership"]["research_enabled"] is False


def test_reports_reviews_and_events_are_scoped_and_bounded(workspace):
    database, _ = workspace
    for index in range(8):
        add_report(database, f"r{index}", age=index, expired=index == 7)
    add_report(database, "other", ticker="MSFT")
    add_report(database, "other-market", market="HK", ticker="AAPL")
    execute_review(database, "review_save", {"report_id": "r1", "status": "verified", "notes": "checked",
                   "next_review_at": "2026-10-10T08:00:00Z", "revision": 0})
    execute_review(database, "review_save", {"report_id": "r2", "status": "deferred", "notes": "later",
                   "next_review_at": "2026-10-21T08:00:00Z", "revision": 0})
    with EventStore(database) as store:
        for index in range(7):
            add_event(store, f"event-{index}", date_value=f"2026-10-{20 + index}")
        add_event(store, "past", date_value="2026-10-14")
        add_event(store, "cancelled", status="cancelled")
        add_event(store, "other-stock", key="MSFT")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = overview(workspace)
    assert result["reports"]["total"] == 8
    assert [r["id"] for r in result["reports"]["items"]] == ["r7", "r6", "r5", "r4", "r3"]
    assert result["reports"]["items"][0]["status"] == "expired"
    assert result["reports"]["items"][0]["original_status"] == "ready"
    assert all(len(r["summary"]) <= 300 for r in result["reports"]["items"])
    assert result["reviews"] == {"status": "ready", "pending_count": 6, "next_review_at": "2026-10-10T08:00:00+00:00"}
    assert result["events"]["total"] == 7 and len(result["events"]["items"]) == 5
    assert result["events"]["items"][0]["event_date"] == "2026-10-20"
    assert len(json.dumps(result).encode()) < 20_000
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_source_failure_and_previous_success_are_visible_only_for_security(workspace):
    database, _ = workspace
    with EventStore(database) as store:
        state = store.claim_refresh(ResearchInstrument(market="US", ticker="AAPL"))
        store.finish_refresh(state, status="success", count=3)
        state = store.claim_refresh(ResearchInstrument(market="US", ticker="AAPL"))
        store.finish_refresh(state, status="failed", detail="permission_required")
    # Source test records use the real clock; the overview clock is later.
    result = execute_overview(database, {"instrument": "US:AAPL"}, now=datetime.now(UTC) + timedelta(seconds=1))
    source = result["events"]["sources"][0]
    assert source["status"] == "failed" and source["detail"] == "permission_required"
    assert source["last_success_at"] is not None
    assert overview(workspace, "US:MSFT")["events"]["sources"] == []


def test_report_status_uses_original_evidence_age_and_source_revision(workspace):
    database, _ = workspace
    add_report(database, "old-evidence", action="hold_review", published_age=timedelta(days=20))
    revised = add_report(database, "revised")
    with ResearchStore(database) as store:
        store.upsert_evidence({"source_url": "https://example.org/revised", "title": "Revised source",
            "published_at": NOW.isoformat(), "content_text": "The source corrected its prior figures.",
            "content_kind": "article", "completeness": "full"}, now=NOW)
        expected = {identity: store.get_report(identity, now=NOW).status
                    for identity in ("old-evidence", revised.id)}
    result = overview(workspace)
    actual = {item["id"]: item["status"] for item in result["reports"]["items"]}
    assert actual == expected == {"old-evidence": "expired", "revised": "insufficient_evidence"}


def test_future_events_use_market_date_and_skip_past_exact_times(workspace):
    database, rules = workspace
    with EventStore(database) as store:
        add_event(store, "late-today", date_value="2026-10-15")
        add_event(store, "already-ended", date_value="2026-10-15", event_at="2026-10-15T13:00:00Z")
    now = datetime(2026, 10, 16, 1, tzinfo=UTC)  # still Oct 15 in New York
    result = execute_overview(database, {"instrument": "US:AAPL"}, rules_path=rules, now=now)
    assert result["events"]["total"] == 1
    assert result["events"]["items"][0]["event_at"] is None


def test_unreadable_or_corrupt_records_are_failures_not_empty(workspace):
    database, _ = workspace
    database.write_bytes(b"not a database")
    result = overview(workspace)
    assert result["reports"]["status"] == "failed" and result["reports"]["total"] is None
    assert result["reviews"]["pending_count"] is None
    database.unlink()
    add_report(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX research_reports_instrument_time")
        connection.execute("UPDATE research_reports SET payload_json='broken-secret-content'")
    result = overview(workspace)
    assert result["reports"]["status"] == "failed" and result["reports"]["total"] is None
    assert "broken-secret-content" not in json.dumps(result)
    assert result["reviews"]["status"] == "failed"


def test_missing_current_event_version_is_failure_not_zero(workspace):
    database, _ = workspace
    with EventStore(database) as store:
        add_event(store, "broken-version")
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM research_event_versions")
    result = overview(workspace)
    assert result["events"]["status"] == "failed"
    assert result["events"]["total"] is None


def test_invalid_event_time_and_rules_are_explicit_section_failures(workspace):
    database, rules = workspace
    with EventStore(database) as store:
        add_event(store, "invalid-clock")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE research_event_versions SET payload_json=json_set(payload_json,'$.event_at','not-a-clock')")
    rules.write_text("watchlist: [unterminated")
    result = overview(workspace)
    assert result["events"]["status"] == "failed" and result["events"]["total"] is None
    assert result["rules"]["status"] == "failed" and result["rules"]["rule_count"] is None


def test_legacy_report_reads_do_not_migrate_or_create_indexes(workspace):
    database, _ = workspace
    add_report(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX research_reports_instrument_time")
    before = database.read_bytes()
    assert overview(workspace)["reports"]["total"] == 1
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='research_reports_instrument_time'").fetchone()
    add_report(database, "r2")
    with sqlite3.connect(database) as connection:
        plan = connection.execute("EXPLAIN QUERY PLAN SELECT id FROM research_reports r WHERE "
            "json_extract(r.payload_json,'$.instrument.market')=? AND json_extract(r.payload_json,'$.instrument.ticker')=? "
            "ORDER BY julianday(created_at) DESC,id LIMIT 5", ("US", "AAPL")).fetchall()
    assert any("research_reports_instrument_time" in row[-1] for row in plan)
    assert list(database.parent.glob("*.pre-overview-index-*.bak"))


def test_global_scans_and_other_security_do_not_become_selected_scan(workspace):
    database, _ = workspace
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE run_log (finished_at TEXT)")
        connection.execute("INSERT INTO run_log VALUES (?)", (NOW.isoformat(),))
    assert overview(workspace)["scan"]["status"] == "not_collected"
    record_instrument_scans(database, [{"market": "US", "ticker": "MSFT", "status": "ready", "detail": "evaluated"}], observed_at=NOW)
    assert overview(workspace)["scan"]["status"] == "not_collected"
    assert overview(workspace, "US:MSFT")["scan"]["observed_at"] == NOW.isoformat()
    assert list(database.parent.glob("*.pre-overview-*.bak"))


def test_authenticated_overview_command_is_read_only_and_bounded(workspace, monkeypatch):
    database, _ = workspace
    monkeypatch.setattr("guardian.backend.STATE_PATH", database)
    backend = AlphaGuardBackend(settings_loader=lambda: Settings(_env_file=None),
                                preferences_store=PreferencesStore(database.parent / "preferences.json"))
    app = GuardianApplication(TOKEN, dispatcher=backend)
    def send(token, fields):
        return app.handle(RpcRequest.model_validate({"jsonrpc": "2.0", "id": "overview", "method": "research.command",
            "params": {"command": "instrument_overview", **fields}, "auth_token": token}))
    try:
        response = send("x" * 43, {"instrument": "US:AAPL"})
        assert isinstance(response, RpcErrorResponse) and response.error.data.kind == "unauthorized"
        response = send(TOKEN, {"instrument": "US:AAPL", "bad": "private"})
        assert isinstance(response, RpcErrorResponse) and "private" not in response.model_dump_json()
        response = send(TOKEN, {"instrument": "US:AAPL"})
        assert isinstance(response, RpcSuccessResponse)
        assert response.result["accepted"] is True
        assert response.result["result"]["instrument"]["key"] == "US:AAPL"
        assert len(serialize_response(response)) < 1_048_576
        assert not database.exists()
    finally:
        backend.close_research()


def test_real_scan_path_records_security_metadata_and_fixture_is_state_free(workspace, monkeypatch):
    import main
    from reliability import evaluate_snapshot_reliability, gate_snapshot_for_decision

    database, rules_file = workspace
    rules = RulesConfig.model_validate(yaml.safe_load(rules_file.read_text()))
    instrument = rules.watchlist["AAPL"].model_copy(update={"enabled": True})
    rules = rules.model_copy(update={"watchlist": {"AAPL": instrument}})
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    monkeypatch.setattr(main, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(main, "STATE_PATH", database)
    def provider(ticker, market, **kwargs):
        snapshot = {"ticker": ticker, "market": market, "price": 150.0, "currency": "USD",
                    "provider": "test-provider", "retrieved_at": NOW.isoformat(), "as_of": NOW.isoformat(),
                    "quality_issues": [], "field_metadata": {"price": {"provider": "test-provider",
                    "source_as_of": NOW.isoformat(), "observed_at": NOW.isoformat(), "time_basis": "source_event"}}}
        report = evaluate_snapshot_reliability(snapshot, kwargs["required_fields"], kwargs["freshness_policies"],
                                              kwargs["freshness_context"], future_tolerance_seconds=kwargs["future_tolerance_seconds"])
        return gate_snapshot_for_decision(snapshot, report, kwargs["required_fields"])
    monkeypatch.setattr(main, "get_stock", provider)
    outcome = asyncio.run(main.run_stock_scan(market="US", now=NOW))
    assert outcome["evaluated"] == 1
    assert overview(workspace)["scan"] == {"status": "ready", "observed_at": NOW.isoformat(), "detail": "evaluated"}
    assert overview(workspace, "HK:00700")["scan"]["status"] == "not_collected"
    def fail(*_args, **_kwargs):
        raise RuntimeError("sensitive-provider-error")
    monkeypatch.setattr(main, "get_stock", fail)
    asyncio.run(main.run_stock_scan(market="US", now=NOW + timedelta(minutes=1)))
    assert overview(workspace)["scan"]["status"] == "failed"
    assert "sensitive-provider-error" not in json.dumps(overview(workspace))
    monkeypatch.setattr(main, "get_stock", provider)
    monkeypatch.setattr("research.overview.record_instrument_scans", fail)
    outcome = asyncio.run(main.run_stock_scan(market="US", now=NOW + timedelta(minutes=2)))
    assert outcome["status"] == "success" and outcome["evaluated"] == 1
    fixture_db = database.parent / "fixture.db"
    monkeypatch.setattr(main, "STATE_PATH", fixture_db)
    asyncio.run(main.run_stock_scan(fixture_path=Path("data/fixtures/snapshots.json"), include_disabled=True, now=NOW))
    assert not fixture_db.exists()
