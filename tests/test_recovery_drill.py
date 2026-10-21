"""Bounded, offline recovery drills against real temporary SQLite databases.

These scenarios verify a restart/restore boundary, not continuous operation.
"""

from __future__ import annotations

import hashlib
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import main
from config import RulesConfig, Settings
from reliability import (
    CacheState,
    CircuitState,
    ProviderKey,
    ProviderRuntime,
    ProviderRuntimeConfig,
    ProviderUnavailableError,
)
from reliability.provider import ProviderHTTPError
from research.events import EventStore
from research.models import ResearchInstrument, ResearchReport
from research.store import ResearchStore
from state import (
    BlindnessObservation,
    CorruptProtectionStateError,
    ProtectionState,
    StateStore,
)
from state.cockpit import build_reliability_cockpit

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
KEY = ProviderKey(provider="drill", operation="quote", market="US")


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("recovery drills must remain offline")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    # model_construct uses only defaults; no .env, environment credentials or key store.
    monkeypatch.setattr(main, "get_settings", lambda: Settings.model_construct())


class Clock:
    def __init__(self):
        self.now = NOW
        self.sleeps = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


def runtime(clock):
    return ProviderRuntime(
        ProviderRuntimeConfig(
            max_attempts=3,
            failure_threshold=1,
            base_backoff_seconds=1,
            max_backoff_seconds=4,
            fresh_cache_seconds=1,
            stale_if_error_seconds=60,
            open_seconds=10,
        ),
        clock=clock,
        sleeper=clock.sleep,
        random_value=lambda: 0.5,
    )


def test_fault_restart_drill_preserves_circuit_and_never_upgrades_old_cache(tmp_path):
    clock = Clock()
    original = runtime(clock)
    failures = [
        ConnectionError("private upstream detail"),
        ProviderHTTPError(
            429, format_datetime(NOW + timedelta(seconds=4), usegmt=True)
        ),
    ]
    calls = []

    def jitter_then_recover():
        calls.append(clock.now)
        if failures:
            raise failures.pop(0)
        return {"price": 100.0}

    success = original.execute(
        KEY, jitter_then_recover, idempotent=True, cache_identity="AAPL"
    )
    assert [attempt.failure_class for attempt in success.attempts] == [
        "connection",
        "rate_limited",
        "none",
    ]
    assert clock.sleeps == [0.5, 3.5]
    assert calls[-1] == NOW + timedelta(seconds=4)
    clock.advance(2)

    def timeout():
        raise TimeoutError("private upstream detail")

    stale = original.execute(KEY, timeout, idempotent=True, cache_identity="AAPL")
    assert (
        stale.cache_state is CacheState.STALE_IF_ERROR and not stale.usable_for_signal
    )
    assert stale.circuit.state is CircuitState.OPEN
    assert [attempt.failure_class for attempt in stale.attempts[:-1]] == ["timeout"] * 3
    assert "private" not in str(stale.attempts)
    database = tmp_path / "runtime.db"
    with StateStore(database) as store:
        store.save_provider_runtime_state(
            original.export_state().model_dump(mode="json"), now=clock.now
        )
    restored = runtime(clock)
    with StateStore(database) as store:
        restored.import_state(
            store.load_provider_runtime_state(strict=True, not_after=clock.now)
        )
        receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={},
            market_contract_hashes={},
            current_delivery_fingerprints={
                channel: "d" * 64 for channel in ("telegram", "whatsapp", "heartbeat")
            },
            generated_at=clock.now,
        )
    capability = receipt["providers"]["capabilities"][0]
    assert (
        capability["circuit_state"] == "open"
        and "circuit_open" in capability["reasons"]
    )
    assert (
        capability["grade"] == "insufficient_data"
    )  # This drill cannot prove provider health.
    blocked = restored.execute(
        KEY,
        lambda: pytest.fail("open circuit reached upstream"),
        idempotent=True,
        cache_identity="AAPL",
    )
    assert (
        blocked.attempts[0].outcome == "circuit_open" and not blocked.usable_for_signal
    )
    clock.advance(11)
    recovered = restored.execute(
        KEY, lambda: {"price": 101.0}, idempotent=True, cache_identity="AAPL"
    )
    assert (
        recovered.circuit.state is CircuitState.CLOSED and recovered.usable_for_signal
    )
    clock.advance(61)
    with pytest.raises(ProviderUnavailableError) as expired:
        restored.execute(KEY, timeout, idempotent=True, cache_identity="AAPL")
    assert all(
        attempt.outcome != "stale_fallback" for attempt in expired.value.attempts
    )


def observation(scope, identity, at):
    return BlindnessObservation(
        scope=scope,
        observation_id=identity,
        observed_at=at,
        enabled_instruments=1,
        usable_instruments=1,
        full_coverage_scan=True,
    )


@pytest.mark.parametrize("market", ["US", "HK"])
def test_cli_scope_repair_backup_is_restorable_and_other_market_is_untouched(
    tmp_path, monkeypatch, market
):
    database = tmp_path / "state.db"
    scope = f"market:{market}"
    other = "market:HK" if market == "US" else "market:US"
    rules = RulesConfig.model_validate(
        {
            "watchlist": {
                "AAPL": {
                    "name": "Apple",
                    "market": "US",
                    "currency": "USD",
                    "enabled": True,
                },
                "00700": {
                    "name": "Tencent",
                    "market": "HK",
                    "currency": "HKD",
                    "enabled": True,
                },
            }
        }
    )
    with StateStore(database) as store:
        store.set_protection_scope(["US", "HK"], now=NOW)
        for market_scope in (scope, other):
            store.observe_protection(
                observation(market_scope, "original-full-scan", NOW)
            )
        other_before = store.load_protection_state(other)
        store.connection.execute(
            "UPDATE protection_state SET snapshot_json=? WHERE scope_key=?",
            ("{broken-drill-payload", scope),
        )
    monkeypatch.setattr(main, "STATE_PATH", database)
    monkeypatch.setattr(main, "load_rules_config", lambda: rules)
    result = CliRunner().invoke(
        main.app, ["repair-state", "--scope", scope, "--confirm"]
    )
    assert result.exit_code == 0, result.output
    assert (
        "state=BLIND color=RED" in result.output
        and "broken-drill-payload" not in result.output
    )
    backups = list(tmp_path.glob("state.db.backup-*.sqlite3"))
    assert len(backups) == 1
    backup_digest = hashlib.sha256(backups[0].read_bytes()).hexdigest()
    restored_path = tmp_path / "restored-before-repair.db"
    with (
        sqlite3.connect(backups[0]) as source,
        sqlite3.connect(restored_path) as target,
    ):
        source.backup(target)
        assert target.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with StateStore(restored_path) as restored:
        # A pre-repair backup preserves evidence, including the known logical corruption.
        with pytest.raises(CorruptProtectionStateError):
            restored.load_protection_state(scope)
        assert restored.load_protection_state(other) == other_before
    assert hashlib.sha256(backups[0].read_bytes()).hexdigest() == backup_digest
    with StateStore(database) as repaired:
        sentinel = repaired.load_protection_state(scope)
        assert sentinel.state is ProtectionState.BLIND
        assert repaired.load_protection_state(other) == other_before
        at = sentinel.updated_at + timedelta(seconds=1)
        first, _ = repaired.observe_protection(observation(scope, "repair-full-1", at))
        replay, duplicate_event = repaired.observe_protection(
            observation(scope, "repair-full-1", at)
        )
        second, _ = repaired.observe_protection(
            observation(scope, "repair-full-2", at + timedelta(seconds=1))
        )
        assert (
            first.snapshot.state is replay.snapshot.state is ProtectionState.RECOVERING
        )
        assert duplicate_event is None
        assert second.snapshot.state is ProtectionState.HEALTHY
        assert repaired.load_protection_state(other) == other_before


def test_sqlite_restore_reopens_versions_reports_events_and_pending_work_without_side_effects(
    tmp_path, monkeypatch
):
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        assert store.should_notify_signal(
            "AAPL:BUY", True, "original-rule-proof", 24, NOW
        )
        store.mark_signal_notified("AAPL:BUY", "original-rule-proof", now=NOW)
        token = store.claim_news_notification("pending-news-proof", now=NOW)
        assert token
        store.save_provider_runtime_state(
            {"circuits": {}, "caches": {}, "observations": {}}, now=NOW
        )
    with ResearchStore(database) as store:
        first = store.upsert_evidence(
            {
                "source_url": "https://example.test/filing",
                "title": "Version one",
                "content_text": "Original disclosed wording.",
                "content_kind": "article",
                "completeness": "full",
            },
            now=NOW,
        )
        report = ResearchReport(
            id="restore-report",
            evidence_id=first.id,
            evidence_hash=first.content_hash,
            instrument=ResearchInstrument(market="US", ticker="AAPL"),
            horizon="long",
            action="watch",
            status="ready",
            extraction_status="fixture",
            summary="Original reviewed material",
            created_at=NOW,
            valid_until=NOW + timedelta(days=1),
            analyzer_version="restore-drill-v1",
        )
        store.save_report(report)
        second = store.upsert_evidence(
            {
                "source_url": "https://example.test/filing",
                "title": "Version two",
                "content_text": "Corrected disclosed wording.",
                "content_kind": "article",
                "completeness": "full",
            },
            now=NOW + timedelta(seconds=1),
        )
        pending_run = store.record_run(
            "running",
            "awaiting explicit follow-up",
            started_at=NOW,
            finished_at=NOW,
            stats={"completed": 1, "pending": 2},
        )
        expected_report = store.get_report(report.id, now=NOW + timedelta(seconds=2))
        expected_runs = store.list_runs()
    event = {
        "instrument": {"market": "US", "ticker": "AAPL"},
        "event_type": "earnings",
        "source": "company-ir",
        "source_event_id": "restore-event",
        "title": "Results",
        "source_url": "https://example.test/results",
        "event_date": "2026-10-30",
        "event_timezone": "America/New_York",
    }
    with EventStore(database) as store:
        event_id = store.save_events([event], now=NOW)["events"][0]["event_id"]
        store.save_events(
            [{**event, "event_date": "2026-11-02"}], now=NOW + timedelta(seconds=1)
        )
        expected_events = store.history(event_id)
    monkeypatch.setattr(main, "STATE_PATH", database)
    with StateStore(database) as store:
        expected_receipt = build_reliability_cockpit(
            store=store,
            enabled_instruments={},
            market_contract_hashes={},
            current_delivery_fingerprints={
                channel: "d" * 64 for channel in ("telegram", "whatsapp", "heartbeat")
            },
            generated_at=NOW + timedelta(seconds=2),
        )
        backup = main._backup_state_database(store, now=NOW + timedelta(seconds=2))
    original_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    restored_path = tmp_path / "restored.db"
    with sqlite3.connect(backup) as source, sqlite3.connect(restored_path) as target:
        source.backup(target)
        assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert target.execute("PRAGMA foreign_key_check").fetchall() == []
        expected_dump = list(source.iterdump())
        assert list(target.iterdump()) == expected_dump
    with ResearchStore(restored_path) as restored:
        assert restored.get_evidence(first.id) == first
        assert restored.get_evidence(second.id) == second
        assert restored.list_current_evidence()[0].id == second.id
        assert (
            restored.get_report(report.id, now=NOW + timedelta(seconds=2))
            == expected_report
        )
        assert restored.list_runs() == expected_runs
        assert restored.list_runs()[0]["id"] == pending_run
        assert restored.list_runs()[0]["status"] == "running"
    with EventStore(restored_path) as restored:
        assert restored.history(event_id) == expected_events
    with StateStore(restored_path) as restored:
        assert (
            build_reliability_cockpit(
                store=restored,
                enabled_instruments={},
                market_contract_hashes={},
                current_delivery_fingerprints={
                    channel: "d" * 64
                    for channel in ("telegram", "whatsapp", "heartbeat")
                },
                generated_at=NOW + timedelta(seconds=2),
            )
            == expected_receipt
        )
        assert (
            list(restored.connection.iterdump()) == expected_dump
        )  # Opening/read did not send or resume work.
        assert not restored.should_notify_signal(
            "AAPL:BUY", True, "original-rule-proof", 24, NOW + timedelta(seconds=3)
        )
        assert (
            restored.claim_news_notification(
                "pending-news-proof", now=NOW + timedelta(seconds=3)
            )
            is None
        )
    assert hashlib.sha256(database.read_bytes()).hexdigest() == original_hash
    assert Path(backup).is_file()
