from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config import load_rules_config
from reliability import ProviderKey, ProviderRuntime
from state import (
    BlindnessObservation,
    CoverageEvidence,
    CorruptProtectionStateError,
    ProtectionSnapshot,
    ProtectionObservationCollisionError,
    ProtectionState,
    StateStore,
    transition_protection,
)
from state.store import (
    _validated_protection_window,
    _validated_run_coverage,
    instrument_set_hash,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
DELIVERY_FINGERPRINT = "d" * 64


def _enabled_contract_rules():
    rules = load_rules_config()
    return rules.model_copy(
        update={
            "watchlist": {
                ticker: instrument.model_copy(update={"enabled": True})
                for ticker, instrument in rules.watchlist.items()
            }
        }
    )


def _protection_contract_version(rules, market: str) -> str:
    from state import protection_contract_version

    return protection_contract_version(rules, market)


def _valid_stock_run_detail() -> dict:
    return {
        "selected": 1,
        "evaluated": 1,
        "notified": 0,
        "error_tickers": [],
        "notification_error_tickers": [],
        "incident_attempted": False,
        "incident_notified": False,
        "incident_notification_error": None,
        "integrity_incident_attempted": 0,
        "integrity_incident_notified": 0,
        "integrity_notification_errors": {},
        "integrity_ledger_error": None,
        "telegram_probe_attempted": False,
        "telegram_probe_success": None,
        "telegram_probe_error": None,
        "protection_event_id": None,
        "protection_event_ids": [],
        "reliability": {
            "fresh_data_coverage": {
                "enabled_instruments": 1,
                "usable_instruments": 1,
                "fresh_coverage": 1.0,
                "unusable_tickers": [],
            },
            "trusted_decision_coverage": {
                "enabled_instruments": 1,
                "usable_instruments": 1,
                "fresh_coverage": 1.0,
                "unusable_tickers": [],
            },
        },
    }


def test_coverage_evidence_rejects_secret_shaped_ticker() -> None:
    with pytest.raises(ValueError, match="canonical identifiers"):
        CoverageEvidence(
            enabled_instruments=2,
            usable_instruments=1,
            ratio=0.5,
            unusable_tickers=("https://heartbeat.example/private-token",),
        )


def test_signal_edges_failures_cooldown_and_unknown(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        assert store.should_notify_signal("AAPL:BUY", True, "v1", 24, T0)

        # No success mark models a failed notification; the next scan retries.
        assert store.should_notify_signal(
            "AAPL:BUY", True, "v1", 24, T0 + timedelta(minutes=5)
        )

        store.mark_signal_notified("AAPL:BUY", "v1", T0 + timedelta(minutes=6))
        assert not store.should_notify_signal(
            "AAPL:BUY", True, "v1", 24, T0 + timedelta(hours=23)
        )
        assert store.should_notify_signal(
            "AAPL:BUY", True, "v1", 24, T0 + timedelta(hours=25)
        )
        store.mark_signal_notified("AAPL:BUY", now=T0 + timedelta(hours=25))

        before_unknown = dict(
            store.connection.execute(
                "SELECT * FROM signal_state WHERE signal_key = 'AAPL:BUY'"
            ).fetchone()
        )
        assert not store.should_notify_signal(
            "AAPL:BUY", None, "unknown-evidence", 24, T0 + timedelta(days=2)
        )
        after_unknown = dict(
            store.connection.execute(
                "SELECT * FROM signal_state WHERE signal_key = 'AAPL:BUY'"
            ).fetchone()
        )
        assert after_unknown == before_unknown

        assert not store.should_notify_signal(
            "AAPL:BUY", False, "reset", 24, T0 + timedelta(days=2)
        )
        assert store.should_notify_signal(
            "AAPL:BUY", True, "v1", 24, T0 + timedelta(days=2, minutes=1)
        )


def test_evidence_change_is_a_new_notification_and_stale_mark_is_rejected(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        assert store.should_notify_signal("AAPL:BUY", True, "old", 0, T0)
        store.mark_signal_notified("AAPL:BUY", "old", T0)
        assert not store.should_notify_signal(
            "AAPL:BUY", True, "old", 0, T0 + timedelta(hours=1)
        )

        assert store.should_notify_signal(
            "AAPL:BUY", True, "new", 0, T0 + timedelta(hours=2)
        )
        with pytest.raises(ValueError, match="fingerprint changed"):
            store.mark_signal_notified("AAPL:BUY", "old", T0 + timedelta(hours=2))
        assert store.should_notify_signal(
            "AAPL:BUY", True, "new", 0, T0 + timedelta(hours=3)
        )


def test_state_and_news_dedup_survive_reopen(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        assert store.should_notify_signal("00700:SELL", True, "proof", 24, T0)
        store.mark_signal_notified("00700:SELL", now=T0)
        assert store.is_news_new("article-hash")
        store.mark_news_notified("article-hash", T0)
        run_id = store.record_run("scan:HK", "success", {"symbols": 3}, now=T0)
        assert run_id > 0

    with StateStore(database) as reopened:
        assert not reopened.should_notify_signal(
            "00700:SELL", True, "proof", 24, T0 + timedelta(hours=1)
        )
        assert not reopened.is_news_new("article-hash")
        assert reopened.recent_status("scan:HK") == [
            {
                "id": run_id,
                "job": "scan:HK",
                "status": "success",
                "started_at": T0.isoformat(timespec="microseconds"),
                "finished_at": T0.isoformat(timespec="microseconds"),
                "detail": {"symbols": 3},
            }
        ]


def test_sqlite_safety_pragmas_and_foreign_keys(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db", busy_timeout_ms=7_500) as store:
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 7_500
        tables = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "signal_state",
            "signal_events",
            "news_seen",
            "notification_claims",
            "run_log",
            "run_log_quarantine",
            "protection_state",
            "protection_events",
            "protection_state_quarantine",
            "protection_scope",
            "protection_windows",
            "delivery_state",
            "provider_runtime_state",
            "provider_runtime_quarantine",
        } <= tables


def test_concurrent_writers_are_atomic(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with StateStore(database):
        pass

    def write_run(index: int) -> None:
        with StateStore(database, busy_timeout_ms=10_000) as store:
            store.record_run("parallel", "success", {"index": index}, now=T0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_run, range(40)))

    with StateStore(database) as store:
        count = store.connection.execute(
            "SELECT COUNT(*) FROM run_log WHERE job = 'parallel'"
        ).fetchone()[0]
    assert count == 40


def test_run_records_validate_full_ledger_before_limit_without_leaking_detail(
    tmp_path: Path,
) -> None:
    secret = "https://heartbeat.example/private-token"
    valid_detail = _valid_stock_run_detail()
    with StateStore(tmp_path / "state.db") as store:
        store.record_run(
            "stock-scan:US",
            "success",
            {"private_payload": secret},
            now=T0,
        )
        store.record_run(
            "stock-scan:US",
            "success",
            valid_detail,
            now=T0 + timedelta(minutes=1),
        )

        with pytest.raises(CorruptProtectionStateError):
            store.run_records(
                not_after=T0 + timedelta(minutes=2),
                job="stock-scan:US",
                limit=1,
            )


@pytest.mark.parametrize(
    "affected",
    [[], ["AAPL", "MSFT"], ["AAPL", "AAPL"]],
)
def test_run_coverage_requires_exact_unique_affected_instruments(affected) -> None:
    with pytest.raises(ValueError, match="affected instruments"):
        _validated_run_coverage(
            {
                "enabled_instruments": 2,
                "usable_instruments": 1,
                "fresh_coverage": 0.5,
                "unusable_tickers": affected,
            }
        )


def test_all_run_market_slices_must_sum_exactly_to_aggregate(
    tmp_path: Path,
) -> None:
    detail = _valid_stock_run_detail()
    detail["reliability"]["by_market"] = {
        "US": {
            "selected": 1,
            "evaluated": 0,
            "fresh_data_coverage": {
                "enabled_instruments": 1,
                "usable_instruments": 0,
                "fresh_coverage": 0.0,
                "unusable_tickers": ["AAPL"],
            },
            "trusted_decision_coverage": {
                "enabled_instruments": 1,
                "usable_instruments": 0,
                "fresh_coverage": 0.0,
                "unusable_tickers": ["AAPL"],
            },
        }
    }
    with StateStore(tmp_path / "state.db") as store:
        store.record_run("stock-scan:ALL", "success", detail, now=T0)
        with pytest.raises(CorruptProtectionStateError):
            store.run_records(not_after=T0)


def test_corrupt_run_log_repair_is_atomic_hash_only_and_rearms_generation(
    tmp_path: Path,
) -> None:
    secret = "https://heartbeat.example/private-token"
    valid_detail = _valid_stock_run_detail()
    with StateStore(tmp_path / "state.db") as store:
        original_scope = store.set_protection_scope(
            ["HK", "US"],
            enabled_instruments_by_market={
                "HK": ("00700",),
                "US": ("AAPL",),
            },
            market_contract_hashes={"HK": "b" * 64, "US": "a" * 64},
            now=T0,
        )
        valid_run_id = store.record_run(
            "stock-scan:US", "success", valid_detail, now=T0
        )
        with pytest.raises(ValueError, match="valid and must not be repaired"):
            store.repair_corrupt_run_log(
                affected_markets=["US"],
                now=T0 + timedelta(seconds=1),
            )
        corrupt_run_id = store.record_run(
            "stock-scan:US",
            "success",
            {"private_payload": secret},
            now=T0 + timedelta(seconds=1),
        )
        incident = store.observe_integrity_incident(
            "global",
            "run_log",
            "state_corrupt",
            delivery_status="pending",
            now=T0 + timedelta(seconds=1),
        )
        claim = store.claim_integrity_notification(
            incident["id"],
            now=T0 + timedelta(seconds=2),
        )
        assert claim is not None
        with pytest.raises(ValueError, match="affected_markets"):
            store.repair_corrupt_run_log(
                affected_markets=["CN"],
                now=T0 + timedelta(seconds=3),
            )

        repaired_at = T0 + timedelta(seconds=3)
        digests = store.repair_corrupt_run_log(
            affected_markets=["US"],
            now=repaired_at,
        )

        assert len(digests) == 1 and len(digests[0]) == 64
        assert [item["id"] for item in store.run_records()] == [valid_run_id]
        quarantine = store.connection.execute(
            """
            SELECT run_id, payload_sha256, raw_payload
            FROM run_log_quarantine
            """
        ).fetchone()
        assert quarantine is not None
        assert quarantine["run_id"] == corrupt_run_id
        assert quarantine["payload_sha256"] == digests[0]
        assert secret in quarantine["raw_payload"]
        repaired_scope = store.get_protection_scope()
        assert repaired_scope is not None
        assert repaired_scope["market_epochs"]["US"] == repaired_at.isoformat(
            timespec="microseconds"
        )
        assert (
            repaired_scope["market_epochs"]["HK"]
            == original_scope["market_epochs"]["HK"]
        )
        assert (
            repaired_scope["market_instrument_hashes"]
            == original_scope["market_instrument_hashes"]
        )
        assert (
            repaired_scope["market_contract_hashes"]
            == original_scope["market_contract_hashes"]
        )
        assert store.integrity_incidents(scope="global")[0]["active"] is False
        assert not store.release_notification_claim(
            f"integrity:{incident['id']}", claim
        )

        store.record_run(
            "stock-scan:US",
            "success",
            {"private_payload": secret},
            now=T0 + timedelta(seconds=4),
        )
        recurrence = store.observe_integrity_incident(
            "global",
            "run_log",
            "state_corrupt",
            delivery_status="pending",
            now=T0 + timedelta(seconds=4),
        )
        assert recurrence["generation"] == 2


@pytest.mark.parametrize(
    ("jobs", "enabled_markets", "wrong_markets", "repair_markets"),
    [
        (("stock-scan:HK",), ("HK", "US"), ("US",), ("HK",)),
        (("stock-scan:ALL",), ("HK", "US"), ("US",), ("HK", "US")),
        (
            ("stock-scan:US", "stock-scan:HK"),
            ("HK", "US"),
            ("US",),
            ("HK", "US"),
        ),
        (("news-scan", "custom-job"), ("HK", "US"), ("US",), ()),
        (("stock-scan:US",), ("HK",), ("US",), ()),
    ],
)
def test_run_log_repair_derives_exact_current_market_responsibility(
    tmp_path: Path,
    jobs: tuple[str, ...],
    enabled_markets: tuple[str, ...],
    wrong_markets: tuple[str, ...],
    repair_markets: tuple[str, ...],
) -> None:
    ticker_by_market = {"US": ("AAPL",), "HK": ("00700",)}
    with StateStore(tmp_path / "state.db") as store:
        original_scope = store.set_protection_scope(
            enabled_markets,
            enabled_instruments_by_market={
                market: ticker_by_market[market] for market in enabled_markets
            },
            market_contract_hashes={
                market: ("a" if market == "US" else "b") * 64
                for market in enabled_markets
            },
            now=T0,
        )
        for job in jobs:
            run_id = store.record_run(
                job,
                "success",
                {"private_payload": "https://secret.example/token"},
                now=T0,
            )
            if job == "custom-job":
                store.connection.execute(
                    "UPDATE run_log SET status = 'bogus' WHERE id = ?",
                    (run_id,),
                )

        before_count = store.connection.execute(
            "SELECT COUNT(*) FROM run_log"
        ).fetchone()[0]
        with pytest.raises(ValueError, match="exactly match"):
            store.repair_corrupt_run_log(
                affected_markets=wrong_markets,
                now=T0 + timedelta(minutes=1),
            )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM run_log_quarantine"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.connection.execute("SELECT COUNT(*) FROM run_log").fetchone()[0]
            == before_count
        )
        assert store.get_protection_scope() == original_scope

        repaired_at = T0 + timedelta(minutes=1)
        digests = store.repair_corrupt_run_log(
            affected_markets=repair_markets,
            now=repaired_at,
        )
        repaired_scope = store.get_protection_scope()
        assert repaired_scope is not None
        assert len(digests) == len(jobs)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM run_log"
        ).fetchone()[0] == 0
        for market in enabled_markets:
            expected_epoch = (
                repaired_at.isoformat(timespec="microseconds")
                if market in repair_markets
                else original_scope["market_epochs"][market]
            )
            assert repaired_scope["market_epochs"][market] == expected_epoch


@pytest.mark.parametrize(
    "column",
    ["job", "status", "started_at", "finished_at", "detail"],
)
def test_run_log_blob_storage_classes_can_be_quarantined(
    tmp_path: Path,
    column: str,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={"US": "a" * 64},
            now=T0,
        )
        run_id = store.record_run(
            "stock-scan:US", "success", _valid_stock_run_detail(), now=T0
        )
        store.connection.execute(
            f"UPDATE run_log SET {column} = ? WHERE id = ?",  # noqa: S608
            (sqlite3.Binary(b"\x00\xff"), run_id),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.run_records(not_after=T0 + timedelta(minutes=1))

        digests = store.repair_corrupt_run_log(
            affected_markets=["US"],
            now=T0 + timedelta(minutes=1),
        )
        quarantined = store.connection.execute(
            "SELECT payload_sha256, raw_payload FROM run_log_quarantine"
        ).fetchone()

    assert digests == (quarantined["payload_sha256"],)
    assert '"type":"blob"' in quarantined["raw_payload"]
    assert '"value":"00ff"' in quarantined["raw_payload"]



def test_claim_schema_is_added_when_opening_an_existing_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "existing.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE preexisting (id INTEGER PRIMARY KEY)")

    with StateStore(database) as store:
        tables = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "preexisting" in tables
        assert "notification_claims" in tables


def test_two_connections_can_only_claim_one_signal_send(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    barrier = threading.Barrier(2)

    with StateStore(database) as first, StateStore(database) as second:

        def claim(store: StateStore) -> str | None:
            barrier.wait()
            return store.claim_signal_notification(
                "AAPL:BUY",
                True,
                "evidence-v1",
                24,
                lease_seconds=60,
                now=T0,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(claim, (first, second)))

        winners = [token for token in tokens if token is not None]
        assert len(winners) == 1
        assert (
            first.connection.execute(
                "SELECT COUNT(*) FROM notification_claims"
            ).fetchone()[0]
            == 1
        )

        second.mark_signal_notified(
            "AAPL:BUY",
            "evidence-v1",
            now=T0 + timedelta(seconds=1),
            claim_token=winners[0],
        )
        assert (
            first.connection.execute(
                "SELECT COUNT(*) FROM notification_claims"
            ).fetchone()[0]
            == 0
        )
        assert (
            first.claim_signal_notification(
                "AAPL:BUY",
                True,
                "evidence-v1",
                24,
                now=T0 + timedelta(hours=1),
            )
            is None
        )


def test_signal_claim_release_retries_and_reset_invalidates_token(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        first = store.claim_signal_notification(
            "AAPL:BUY", True, "v1", 24, lease_seconds=60, now=T0
        )
        assert first is not None
        assert not store.release_notification_claim("wrong-key", first)
        assert store.release_notification_claim("AAPL:BUY", first)
        assert not store.release_notification_claim("AAPL:BUY", first)

        retry = store.claim_signal_notification(
            "AAPL:BUY",
            True,
            "v1",
            24,
            lease_seconds=60,
            now=T0 + timedelta(seconds=1),
        )
        assert retry is not None and retry != first
        with pytest.raises(ValueError, match="invalid or expired"):
            store.mark_signal_notified(
                "AAPL:BUY",
                "v1",
                now=T0 + timedelta(seconds=2),
                claim_token=first,
            )

        assert (
            store.claim_signal_notification(
                "AAPL:BUY",
                False,
                "reset",
                24,
                now=T0 + timedelta(seconds=2),
            )
            is None
        )
        with pytest.raises(ValueError, match="inactive signal"):
            store.mark_signal_notified(
                "AAPL:BUY",
                "v1",
                now=T0 + timedelta(seconds=3),
                claim_token=retry,
            )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM notification_claims"
            ).fetchone()[0]
            == 0
        )


def test_expired_signal_lease_is_replaced_and_old_token_cannot_finish(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        old_token = store.claim_signal_notification(
            "00700:SELL", True, "v1", 24, lease_seconds=30, now=T0
        )
        assert old_token is not None
        assert (
            store.claim_signal_notification(
                "00700:SELL",
                True,
                "v1",
                24,
                lease_seconds=30,
                now=T0 + timedelta(seconds=29),
            )
            is None
        )

        new_token = store.claim_signal_notification(
            "00700:SELL",
            True,
            "v1",
            24,
            lease_seconds=30,
            now=T0 + timedelta(seconds=30),
        )
        assert new_token is not None and new_token != old_token
        with pytest.raises(ValueError, match="invalid or expired"):
            store.mark_signal_notified(
                "00700:SELL",
                "v1",
                now=T0 + timedelta(seconds=31),
                claim_token=old_token,
            )

        store.mark_signal_notified(
            "00700:SELL",
            "v1",
            now=T0 + timedelta(seconds=31),
            claim_token=new_token,
        )
        assert not store.should_notify_signal(
            "00700:SELL", True, "v1", 24, T0 + timedelta(hours=1)
        )


def test_unknown_signal_claim_does_not_write_state_or_claim(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        before = store.connection.total_changes
        assert (
            store.claim_signal_notification(
                "AAPL:BUY",
                None,
                "unavailable",
                24,
                now=T0,
            )
            is None
        )
        assert store.connection.total_changes == before
        assert (
            store.connection.execute("SELECT COUNT(*) FROM signal_state").fetchone()[0]
            == 0
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM notification_claims"
            ).fetchone()[0]
            == 0
        )


def test_news_claim_is_atomic_releasable_and_lease_safe(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    barrier = threading.Barrier(2)

    with StateStore(database) as first, StateStore(database) as second:

        def claim(store: StateStore) -> str | None:
            barrier.wait()
            return store.claim_news_notification(
                "article-hash",
                lease_seconds=30,
                now=T0,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(claim, (first, second)))

        old_token = next(token for token in tokens if token is not None)
        assert sum(token is not None for token in tokens) == 1
        assert second.release_notification_claim("article-hash", old_token)

        released_retry = first.claim_news_notification(
            "article-hash",
            lease_seconds=30,
            now=T0 + timedelta(seconds=1),
        )
        assert released_retry is not None and released_retry != old_token
        replacement = second.claim_news_notification(
            "article-hash",
            lease_seconds=30,
            now=T0 + timedelta(seconds=31),
        )
        assert replacement is not None and replacement != released_retry

        with pytest.raises(ValueError, match="invalid or expired"):
            first.mark_news_notified(
                "article-hash",
                now=T0 + timedelta(seconds=32),
                claim_token=released_retry,
            )
        second.mark_news_notified(
            "article-hash",
            now=T0 + timedelta(seconds=32),
            claim_token=replacement,
        )

        assert not first.is_news_new("article-hash")
        assert first.claim_news_notification("article-hash", now=T0) is None
        assert (
            first.connection.execute(
                "SELECT COUNT(*) FROM notification_claims"
            ).fetchone()[0]
            == 0
        )


def _protection_observation(
    observation_id: str,
    at: datetime,
    *,
    usable: int,
    full_scan: bool = False,
) -> BlindnessObservation:
    return BlindnessObservation(
        observation_id=observation_id,
        observed_at=at,
        enabled_instruments=1,
        usable_instruments=usable,
        full_coverage_scan=full_scan,
        unusable_tickers=("AAPL",) if not usable else (),
        reason_codes=("no_data",) if not usable else (),
    )


def test_protection_transition_and_distinct_scan_recovery_survive_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        blind, blind_event = store.observe_protection(
            _protection_observation("scan-0", T0, usable=0)
        )
        recovering, recovery_event = store.observe_protection(
            _protection_observation(
                "scan-1", T0 + timedelta(minutes=1), usable=1, full_scan=True
            )
        )
        replay, replay_event = store.observe_protection(
            _protection_observation(
                "scan-1", T0 + timedelta(minutes=1), usable=1, full_scan=True
            )
        )
        assert blind.snapshot.state is ProtectionState.BLIND
        assert recovering.snapshot.state is ProtectionState.RECOVERING
        assert replay.snapshot == recovering.snapshot
        assert blind_event is not None and recovery_event is not None
        assert replay_event is None

    with StateStore(database) as reopened:
        recovered, recovered_event = reopened.observe_protection(
            _protection_observation(
                "scan-2", T0 + timedelta(minutes=2), usable=1, full_scan=True
            )
        )
        assert recovered.snapshot.state is ProtectionState.HEALTHY
        assert recovered.snapshot.healthy_confirmations == 2
        assert recovered_event is not None
        assert len(reopened.protection_events()) == 3


def test_incident_edge_claim_retries_and_deduplicates_across_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        _transition, event_id = store.observe_protection(
            _protection_observation("scan-0", T0, usable=0),
            delivery_status="pending",
        )
        assert event_id is not None
        first = store.claim_incident_notification(event_id, now=T0)
        assert first is not None
        assert store.release_notification_claim(f"incident:{event_id}", first)

    with StateStore(database) as reopened:
        retry = reopened.claim_incident_notification(
            event_id, now=T0 + timedelta(seconds=1)
        )
        assert retry is not None and retry != first
        reopened.mark_incident_notified(
            event_id, retry, now=T0 + timedelta(seconds=2)
        )

    with StateStore(database) as final:
        assert (
            final.claim_incident_notification(
                event_id, now=T0 + timedelta(seconds=3)
            )
            is None
        )
        assert final.protection_events()[0]["notified_at"] is not None


def test_integrity_incident_preview_activation_and_restart_safe_claim(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as preview:
        first = preview.observe_integrity_incident(
            "global",
            "protection_state",
            "state_corrupt",
            delivery_status="suppressed",
            now=T0,
        )
        repeated = preview.observe_integrity_incident(
            "global",
            "protection_state",
            "state_corrupt",
            delivery_status="suppressed",
            now=T0 + timedelta(seconds=1),
        )
        activated = preview.observe_integrity_incident(
            "global",
            "protection_state",
            "state_corrupt",
            delivery_status="pending",
            now=T0 + timedelta(seconds=2),
        )
        assert first["id"] == repeated["id"] == activated["id"]
        assert activated["generation"] == 1
        assert activated["delivery_kind"] == "activation_sync"
        assert activated["delivery_status"] == "pending"
        assert len(preview.integrity_incidents(active_only=True)) == 1
        claim = preview.claim_integrity_notification(
            activated["id"], now=T0 + timedelta(seconds=3)
        )
        assert claim is not None
        assert preview.release_notification_claim(
            f"integrity:{activated['id']}", claim
        )

    with StateStore(database) as restarted:
        pending = restarted.pending_integrity_incidents(scope="global")
        assert [item["id"] for item in pending] == [activated["id"]]
        retry = restarted.claim_integrity_notification(
            activated["id"], now=T0 + timedelta(seconds=4)
        )
        assert retry is not None and retry != claim
        restarted.mark_integrity_notified(
            activated["id"], retry, now=T0 + timedelta(seconds=5)
        )

    with StateStore(database) as final:
        rows = final.integrity_incidents(scope="global")
        assert len(rows) == 1
        assert rows[0]["delivery_status"] == "sent"
        assert final.pending_integrity_incidents(scope="global") == []


def test_integrity_resolve_closes_generation_and_recurrence_opens_next(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        first = store.observe_integrity_incident(
            "global",
            "protection_scope",
            "state_corrupt",
            evidence_sha256="a" * 64,
            delivery_status="pending",
            now=T0,
        )
        claim = store.claim_integrity_notification(first["id"], now=T0)
        assert claim is not None
        assert store.resolve_integrity_incident(
            "global",
            "protection_scope",
            now=T0 + timedelta(seconds=1),
        )
        assert not store.release_notification_claim(
            f"integrity:{first['id']}", claim
        )
        assert store.pending_integrity_incidents(scope="global") == []

        recurrence = store.observe_integrity_incident(
            "global",
            "protection_scope",
            "state_corrupt",
            evidence_sha256="b" * 64,
            delivery_status="pending",
            now=T0 + timedelta(seconds=2),
        )
        rows = sorted(
            store.integrity_incidents(scope="global"),
            key=lambda item: item["generation"],
        )

    assert recurrence["id"] != first["id"]
    assert recurrence["generation"] == 2
    assert [item["active"] for item in rows] == [False, True]
    assert rows[0]["delivery_status"] == "suppressed"
    assert rows[1]["delivery_status"] == "pending"


@pytest.mark.parametrize(
    ("component", "corrupt_sql"),
    [
        (
            "protection_state",
            "UPDATE protection_state SET snapshot_json = '{broken'",
        ),
        (
            "protection_scope",
            "UPDATE protection_scope SET market_epochs_json = '{}'",
        ),
    ],
)
def test_explicit_repair_atomically_resolves_integrity_and_allows_recurrence(
    tmp_path: Path,
    component: str,
    corrupt_sql: str,
) -> None:
    database = tmp_path / f"{component}.db"
    with StateStore(database) as store:
        store.set_protection_scope(["US"], now=T0)
        store.observe_protection(
            _protection_observation(
                "baseline", T0, usable=1, full_scan=True
            )
        )
        detected = store.observe_integrity_incident(
            "global",
            component,
            "state_corrupt",
            delivery_status="pending",
            now=T0 + timedelta(seconds=1),
        )
        claim = store.claim_integrity_notification(
            detected["id"], now=T0 + timedelta(seconds=1)
        )
        assert claim is not None
        store.connection.execute(corrupt_sql)

        if component == "protection_scope":
            store.repair_corrupt_protection_scope(
                ["US"],
                enabled_instruments=1,
                now=T0 + timedelta(seconds=2),
            )
        else:
            store.repair_corrupt_protection_state(
                enabled_instruments=1,
                now=T0 + timedelta(seconds=2),
            )

        repaired = store.integrity_incidents(scope="global")
        first = next(item for item in repaired if item["id"] == detected["id"])
        assert first["active"] is False
        assert first["delivery_status"] == "suppressed"
        assert not store.release_notification_claim(
            f"integrity:{detected['id']}", claim
        )

        recurrence = store.observe_integrity_incident(
            "global",
            component,
            "state_corrupt",
            delivery_status="pending",
            now=T0 + timedelta(seconds=3),
        )

    assert recurrence["generation"] == 2
    assert recurrence["active"] is True
    assert recurrence["delivery_status"] == "pending"


def test_corrupt_pending_event_repair_quarantines_and_rearms_current_incident(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        _transition, event_id = store.observe_protection(
            _protection_observation("blind", T0, usable=0),
            delivery_status="pending",
        )
        assert event_id is not None
        integrity = store.observe_integrity_incident(
            "global",
            "protection_event",
            "state_corrupt",
            delivery_status="pending",
            now=T0 + timedelta(seconds=1),
        )
        claim = store.claim_integrity_notification(
            integrity["id"], now=T0 + timedelta(seconds=1)
        )
        assert claim is not None
        store.connection.execute(
            "UPDATE protection_events SET payload_json = '{broken' WHERE id = ?",
            (event_id,),
        )

        digest, repaired_event_id = store.repair_corrupt_protection_event(
            now=T0 + timedelta(seconds=2)
        )

        assert repaired_event_id == event_id
        assert len(digest) == 64
        repaired_integrity = store.integrity_incidents(scope="global")[0]
        assert repaired_integrity["active"] is False
        assert not store.release_notification_claim(
            f"integrity:{integrity['id']}", claim
        )
        quarantine = store.connection.execute(
            "SELECT payload_sha256, raw_payload FROM protection_event_quarantine"
        ).fetchone()
        assert quarantine is not None
        assert quarantine["payload_sha256"] == digest
        assert "{broken" in quarantine["raw_payload"]
        activation_id = store.ensure_current_incident_pending(
            now=T0 + timedelta(seconds=3)
        )
        assert activation_id is not None and activation_id != event_id
        pending = store.pending_current_incident_event("global")
        assert pending is not None
        assert pending["id"] == activation_id
        assert pending["event_type"] == "activation_sync"


def test_provider_runtime_state_restores_cache_and_corruption_fails_safe(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    key = ProviderKey(provider="probe", operation="quote", market="US")
    runtime = ProviderRuntime(clock=lambda: T0)
    first = runtime.execute(
        key, lambda: {"price": 100}, idempotent=True, cache_identity="AAPL"
    )
    assert first.value == {"price": 100}

    with StateStore(database) as store:
        store.save_provider_runtime_state(
            runtime.export_state().model_dump(mode="json"), now=T0
        )
        transition, _event_id = store.observe_protection(
            _protection_observation("scan-0", T0, usable=0)
        )
        assert transition.snapshot.state is ProtectionState.BLIND

    restored = ProviderRuntime(clock=lambda: T0 + timedelta(seconds=1))
    with StateStore(database) as reopened:
        payload = reopened.load_provider_runtime_state()
        assert payload is not None
        restored.import_state(payload)
        cached = restored.execute(
            key,
            lambda: (_ for _ in ()).throw(AssertionError("cache was not restored")),
            idempotent=True,
            cache_identity="AAPL",
        )
        assert cached.value == {"price": 100}
        assert cached.cache_state.value == "fresh"
        reopened.connection.execute(
            "UPDATE provider_runtime_state SET payload_json = '{broken'"
        )
        reopened.connection.execute(
            "UPDATE protection_state SET snapshot_json = '{broken'"
        )
        assert reopened.load_provider_runtime_state() is None
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM protection_state"
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(CorruptProtectionStateError):
            reopened.load_protection_state()
        with pytest.raises(CorruptProtectionStateError):
            reopened.observe_protection(
                _protection_observation(
                    "scan-healthy", T0 + timedelta(minutes=2), usable=1, full_scan=True
                )
            )
        repaired, digest, repair_event = reopened.repair_corrupt_protection_state(
            enabled_instruments=1,
            now=T0 + timedelta(minutes=3),
        )
        assert repaired.state is ProtectionState.BLIND
        assert repaired.reason_codes == ("state_repaired",)
        assert len(digest) == 64
        assert repair_event > 0
        quarantined = reopened.connection.execute(
            "SELECT payload_sha256, raw_payload FROM protection_state_quarantine"
        ).fetchone()
        assert quarantined["payload_sha256"] == digest
        assert quarantined["raw_payload"] == "{broken"

        first_recovery, _ = reopened.observe_protection(
            _protection_observation(
                "repair-scan-1",
                T0 + timedelta(minutes=4),
                usable=1,
                full_scan=True,
            )
        )
        replay, replay_event = reopened.observe_protection(
            _protection_observation(
                "repair-scan-1",
                T0 + timedelta(minutes=4),
                usable=1,
                full_scan=True,
            )
        )
        final, _ = reopened.observe_protection(
            _protection_observation(
                "repair-scan-2",
                T0 + timedelta(minutes=5),
                usable=1,
                full_scan=True,
            )
        )
        assert first_recovery.snapshot.state is ProtectionState.RECOVERING
        assert replay.snapshot.state is ProtectionState.RECOVERING
        assert replay_event is None
        assert final.snapshot.state is ProtectionState.HEALTHY


def test_provider_runtime_repair_quarantines_resets_epoch_and_rearms_generation(
    tmp_path: Path,
) -> None:
    secret = "https://provider.example/private-token"
    database = tmp_path / "state.db"
    key = ProviderKey(provider="probe", operation="quote", market="US")
    runtime = ProviderRuntime(clock=lambda: T0)
    runtime.execute(key, lambda: {"price": 100}, idempotent=True)
    with StateStore(database) as store:
        original_scope = store.set_protection_scope(
            ["HK", "US"],
            enabled_instruments_by_market={
                "HK": ("00700",),
                "US": ("AAPL",),
            },
            market_contract_hashes={"HK": "b" * 64, "US": "a" * 64},
            now=T0,
        )
        store.save_provider_runtime_state(
            runtime.export_state().model_dump(mode="json"),
            now=T0,
        )
        assert store.load_provider_runtime_state(strict=True, not_after=T0)
        with pytest.raises(ValueError, match="valid and must not be repaired"):
            store.repair_corrupt_provider_runtime_state(
                ["US"], now=T0 + timedelta(seconds=1)
            )

        store.connection.execute(
            "UPDATE provider_runtime_state SET payload_json = ?",
            (json.dumps({"circuits": {secret: {}}, "caches": {}, "observations": {}}),),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.load_provider_runtime_state(
                strict=True,
                not_after=T0 + timedelta(seconds=1),
            )
        incident = store.observe_integrity_incident(
            "global",
            "provider_runtime",
            "state_corrupt",
            delivery_status="pending",
            now=T0 + timedelta(seconds=1),
        )
        claim = store.claim_integrity_notification(
            incident["id"], now=T0 + timedelta(seconds=2)
        )
        assert claim is not None

        repaired_at = T0 + timedelta(seconds=3)
        with pytest.raises(ValueError, match="exactly match"):
            store.repair_corrupt_provider_runtime_state(
                ["US"],
                now=repaired_at,
            )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM provider_runtime_state"
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM provider_runtime_quarantine"
        ).fetchone()[0] == 0
        digest = store.repair_corrupt_provider_runtime_state(
            ["HK", "US"],
            now=repaired_at,
        )

        assert len(digest) == 64
        assert store.load_provider_runtime_state(strict=True) is None
        repaired_scope = store.get_protection_scope()
        assert repaired_scope is not None
        assert repaired_scope["market_epochs"]["US"] == repaired_at.isoformat(
            timespec="microseconds"
        )
        assert (
            repaired_scope["market_epochs"]["HK"]
            == repaired_at.isoformat(timespec="microseconds")
        )
        assert (
            repaired_scope["market_instrument_hashes"]
            == original_scope["market_instrument_hashes"]
        )
        assert (
            repaired_scope["market_contract_hashes"]
            == original_scope["market_contract_hashes"]
        )
        assert store.integrity_incidents(scope="global")[0]["active"] is False
        assert not store.release_notification_claim(
            f"integrity:{incident['id']}", claim
        )
        quarantine = store.connection.execute(
            """
            SELECT payload_sha256, raw_payload
            FROM provider_runtime_quarantine
            """
        ).fetchone()
        assert quarantine is not None
        assert quarantine["payload_sha256"] == digest
        assert secret in quarantine["raw_payload"]

        store.save_provider_runtime_state(
            {"circuits": {secret: {}}, "caches": {}, "observations": {}},
            now=T0 + timedelta(seconds=4),
        )
        recurrence = store.observe_integrity_incident(
            "global",
            "provider_runtime",
            "state_corrupt",
            delivery_status="pending",
            now=T0 + timedelta(seconds=4),
        )
        assert recurrence["generation"] == 2
        assert recurrence["delivery_status"] == "pending"


@pytest.mark.parametrize("column", ["payload_json", "updated_at"])
def test_provider_runtime_blob_storage_classes_can_be_quarantined(
    tmp_path: Path,
    column: str,
) -> None:
    runtime = ProviderRuntime(clock=lambda: T0)
    runtime.execute(
        ProviderKey(provider="probe", operation="quote", market="US"),
        lambda: {"price": 100},
        idempotent=True,
    )
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={"US": "a" * 64},
            now=T0,
        )
        store.save_provider_runtime_state(
            runtime.export_state().model_dump(mode="json"),
            now=T0,
        )
        store.connection.execute(
            f"UPDATE provider_runtime_state SET {column} = ?",  # noqa: S608
            (sqlite3.Binary(b"\x00\xff"),),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.load_provider_runtime_state(
                strict=True,
                not_after=T0 + timedelta(minutes=1),
            )

        digest = store.repair_corrupt_provider_runtime_state(
            ["US"],
            now=T0 + timedelta(minutes=1),
        )
        quarantined = store.connection.execute(
            "SELECT payload_sha256, raw_payload FROM provider_runtime_quarantine"
        ).fetchone()

    assert digest == quarantined["payload_sha256"]
    assert '"type":"blob"' in quarantined["raw_payload"]
    assert '"value":"00ff"' in quarantined["raw_payload"]


def test_legacy_incident_migration_never_backfills_deliverable_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE protection_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            previous_state TEXT,
            current_state TEXT NOT NULL,
            incident_id TEXT,
            occurred_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            notified_at TEXT
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO protection_events (
            scope_key, event_type, previous_state, current_state, incident_id,
            occurred_at, payload_json, notified_at
        ) VALUES ('global', 'blind', 'HEALTHY', 'BLIND', 'incident-1', ?, '{}', ?)
        """,
        (
            (T0.isoformat(), T0.isoformat()),
            ((T0 + timedelta(minutes=1)).isoformat(), None),
        ),
    )
    connection.commit()
    connection.close()

    with StateStore(database) as store:
        events = sorted(store.protection_events(), key=lambda item: item["id"])
        assert [event["delivery_status"] for event in events] == [
            "sent",
            "suppressed",
        ]
        assert store.claim_incident_notification(events[0]["id"], now=T0) is None
        assert store.claim_incident_notification(events[1]["id"], now=T0) is None


def test_preview_incident_disposition_is_crash_safe_and_never_backflows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as preview:
        _transition, preview_event_id = preview.observe_protection(
            _protection_observation("preview-scan", T0, usable=0),
            delivery_status="suppressed",
        )
        assert preview_event_id is not None

    # Model a restart into ACTIVE without any post-observe cleanup having run.
    with StateStore(database) as active:
        assert (
            active.claim_incident_notification(preview_event_id, now=T0) is None
        )
        assert active.pending_current_incident_event("global") is None
        _transition, active_event_id = active.observe_protection(
            _protection_observation(
                "active-scan",
                T0 + timedelta(minutes=1),
                usable=1,
                full_scan=True,
            ),
            delivery_status="pending",
        )
        assert active_event_id is not None
        pending = active.pending_current_incident_event("global")
        assert pending is not None
        assert pending["id"] == active_event_id
        statuses = {
            event["id"]: event["delivery_status"]
            for event in active.protection_events()
        }
        assert statuses[preview_event_id] == "suppressed"
        assert statuses[active_event_id] == "pending"


@pytest.mark.parametrize("kind", ["healthy", "unconfigured", "paused"])
def test_nonincident_edges_are_forced_suppressed_even_when_caller_is_active(
    tmp_path: Path,
    kind: str,
) -> None:
    if kind == "healthy":
        observation = _protection_observation(
            "initial",
            T0,
            usable=1,
            full_scan=True,
        )
    elif kind == "unconfigured":
        observation = BlindnessObservation(
            observation_id="initial",
            observed_at=T0,
            enabled_instruments=0,
            usable_instruments=0,
        )
    else:
        observation = BlindnessObservation(
            observation_id="initial",
            observed_at=T0,
            enabled_instruments=1,
            usable_instruments=1,
            paused=True,
        )
    with StateStore(tmp_path / f"{kind}.db") as store:
        _transition, event_id = store.observe_protection(
            observation,
            delivery_status="pending",
        )
        assert event_id is not None
        event = store.protection_events()[0]
        assert event["delivery_status"] == "suppressed"
        assert store.claim_incident_notification(event_id, now=T0) is None


def test_active_red_to_blue_coalesces_old_pending_edge(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        _blind, blind_event = store.observe_protection(
            _protection_observation("blind", T0, usable=0),
            delivery_status="pending",
        )
        assert blind_event is not None
        claim = store.claim_incident_notification(blind_event, now=T0)
        assert claim is not None
        _blue, blue_event = store.observe_protection(
            _protection_observation(
                "scan-1",
                T0 + timedelta(minutes=1),
                usable=1,
                full_scan=True,
            ),
            delivery_status="pending",
        )
        assert blue_event is not None
        events = sorted(store.protection_events(), key=lambda item: item["id"])
        assert [event["delivery_status"] for event in events] == [
            "suppressed",
            "pending",
        ]
        pending = store.pending_current_incident_event("global")
        assert pending is not None and pending["id"] == blue_event
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM notification_claims"
            ).fetchone()[0]
            == 0
        )


def test_active_startup_syncs_only_current_incident_and_is_restart_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as preview:
        _transition, preview_event = preview.observe_protection(
            _protection_observation("preview-blind", T0, usable=0),
            delivery_status="suppressed",
        )
        assert preview_event is not None

    with StateStore(database) as active:
        sync_event = active.ensure_current_incident_pending(
            now=T0 + timedelta(minutes=1)
        )
        assert sync_event is not None
        assert (
            active.ensure_current_incident_pending(
                now=T0 + timedelta(minutes=2)
            )
            == sync_event
        )
        assert len(active.protection_events()) == 2
        events = {event["id"]: event for event in active.protection_events()}
        assert events[preview_event]["delivery_status"] == "suppressed"
        assert events[sync_event]["event_type"] == "activation_sync"
        assert events[sync_event]["delivery_status"] == "pending"

    with StateStore(database) as restarted:
        assert (
            restarted.ensure_current_incident_pending(
                now=T0 + timedelta(minutes=3)
            )
            == sync_event
        )
        assert len(restarted.protection_events()) == 2

    healthy_db = tmp_path / "healthy.db"
    with StateStore(healthy_db) as healthy:
        healthy.observe_protection(
            _protection_observation("healthy", T0, usable=1, full_scan=True),
            delivery_status="pending",
        )
        assert healthy.ensure_current_incident_pending(now=T0) is None

    recovered_db = tmp_path / "recovered.db"
    with StateStore(recovered_db) as preview:
        preview.observe_protection(
            _protection_observation("blind", T0, usable=0),
            delivery_status="suppressed",
        )
        preview.observe_protection(
            _protection_observation(
                "scan-1",
                T0 + timedelta(minutes=1),
                usable=1,
                full_scan=True,
            ),
            delivery_status="suppressed",
        )
        preview.observe_protection(
            _protection_observation(
                "scan-2",
                T0 + timedelta(minutes=2),
                usable=1,
                full_scan=True,
            ),
            delivery_status="suppressed",
        )
    with StateStore(recovered_db) as active_after_recovery:
        assert (
            active_after_recovery.ensure_current_incident_pending(
                now=T0 + timedelta(minutes=3)
            )
            is None
        )
        assert all(
            event["delivery_status"] == "suppressed"
            for event in active_after_recovery.protection_events()
        )


def test_failed_recovered_edge_survives_restart_until_successful_send(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as active:
        active.observe_protection(
            _protection_observation("blind", T0, usable=0),
            delivery_status="pending",
        )
        active.observe_protection(
            _protection_observation(
                "scan-1",
                T0 + timedelta(minutes=1),
                usable=1,
                full_scan=True,
            ),
            delivery_status="pending",
        )
        _healthy, recovered_event = active.observe_protection(
            _protection_observation(
                "scan-2",
                T0 + timedelta(minutes=2),
                usable=1,
                full_scan=True,
            ),
            delivery_status="pending",
        )
        assert recovered_event is not None
        failed_claim = active.claim_incident_notification(
            recovered_event, now=T0 + timedelta(minutes=2)
        )
        assert failed_claim is not None
        assert active.release_notification_claim(
            f"incident:{recovered_event}", failed_claim
        )

    with StateStore(database) as restarted:
        assert (
            restarted.ensure_current_incident_pending(
                now=T0 + timedelta(minutes=3)
            )
            == recovered_event
        )
        retry = restarted.claim_incident_notification(
            recovered_event, now=T0 + timedelta(minutes=3)
        )
        assert retry is not None
        restarted.mark_incident_notified(
            recovered_event,
            retry,
            now=T0 + timedelta(minutes=3, seconds=1),
        )

    with StateStore(database) as final:
        assert (
            final.claim_incident_notification(
                recovered_event, now=T0 + timedelta(minutes=4)
            )
            is None
        )
        event = next(
            item
            for item in final.protection_events()
            if item["id"] == recovered_event
        )
        assert event["delivery_status"] == "sent"


def test_tampered_pending_incident_payload_is_typed_and_never_claimed(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        _transition, event_id = store.observe_protection(
            _protection_observation("blind", T0, usable=0),
            delivery_status="pending",
        )
        assert event_id is not None
        store.connection.execute(
            "UPDATE protection_events SET payload_json = '{broken' WHERE id = ?",
            (event_id,),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.pending_current_incident_event("global")
        with pytest.raises(CorruptProtectionStateError):
            store.claim_incident_notification(event_id, now=T0)
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM notification_claims"
            ).fetchone()[0]
            == 0
        )


def test_pending_incident_renders_latest_same_state_impact_not_stale_edge(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        first = BlindnessObservation(
            observation_id="partial-a",
            observed_at=T0,
            enabled_instruments=2,
            usable_instruments=1,
            unusable_tickers=("AAPL",),
            reason_codes=("partial_coverage",),
        )
        _transition, event_id = store.observe_protection(
            first,
            delivery_status="pending",
        )
        assert event_id is not None
        store.observe_protection(
            BlindnessObservation(
                observation_id="partial-b",
                observed_at=T0 + timedelta(minutes=1),
                enabled_instruments=2,
                usable_instruments=1,
                unusable_tickers=("MSFT",),
                reason_codes=("provider_degraded",),
                provider_degraded=True,
            ),
            delivery_status="pending",
        )
        pending = store.pending_current_incident_event("global")
        assert pending is not None and pending["id"] == event_id
        assert pending["payload"]["coverage"]["unusable_tickers"] == ["MSFT"]
        assert pending["payload"]["reason_codes"] == ["provider_degraded"]


def test_delivery_failure_is_sticky_until_real_success(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.record_delivery_state(
            "telegram",
            config_fingerprint=DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            attempted_at=T0,
            success=False,
            error_code="timeout",
            now=T0,
        )
        store.record_delivery_state(
            "telegram",
            config_fingerprint=DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            now=T0 + timedelta(minutes=1),
        )
        failed = store.delivery_states()["telegram"]
        assert failed["error_code"] == "timeout"
        assert failed["last_attempt_at"] == T0.isoformat(timespec="microseconds")

        succeeded_at = T0 + timedelta(minutes=2)
        store.record_delivery_state(
            "telegram",
            config_fingerprint=DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            attempted_at=succeeded_at,
            success=True,
            now=succeeded_at,
        )
        ready = store.delivery_states()["telegram"]
        assert ready["error_code"] is None
        assert ready["last_success_at"] == succeeded_at.isoformat(
            timespec="microseconds"
        )


def test_protection_scope_windows_and_delivery_state_are_secret_free(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scope = store.set_protection_scope(["US", "HK", "US"], now=T0)
        assert scope["enabled_markets"] == ["HK", "US"]
        store.record_protection_window(
            "US:2026-01-05",
            "US",
            T0,
            T0 + timedelta(minutes=15),
            "good",
            actual_at=T0 + timedelta(minutes=1),
            last_success_at=T0 + timedelta(minutes=1),
            enabled_instruments=2,
            usable_instruments=2,
            now=T0 + timedelta(minutes=1),
        )
        store.record_delivery_state(
            "heartbeat",
            config_fingerprint=DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            attempted_at=T0,
            success=False,
            error_code="timeout",
            now=T0,
        )
        assert store.protection_windows()[0]["coverage_ratio"] == 1.0
        heartbeat = store.delivery_states()["heartbeat"]
        assert heartbeat["configured"] is True
        assert heartbeat["error_code"] == "timeout"
        with pytest.raises(ValueError, match="low-cardinality"):
            store.record_delivery_state(
                "heartbeat",
                config_fingerprint=DELIVERY_FINGERPRINT,
                configured=True,
                mode="active",
                attempted_at=T0,
                success=False,
                error_code="https://secret.example/token",
                now=T0,
            )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("channel", "webhook"),
        ("generation", 0),
        ("configured", 2),
        ("mode", "maybe"),
        ("error_code", "https://heartbeat.example/private-token"),
        ("last_attempt_at", "2026-01-05T08:00:00"),
        ("last_success_at", "2026-01-05T08:01:00+00:00"),
    ],
)
def test_delivery_state_reader_fails_closed_on_tampering(
    tmp_path: Path,
    column: str,
    value: object,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.record_delivery_state(
            "telegram",
            config_fingerprint=DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            attempted_at=T0,
            success=False,
            error_code="timeout",
            now=T0,
        )
        store.connection.execute("PRAGMA ignore_check_constraints = ON")
        store.connection.execute(
            f"UPDATE delivery_state SET {column} = ?",  # noqa: S608 - fixed test matrix
            (value,),
        )

        with pytest.raises(CorruptProtectionStateError):
            store.delivery_states()


@pytest.mark.parametrize(
    ("channel", "mode"),
    [("webhook", "active"), ("telegram", "maybe")],
)
def test_delivery_state_writer_rejects_unknown_channel_or_mode(
    tmp_path: Path,
    channel: str,
    mode: str,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        with pytest.raises(ValueError):
            store.record_delivery_state(
                channel,
                config_fingerprint=DELIVERY_FINGERPRINT,
                configured=True,
                mode=mode,
                now=T0,
            )


def test_delivery_configuration_generation_change_clears_old_proof_atomically(
    tmp_path: Path,
) -> None:
    first_fingerprint = "a" * 64
    second_fingerprint = "b" * 64
    with StateStore(tmp_path / "state.db") as store:
        store.record_delivery_state(
            "telegram",
            config_fingerprint=first_fingerprint,
            configured=True,
            mode="active",
            attempted_at=T0,
            success=True,
            now=T0,
        )
        store.record_delivery_state(
            "telegram",
            config_fingerprint=first_fingerprint,
            configured=True,
            mode="active",
            now=T0 + timedelta(minutes=1),
        )
        retained = store.delivery_states()["telegram"]
        assert retained["last_success_at"] == T0.isoformat(timespec="microseconds")
        assert retained["generation"] == 1

        store.record_delivery_state(
            "telegram",
            config_fingerprint=second_fingerprint,
            configured=True,
            mode="active",
            now=T0 + timedelta(minutes=2),
        )
        reset = store.delivery_states()["telegram"]

    assert reset["config_fingerprint"] == second_fingerprint
    assert reset["generation"] == 2
    assert reset["last_attempt_at"] is None
    assert reset["last_success_at"] is None
    assert reset["success"] is None
    assert reset["error_code"] is None


def test_delivery_legacy_migration_keeps_fingerprint_unproven(tmp_path: Path) -> None:
    state_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(state_path)
    connection.execute(
        """
        CREATE TABLE delivery_state (
            channel TEXT PRIMARY KEY,
            configured INTEGER NOT NULL,
            mode TEXT NOT NULL,
            last_attempt_at TEXT,
            last_success_at TEXT,
            error_code TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    timestamp = T0.isoformat(timespec="microseconds")
    connection.execute(
        """
        INSERT INTO delivery_state (
            channel, configured, mode, last_attempt_at,
            last_success_at, error_code, updated_at
        ) VALUES ('telegram', 1, 'active', ?, ?, NULL, ?)
        """,
        (timestamp, timestamp, timestamp),
    )
    connection.commit()
    connection.close()

    with StateStore(state_path) as store:
        state = store.delivery_states()["telegram"]
        columns = {
            row[1]
            for row in store.connection.execute(
                "PRAGMA table_info(delivery_state)"
            )
        }

    assert "config_fingerprint" in columns
    assert "generation" in columns
    assert state["generation"] == 1
    assert state["config_fingerprint"] is None
    assert state["last_success_at"] == timestamp


@pytest.mark.parametrize("fingerprint", ["short", "A" * 64])
def test_delivery_writer_rejects_noncanonical_configuration_fingerprint(
    tmp_path: Path,
    fingerprint: str,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        with pytest.raises(ValueError, match="config_fingerprint"):
            store.record_delivery_state(
                "telegram",
                config_fingerprint=fingerprint,
                configured=True,
                mode="active",
                now=T0,
            )


def test_delivery_reader_fails_closed_on_fingerprint_tamper(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.record_delivery_state(
            "telegram",
            config_fingerprint=DELIVERY_FINGERPRINT,
            configured=True,
            mode="active",
            now=T0,
        )
        store.connection.execute(
            "UPDATE delivery_state SET config_fingerprint = 'private-token'"
        )

        with pytest.raises(CorruptProtectionStateError):
            store.delivery_states()


def test_protection_snapshot_rejects_semantically_impossible_states() -> None:
    healthy_snapshot = transition_protection(
        None,
        _protection_observation("healthy", T0, usable=1, full_scan=True),
    ).snapshot
    healthy_payload = healthy_snapshot.model_dump(mode="python")
    healthy_payload["coverage"] = {
        "enabled_instruments": 1,
        "usable_instruments": 0,
        "ratio": 0.0,
        "unusable_tickers": ["AAPL"],
    }
    with pytest.raises(ValueError, match="HEALTHY requires full coverage"):
        ProtectionSnapshot.model_validate(healthy_payload)

    missing_success = healthy_snapshot.model_dump(mode="python")
    missing_success["last_success_at"] = None
    with pytest.raises(ValueError, match="HEALTHY requires last_success_at"):
        ProtectionSnapshot.model_validate(missing_success)

    healthy_with_reason = healthy_snapshot.model_dump(mode="python")
    healthy_with_reason["reason_codes"] = ["stale_data"]
    with pytest.raises(ValueError, match="cannot retain degradation"):
        ProtectionSnapshot.model_validate(healthy_with_reason)

    blind = transition_protection(
        None,
        _protection_observation("blind", T0, usable=0),
    ).snapshot
    recovering = transition_protection(
        blind,
        _protection_observation(
            "recovering", T0 + timedelta(minutes=1), usable=1, full_scan=True
        ),
    ).snapshot
    recovering_payload = recovering.model_dump(mode="python")
    recovering_payload["healthy_confirmations"] = 0
    with pytest.raises(ValueError, match="exactly one"):
        ProtectionSnapshot.model_validate(recovering_payload)

    recovering_without_success = recovering.model_dump(mode="python")
    recovering_without_success["last_success_at"] = None
    with pytest.raises(ValueError, match="RECOVERING requires last_success_at"):
        ProtectionSnapshot.model_validate(recovering_without_success)

    degraded_payload = blind.model_dump(mode="python")
    degraded_payload.update(
        {
            "state": "DEGRADED",
            "color": "AMBER",
            "blind_started_at": None,
        }
    )
    with pytest.raises(ValueError, match="DEGRADED requires some usable"):
        ProtectionSnapshot.model_validate(degraded_payload)

    unconfigured_payload = healthy_snapshot.model_dump(mode="python")
    unconfigured_payload.update(
        {
            "state": "UNCONFIGURED",
            "color": "GRAY",
            "incident_id": None,
            "incident_started_at": None,
        }
    )
    with pytest.raises(ValueError, match="zero enabled"):
        ProtectionSnapshot.model_validate(unconfigured_payload)


def test_persisted_snapshot_scope_mismatch_is_typed_corruption(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        store.observe_protection(
            _protection_observation("healthy", T0, usable=1, full_scan=True)
        )
        raw = store.connection.execute(
            "SELECT snapshot_json FROM protection_state WHERE scope_key = 'global'"
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["scope"] = "other"
        store.connection.execute(
            "UPDATE protection_state SET snapshot_json = ? WHERE scope_key = 'global'",
            (json.dumps(payload),),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.load_protection_state("global")
        with pytest.raises(CorruptProtectionStateError):
            store.protection_states()
        with pytest.raises(CorruptProtectionStateError):
            store.observe_protection(
                _protection_observation(
                    "next", T0 + timedelta(minutes=1), usable=1
                )
            )
        repaired, digest, repair_event = store.repair_corrupt_protection_state(
            enabled_instruments=1,
            now=T0 + timedelta(minutes=2),
        )
        assert repaired.state is ProtectionState.BLIND
        assert len(digest) == 64
        assert repair_event > 0


def test_protection_scope_retains_per_market_epochs_across_membership_changes(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        initial = store.set_protection_scope(["US"], now=T0)
        expanded = store.set_protection_scope(
            ["US", "HK"], now=T0 + timedelta(hours=1)
        )
        reduced = store.set_protection_scope(
            ["HK"], now=T0 + timedelta(hours=2)
        )
        reexpanded = store.set_protection_scope(
            ["HK", "US"], now=T0 + timedelta(hours=3)
        )

    assert initial["market_epochs"] == {"US": initial["activated_at"]}
    assert expanded["market_epochs"]["US"] == initial["market_epochs"]["US"]
    assert expanded["market_epochs"]["HK"] == (
        T0 + timedelta(hours=1)
    ).isoformat(timespec="microseconds")
    assert reduced["market_epochs"] == {
        "HK": expanded["market_epochs"]["HK"]
    }
    assert reexpanded["market_epochs"]["HK"] == expanded["market_epochs"]["HK"]
    assert reexpanded["market_epochs"]["US"] == (
        T0 + timedelta(hours=3)
    ).isoformat(timespec="microseconds")


def test_protection_scope_identity_hash_retains_or_resets_only_changed_market(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        initial = store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            now=T0,
        )
        retained = store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            now=T0 + timedelta(hours=1),
        )
        changed = store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("MSFT",), "HK": ("00700",)},
            now=T0 + timedelta(hours=2),
        )

    assert retained["market_instrument_hashes"] == initial[
        "market_instrument_hashes"
    ]
    assert retained["market_epochs"] == initial["market_epochs"]
    assert changed["market_epochs"]["HK"] == initial["market_epochs"]["HK"]
    assert changed["market_epochs"]["US"] == (
        T0 + timedelta(hours=2)
    ).isoformat(timespec="microseconds")
    assert changed["market_instrument_hashes"]["US"] == instrument_set_hash(
        ("MSFT",)
    )


def test_hashed_scope_delete_and_readd_resets_only_readded_market(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        initial = store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            now=T0,
        )
        removed = store.set_protection_scope(
            ["HK"],
            enabled_instruments_by_market={"HK": ("00700",)},
            now=T0 + timedelta(hours=1),
        )
        readded = store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            now=T0 + timedelta(hours=2),
        )

    assert removed["market_epochs"]["HK"] == initial["market_epochs"]["HK"]
    assert readded["market_epochs"]["HK"] == initial["market_epochs"]["HK"]
    assert readded["market_epochs"]["US"] == (
        T0 + timedelta(hours=2)
    ).isoformat(timespec="microseconds")


def test_protection_scope_hash_is_stable_for_sorted_identity() -> None:
    assert instrument_set_hash(("MSFT", "AAPL")) == instrument_set_hash(
        ("AAPL", "MSFT")
    )


def test_legacy_scope_migration_marks_instrument_identity_unproven(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE protection_scope (
            scope_key TEXT PRIMARY KEY,
            activated_at TEXT NOT NULL,
            enabled_markets_json TEXT NOT NULL,
            market_epochs_json TEXT NOT NULL DEFAULT '{}',
            paused INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    timestamp = T0.isoformat(timespec="microseconds")
    connection.execute(
        "INSERT INTO protection_scope VALUES (?, ?, ?, ?, ?, ?)",
        ("global", timestamp, '["US"]', f'{{"US":"{timestamp}"}}', 0, timestamp),
    )
    connection.commit()
    connection.close()

    with StateStore(database) as store:
        scope = store.get_protection_scope()

    assert scope is not None
    assert scope["market_instrument_hashes"] == {}
    assert scope["market_contract_hashes"] == {}


def test_legacy_set_scope_call_does_not_erase_proven_identity(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        proven = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        legacy_call = store.set_protection_scope(
            ["US"], now=T0 + timedelta(hours=1)
        )

    assert legacy_call["market_instrument_hashes"] == proven[
        "market_instrument_hashes"
    ]
    assert legacy_call["market_epochs"] == proven["market_epochs"]


def test_scope_update_without_contract_map_preserves_proven_contract(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        proven = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={"US": "a" * 64},
            now=T0,
        )
        legacy_call = store.set_protection_scope(
            ["US"], now=T0 + timedelta(hours=1)
        )

    assert legacy_call["market_contract_hashes"] == proven[
        "market_contract_hashes"
    ]
    assert legacy_call["market_epochs"] == proven["market_epochs"]


def test_scope_identity_rejects_empty_market_and_non_ascii_ticker(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        with pytest.raises(ValueError, match="at least one ticker"):
            store.set_protection_scope(
                ["US"], enabled_instruments_by_market={"US": ()}, now=T0
            )
        with pytest.raises(ValueError, match="canonical identifier"):
            store.set_protection_scope(
                ["US"], enabled_instruments_by_market={"US": ("苹果",)}, now=T0
            )
        with pytest.raises(TypeError, match="sequence, not text"):
            store.set_protection_scope(
                ["US"], enabled_instruments_by_market={"US": "AAPL"}, now=T0
            )
        with pytest.raises(ValueError, match="canonical identifier"):
            store.set_protection_scope(
                ["US"], enabled_instruments_by_market={"US": ("_AAPL",)}, now=T0
            )


@pytest.mark.parametrize("scope_key", ["global", "market:US"])
def test_corrupt_scope_hash_is_rebuilt_by_explicit_repair(
    tmp_path: Path,
    scope_key: str,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            scope=scope_key,
            now=T0,
        )
        store.connection.execute(
            "UPDATE protection_scope SET market_instrument_hashes_json = ? WHERE scope_key = ?",
            ('{"US":"https://secret.example/token"}', scope_key),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.get_protection_scope(scope_key)
        repaired, _digest, _snapshot, _event_id = (
            store.repair_corrupt_protection_scope(
                ["US"],
                enabled_instruments=1,
                enabled_instruments_by_market={"US": ("AAPL",)},
                scope=scope_key,
                now=T0 + timedelta(minutes=1),
            )
        )

    assert repaired["market_instrument_hashes"] == {
        "US": instrument_set_hash(("AAPL",))
    }


def test_corrupt_scope_contract_is_rebuilt_with_exact_market_map(
    tmp_path: Path,
) -> None:
    contracts = {"US": "a" * 64, "HK": "b" * 64}
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            market_contract_hashes=contracts,
            now=T0,
        )
        store.connection.execute(
            "UPDATE protection_scope SET market_contract_hashes_json = ?",
            ('{"US":"https://secret.example/token"}',),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.get_protection_scope()
        repaired, _digest, _snapshot, _event_id = (
            store.repair_corrupt_protection_scope(
                ["US", "HK"],
                enabled_instruments=2,
                enabled_instruments_by_market={
                    "US": ("AAPL",),
                    "HK": ("00700",),
                },
                market_contract_hashes=contracts,
                now=T0 + timedelta(minutes=1),
            )
        )

    assert repaired["market_contract_hashes"] == contracts


def test_market_scope_contract_repair_writes_exact_map(tmp_path: Path) -> None:
    contracts = {"US": "c" * 64}
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes=contracts,
            scope="market:US",
            now=T0,
        )
        store.connection.execute(
            """
            UPDATE protection_scope SET market_contract_hashes_json = '{broken'
            WHERE scope_key = 'market:US'
            """
        )
        repaired, _digest, _snapshot, _event_id = (
            store.repair_corrupt_protection_scope(
                ["US"],
                enabled_instruments=1,
                enabled_instruments_by_market={"US": ("AAPL",)},
                market_contract_hashes=contracts,
                scope="market:US",
                now=T0 + timedelta(minutes=1),
            )
        )

    assert repaired["market_contract_hashes"] == contracts


@pytest.mark.parametrize(
    "payload",
    ["{broken", json.dumps({"US": "A" * 64}), json.dumps({"HK": "a" * 64})],
)
def test_scope_contract_corruption_is_typed(
    tmp_path: Path,
    payload: str,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            market_contract_hashes={"US": "a" * 64},
            now=T0,
        )
        store.connection.execute(
            "UPDATE protection_scope SET market_contract_hashes_json = ?",
            (payload,),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.get_protection_scope()


@pytest.mark.parametrize(
    "contracts",
    [{"HK": "a" * 64}, {"US": "A" * 64}, {"US": "short"}],
)
def test_scope_rejects_noncanonical_contract_map(
    tmp_path: Path,
    contracts: dict[str, str],
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        with pytest.raises(ValueError):
            store.set_protection_scope(
                ["US"], market_contract_hashes=contracts, now=T0
            )


def test_corrupt_protection_scope_fails_closed_until_explicit_quarantine_repair(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(["US"], now=T0)
        before_repair, _ = store.observe_protection(
            _protection_observation(
                "healthy-before-scope-corruption",
                T0 + timedelta(minutes=1),
                usable=1,
                full_scan=True,
            )
        )
        assert before_repair.snapshot.state is ProtectionState.HEALTHY
        store.connection.execute(
            "UPDATE protection_scope SET enabled_markets_json = '{broken'"
        )
        with pytest.raises(CorruptProtectionStateError):
            store.get_protection_scope()
        with pytest.raises(CorruptProtectionStateError):
            store.set_protection_scope(["US", "HK"], now=T0 + timedelta(hours=1))
        assert (
            store.connection.execute(
                "SELECT enabled_markets_json FROM protection_scope"
            ).fetchone()[0]
            == "{broken"
        )

        repaired, digest, sentinel, repair_event = (
            store.repair_corrupt_protection_scope(
                ["US", "HK"],
                enabled_instruments=1,
                now=T0 + timedelta(hours=2),
            )
        )
        expected_epoch = (T0 + timedelta(hours=2)).isoformat(
            timespec="microseconds"
        )
        assert repaired["market_epochs"] == {
            "HK": expected_epoch,
            "US": expected_epoch,
        }
        assert len(digest) == 64
        assert sentinel.state is ProtectionState.BLIND
        assert sentinel.reason_codes == ("state_repaired",)
        assert repair_event is not None
        quarantined = store.connection.execute(
            "SELECT payload_sha256, raw_payload FROM protection_scope_quarantine"
        ).fetchone()
        assert quarantined["payload_sha256"] == digest
        assert "{broken" in quarantined["raw_payload"]

        first, _ = store.observe_protection(
            _protection_observation(
                "scope-recovery-1",
                T0 + timedelta(hours=3),
                usable=1,
                full_scan=True,
            )
        )
        replay, replay_event = store.observe_protection(
            _protection_observation(
                "scope-recovery-1",
                T0 + timedelta(hours=3),
                usable=1,
                full_scan=True,
            )
        )
        second, _ = store.observe_protection(
            _protection_observation(
                "scope-recovery-2",
                T0 + timedelta(hours=4),
                usable=1,
                full_scan=True,
            )
        )
        assert first.snapshot.state is ProtectionState.RECOVERING
        assert replay.snapshot.state is ProtectionState.RECOVERING
        assert replay_event is None
        assert second.snapshot.state is ProtectionState.HEALTHY


def test_legacy_scope_migration_preserves_activation_as_market_epoch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-scope.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE protection_scope (
            scope_key TEXT PRIMARY KEY,
            activated_at TEXT NOT NULL,
            enabled_markets_json TEXT NOT NULL,
            paused INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    stamp = T0.isoformat(timespec="microseconds")
    connection.execute(
        """
        INSERT INTO protection_scope (
            scope_key, activated_at, enabled_markets_json, paused, updated_at
        ) VALUES ('global', ?, '["US"]', 0, ?)
        """,
        (stamp, stamp),
    )
    connection.commit()
    connection.close()

    with StateStore(database) as store:
        scope = store.get_protection_scope()
    assert scope is not None
    assert scope["market_epochs"] == {"US": stamp}


def test_protection_edges_and_incident_claims_are_atomic_across_connections(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    barrier = threading.Barrier(2)
    observation = _protection_observation("same-run", T0, usable=0)
    with StateStore(database) as first, StateStore(database) as second:

        def observe(store: StateStore):
            barrier.wait()
            return store.observe_protection(
                observation,
                delivery_status="pending",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(observe, (first, second)))
        event_ids = [event_id for _transition, event_id in results if event_id]
        assert len(event_ids) == 1
        assert len(first.protection_events()) == 1

        claim_barrier = threading.Barrier(2)

        def claim(store: StateStore):
            claim_barrier.wait()
            return store.claim_incident_notification(event_ids[0], now=T0)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, (first, second)))
        assert sum(token is not None for token in claims) == 1


def test_observation_ledger_makes_nonconsecutive_replay_globally_idempotent(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        blind_observation = _protection_observation("scan-a", T0, usable=0)
        blind, _ = store.observe_protection(blind_observation)
        recovering, _ = store.observe_protection(
            _protection_observation(
                "scan-b",
                T0 + timedelta(minutes=1),
                usable=1,
                full_scan=True,
            )
        )
        replay, replay_event = store.observe_protection(blind_observation)

        assert blind.snapshot.state is ProtectionState.BLIND
        assert recovering.snapshot.state is ProtectionState.RECOVERING
        assert recovering.snapshot.healthy_confirmations == 1
        assert replay.snapshot == recovering.snapshot
        assert replay_event is None
        assert len(store.protection_events()) == 2
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM protection_observations"
            ).fetchone()[0]
            == 2
        )


def test_observation_id_collision_is_typed_persisted_and_forces_red(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    first = _protection_observation(
        "same-id",
        T0,
        usable=1,
        full_scan=True,
    )
    conflicting = _protection_observation(
        "same-id",
        T0 + timedelta(minutes=1),
        usable=0,
    )
    with StateStore(database) as store:
        healthy, _ = store.observe_protection(first)
        assert healthy.snapshot.state is ProtectionState.HEALTHY
        with pytest.raises(
            ProtectionObservationCollisionError,
            match="observation id collision",
        ):
            store.observe_protection(conflicting, delivery_status="pending")
        sealed = store.load_protection_state()
        assert sealed is not None
        assert sealed.state is ProtectionState.BLIND
        assert sealed.reason_codes == ("observation_id_collision",)
        collisions = store.protection_observation_collisions()
        assert len(collisions) == 1
        assert set(collisions[0]) == {
            "id",
            "scope_key",
            "observation_id",
            "original_sha256",
            "conflicting_sha256",
            "detected_at",
        }

    with StateStore(database) as reopened:
        with pytest.raises(ProtectionObservationCollisionError):
            reopened.observe_protection(conflicting, delivery_status="pending")
        assert len(reopened.protection_observation_collisions()) == 1
        assert reopened.load_protection_state().state is ProtectionState.BLIND


def test_scan_replay_around_watchdog_is_idempotent_but_changed_payload_collides(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    scan_a = _protection_observation(
        "scan-a",
        T0 + timedelta(minutes=1),
        usable=1,
        full_scan=True,
    )
    with StateStore(database) as store:
        store.observe_protection(
            _protection_observation("initial-blind", T0, usable=0)
        )
        recovering, _ = store.observe_protection(scan_a)
        watchdog, _ = store.observe_protection(
            _protection_observation(
                "watchdog-b",
                T0 + timedelta(minutes=2),
                usable=1,
                full_scan=False,
            )
        )
        assert recovering.snapshot.state is ProtectionState.RECOVERING
        assert watchdog.snapshot.state is ProtectionState.RECOVERING
        assert watchdog.snapshot.healthy_confirmations == 1

    with StateStore(database) as reopened:
        replay, replay_event = reopened.observe_protection(scan_a)
        assert replay.snapshot.state is ProtectionState.RECOVERING
        assert replay.snapshot.healthy_confirmations == 1
        assert replay_event is None
        changed_scan_a = scan_a.model_copy(
            update={"observed_at": T0 + timedelta(minutes=3)}
        )
        with pytest.raises(ProtectionObservationCollisionError):
            reopened.observe_protection(changed_scan_a)
        sealed = reopened.load_protection_state()
        assert sealed is not None
        assert sealed.state is ProtectionState.BLIND


def test_protection_window_terminal_status_is_monotonic_and_late_good_is_bad(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as first, StateStore(database) as second:
        good_at = T0
        bad_at = T0 + timedelta(days=1)
        late_at = T0 + timedelta(days=2)
        early_at = T0 + timedelta(days=3)
        good_common = ("US", good_at, good_at + timedelta(minutes=15))
        bad_common = ("US", bad_at, bad_at + timedelta(minutes=15))
        late_common = ("US", late_at, late_at + timedelta(minutes=15))
        early_common = ("US", early_at, early_at + timedelta(minutes=15))
        first.record_protection_window(
            "US:2026-01-05", *good_common, "pending", now=good_at
        )
        first.record_protection_window(
            "US:2026-01-05",
            *good_common,
            "good",
            actual_at=good_at + timedelta(minutes=1),
            last_success_at=good_at + timedelta(minutes=1),
            enabled_instruments=1,
            usable_instruments=1,
            reason_codes=("initial_good",),
            now=good_at + timedelta(minutes=1),
        )
        second.record_protection_window(
            "US:2026-01-05",
            *good_common,
            "bad",
            enabled_instruments=1,
            usable_instruments=0,
            affected_tickers=("AAPL",),
            reason_codes=("late_failure",),
            now=good_at + timedelta(minutes=16),
        )

        first.record_protection_window(
            "US:2026-01-06",
            *bad_common,
            "bad",
            enabled_instruments=1,
            usable_instruments=0,
            affected_tickers=("AAPL",),
            reason_codes=("no_data",),
            now=bad_at + timedelta(minutes=16),
        )
        second.record_protection_window(
            "US:2026-01-06",
            *bad_common,
            "good",
            actual_at=bad_at + timedelta(minutes=1),
            enabled_instruments=1,
            usable_instruments=1,
            reason_codes=("recovered",),
            now=bad_at + timedelta(minutes=17),
        )
        first.record_protection_window(
            "US:2026-01-07",
            *late_common,
            "good",
            actual_at=late_at + timedelta(minutes=16),
            enabled_instruments=1,
            usable_instruments=1,
            now=late_at + timedelta(minutes=16),
        )
        first.record_protection_window(
            "US:2026-01-08",
            *early_common,
            "good",
            actual_at=early_at - timedelta(minutes=1),
            enabled_instruments=1,
            usable_instruments=1,
            now=early_at,
        )

        windows = {
            item["window_key"]: item for item in first.protection_windows()
        }
        assert {key: item["status"] for key, item in windows.items()} == {
            "US:2026-01-05": "good",
            "US:2026-01-06": "bad",
            "US:2026-01-07": "bad",
            "US:2026-01-08": "bad",
        }
        assert windows["US:2026-01-05"]["coverage_ratio"] == 1.0
        assert windows["US:2026-01-05"]["usable_instruments"] == 1
        assert windows["US:2026-01-05"]["actual_at"] == (
            good_at + timedelta(minutes=1)
        ).isoformat(timespec="microseconds")
        assert windows["US:2026-01-05"]["reasons"] == ["initial_good"]
        assert windows["US:2026-01-06"]["coverage_ratio"] == 0.0
        assert windows["US:2026-01-06"]["usable_instruments"] == 0
        assert windows["US:2026-01-06"]["affected"] == ["AAPL"]
        assert windows["US:2026-01-06"]["reasons"] == ["no_data"]


def test_two_connections_finalize_one_watchdog_market_batch_atomically(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    deadline = T0 + timedelta(minutes=15)
    finalized_at = deadline + timedelta(microseconds=1)
    barrier = threading.Barrier(2)
    with StateStore(database) as first, StateStore(database) as second:
        first.observe_protection(
            BlindnessObservation(
                scope="market:US",
                observation_id="scan:market:US:baseline",
                observed_at=T0,
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )

        def finalize(store: StateStore) -> tuple[tuple[str, ...], int | None]:
            barrier.wait()
            return store.finalize_overdue_market_windows(
                "US",
                (("US:2026-01-05", T0, deadline),),
                enabled_tickers=("AAPL",),
                now=finalized_at,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(finalize, (first, second)))

        assert sum(bool(keys) for keys, _event_id in results) == 1
        assert sum(event_id is not None for _keys, event_id in results) == 1
        window = first.protection_windows()[0]
        assert window["window_key"] == "US:2026-01-05"
        assert window["status"] == "bad"
        assert len(first.protection_events(scope="market:US")) == 2
        assert (
            first.connection.execute(
                """
                SELECT COUNT(*) FROM protection_observations
                WHERE scope_key = 'market:US'
                  AND observation_id LIKE 'deadline-batch:%'
                """
            ).fetchone()[0]
            == 1
        )


def _watchdog_incident_evidence(
    *,
    scope_generation: str = "a" * 64,
    enabled_instruments: int = 2,
    window_keys: tuple[str, ...] = ("US:2026-01-05",),
    affected_tickers: tuple[str, ...] = ("AAPL",),
) -> dict:
    return {
        "scope_generation": scope_generation,
        "enabled_instruments": enabled_instruments,
        "affected_tickers": affected_tickers,
        "markets": tuple(key.split(":", 1)[0] for key in window_keys),
        "window_keys": window_keys,
        "first_seen_at": T0,
    }


def _watchdog_scope_generation(scope: dict) -> str:
    payload = {
        "version": 1,
        "enabled_markets": scope["enabled_markets"],
        "market_epochs": scope["market_epochs"],
        "market_instrument_hashes": scope["market_instrument_hashes"],
        "market_contract_hashes": scope["market_contract_hashes"],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(
        b"alpha-guard:watchdog-scope:v1\x00" + encoded.encode()
    ).hexdigest()


def test_watchdog_incident_same_set_is_idempotent_and_preview_activates(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        preview = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(),
            delivery_status="suppressed",
            now=T0,
        )
        replay = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(),
            delivery_status="suppressed",
            now=T0 + timedelta(minutes=1),
        )
        activated_id = store.ensure_current_watchdog_incident_pending(
            now=T0 + timedelta(minutes=2)
        )
        activated = store.watchdog_incidents(active_only=True)[0]

    assert replay == preview
    assert activated_id == preview["id"]
    assert activated["generation"] == 1
    assert activated["state"] == "BLIND"
    assert activated["delivery_status"] == "pending"


def test_scope_generation_change_suppresses_unclaimed_watchdog_recovery_debt(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        initial_scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        incident = store.observe_watchdog_incident(
            scope_generation=_watchdog_scope_generation(initial_scope),
            enabled_instruments=1,
            affected_tickers=("AAPL",),
            markets=("US",),
            window_keys=("US:2026-01-05",),
            first_seen_at=T0,
            delivery_status="pending",
            now=T0,
        )
        claim = store.claim_watchdog_incident_notification(
            incident["id"], now=T0 + timedelta(seconds=1)
        )
        assert claim is not None
        store.mark_watchdog_incident_notified(
            incident["id"], claim, now=T0 + timedelta(seconds=2)
        )
        recovered = store.resolve_watchdog_incident(
            delivery_status="pending",
            now=T0 + timedelta(seconds=3),
        )
        assert recovered is not None
        assert recovered["delivery_status"] == "pending"

    with StateStore(state_path) as reopened:
        changed_scope = reopened.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
            now=T0 + timedelta(seconds=4),
        )
        history = reopened.watchdog_incidents()
        claims = reopened.connection.execute(
            "SELECT claim_key FROM notification_claims WHERE claim_key LIKE 'watchdog:%'"
        ).fetchall()

    assert _watchdog_scope_generation(changed_scope) != _watchdog_scope_generation(
        initial_scope
    )
    assert len(history) == 1
    assert history[0]["state"] == "RECOVERED"
    assert history[0]["active"] is False
    assert history[0]["delivery_status"] == "suppressed"
    assert history[0]["notified_at"] is None
    assert claims == []


def test_scope_generation_change_rejects_unknown_watchdog_generation_atomically(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        initial_scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        incident = store.observe_watchdog_incident(
            scope_generation=_watchdog_scope_generation(initial_scope),
            enabled_instruments=1,
            affected_tickers=("AAPL",),
            markets=("US",),
            window_keys=("US:2026-01-05",),
            first_seen_at=T0,
            delivery_status="pending",
            now=T0,
        )
        tampered_payload = dict(incident["payload"])
        tampered_payload["scope_generation"] = "f" * 64
        encoded = json.dumps(
            tampered_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        store.connection.execute(
            """
            UPDATE watchdog_incidents
            SET payload_json = ?, evidence_sha256 = ?
            WHERE id = ?
            """,
            (
                encoded,
                hashlib.sha256(encoded.encode()).hexdigest(),
                incident["id"],
            ),
        )

        with pytest.raises(CorruptProtectionStateError):
            store.set_protection_scope(
                ["US"],
                enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
                now=T0 + timedelta(seconds=1),
            )

        persisted_scope = store.get_protection_scope("global")
        persisted_watchdog = store.watchdog_incidents()[0]

    assert persisted_scope == initial_scope
    assert persisted_watchdog["payload"]["scope_generation"] == "f" * 64
    assert persisted_watchdog["delivery_status"] == "pending"


def test_scope_generation_chain_orders_same_timestamp_changes_by_sequence(
    tmp_path: Path,
) -> None:
    scenarios = (
        (
            {"US": ("AAPL",)},
            {"US": ("AAPL", "MSFT")},
            {"US": "a" * 64},
            {"US": "a" * 64},
        ),
        (
            {"US": ("AAPL", "MSFT")},
            {"US": ("AAPL",)},
            {"US": "a" * 64},
            {"US": "a" * 64},
        ),
        (
            {"US": ("AAPL",)},
            {"US": ("MSFT",)},
            {"US": "a" * 64},
            {"US": "a" * 64},
        ),
        (
            {"US": ("AAPL",)},
            {"US": ("AAPL",)},
            {"US": "a" * 64},
            {"US": "b" * 64},
        ),
    )
    digest_directions: set[bool] = set()
    for index, (old_ids, new_ids, old_contracts, new_contracts) in enumerate(
        scenarios
    ):
        state_path = tmp_path / f"state-{index}.db"
        with StateStore(state_path) as store:
            initial = store.set_protection_scope(
                ["US"],
                enabled_instruments_by_market=old_ids,
                market_contract_hashes=old_contracts,
                now=T0,
            )
            changed = store.set_protection_scope(
                ["US"],
                enabled_instruments_by_market=new_ids,
                market_contract_hashes=new_contracts,
                now=T0,
            )
            rows = store.connection.execute(
                """
                SELECT id, generation, activated_at, superseded_at
                FROM protection_scope_generations
                ORDER BY id
                """
            ).fetchall()
        with StateStore(state_path) as reopened:
            persisted = reopened.get_protection_scope()

        assert persisted == changed
        assert [row["id"] for row in rows] == [1, 2]
        assert rows[0]["generation"] == initial["watchdog_generation"]
        assert rows[1]["generation"] == changed["watchdog_generation"]
        assert rows[0]["superseded_at"] == rows[1]["activated_at"]
        assert rows[1]["superseded_at"] is None
        digest_directions.add(
            initial["watchdog_generation"] < changed["watchdog_generation"]
        )

    assert digest_directions == {False, True}


@pytest.mark.parametrize("delivery_kind", ["detected", "recovery"])
def test_scope_change_allows_only_exact_live_watchdog_claim_to_finish(
    tmp_path: Path,
    delivery_kind: str,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        initial_scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        incident = store.observe_watchdog_incident(
            scope_generation=initial_scope["watchdog_generation"],
            enabled_instruments=1,
            affected_tickers=("AAPL",),
            markets=("US",),
            window_keys=("US:2026-01-05",),
            first_seen_at=T0,
            delivery_status="pending",
            now=T0,
        )
        if delivery_kind == "recovery":
            detection_claim = store.claim_watchdog_incident_notification(
                incident["id"], now=T0 + timedelta(seconds=1)
            )
            assert detection_claim is not None
            store.mark_watchdog_incident_notified(
                incident["id"],
                detection_claim,
                now=T0 + timedelta(seconds=2),
            )
            recovered = store.resolve_watchdog_incident(
                delivery_status="pending",
                now=T0 + timedelta(seconds=3),
            )
            assert recovered is not None
        claim = store.claim_watchdog_incident_notification(
            incident["id"],
            lease_seconds=30,
            now=T0 + timedelta(seconds=4),
        )
        assert claim is not None

        changed_scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
            now=T0 + timedelta(seconds=5),
        )
        leased = store.read_claimed_watchdog_incident(
            incident["id"],
            claim,
            not_after=T0 + timedelta(seconds=6),
        )
        competing = store.claim_watchdog_incident_notification(
            incident["id"], now=T0 + timedelta(seconds=6)
        )
        assert leased is not None and leased["id"] == incident["id"]
        assert competing is None
        store.mark_watchdog_incident_notified(
            incident["id"], claim, now=T0 + timedelta(seconds=7)
        )
        persisted = store.watchdog_incidents()[0]
        claim_rows = store.connection.execute(
            "SELECT * FROM notification_claims WHERE claim_key = ?",
            (f"watchdog:{incident['id']}",),
        ).fetchall()
        generations = store.connection.execute(
            """
            SELECT generation, superseded_at
            FROM protection_scope_generations
            ORDER BY id
            """
        ).fetchall()

    assert changed_scope["watchdog_generation"] != initial_scope[
        "watchdog_generation"
    ]
    if delivery_kind == "detected":
        assert persisted["delivery_kind"] == "recovery"
        assert persisted["state"] == "RECOVERED"
        assert persisted["active"] is False
        assert persisted["delivery_status"] == "suppressed"
        assert persisted["detected_notified_at"] is not None
        assert persisted["notified_at"] is None
    else:
        assert persisted["delivery_kind"] == "recovery"
        assert persisted["state"] == "RECOVERED"
        assert persisted["active"] is False
        assert persisted["delivery_status"] == "sent"
    assert claim_rows == []
    assert [row["generation"] for row in generations] == [
        initial_scope["watchdog_generation"],
        changed_scope["watchdog_generation"],
    ]
    assert generations[0]["superseded_at"] is not None
    assert generations[1]["superseded_at"] is None


@pytest.mark.parametrize("delivery_kind", ["detected", "recovery"])
def test_expired_superseded_watchdog_claim_cannot_be_reclaimed(
    tmp_path: Path,
    delivery_kind: str,
) -> None:
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        incident = store.observe_watchdog_incident(
            scope_generation=scope["watchdog_generation"],
            enabled_instruments=1,
            affected_tickers=("AAPL",),
            markets=("US",),
            window_keys=("US:2026-01-05",),
            first_seen_at=T0,
            delivery_status="pending",
            now=T0,
        )
        if delivery_kind == "recovery":
            detection_claim = store.claim_watchdog_incident_notification(
                incident["id"], now=T0 + timedelta(milliseconds=100)
            )
            assert detection_claim is not None
            store.mark_watchdog_incident_notified(
                incident["id"],
                detection_claim,
                now=T0 + timedelta(milliseconds=200),
            )
            store.resolve_watchdog_incident(
                delivery_status="pending",
                now=T0 + timedelta(milliseconds=300),
            )
        claim = store.claim_watchdog_incident_notification(
            incident["id"],
            lease_seconds=2,
            now=T0 + timedelta(seconds=1),
        )
        assert claim is not None
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
            now=T0 + timedelta(seconds=2),
        )

    with StateStore(state_path) as reopened:
        assert (
            reopened.claim_watchdog_incident_notification(
                incident["id"], now=T0 + timedelta(milliseconds=2500)
            )
            is None
        )
        still_pending = reopened.pending_watchdog_incident()
        assert still_pending is not None
        assert (
            reopened.claim_watchdog_incident_notification(
                incident["id"], now=T0 + timedelta(seconds=4)
            )
            is None
        )
        retired = reopened.watchdog_incidents()[0]

    assert retired["state"] == "RECOVERED"
    assert retired["active"] is False
    assert retired["delivery_status"] == "suppressed"


def test_live_generation_claim_survives_two_same_timestamp_scope_changes(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        first_scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        incident = store.observe_watchdog_incident(
            scope_generation=first_scope["watchdog_generation"],
            enabled_instruments=1,
            affected_tickers=("AAPL",),
            markets=("US",),
            window_keys=("US:2026-01-05",),
            first_seen_at=T0,
            delivery_status="pending",
            now=T0,
        )
        claim = store.claim_watchdog_incident_notification(
            incident["id"],
            lease_seconds=30,
            now=T0 + timedelta(seconds=1),
        )
        assert claim is not None
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
            now=T0 + timedelta(seconds=2),
        )
        latest_scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("MSFT",)},
            now=T0 + timedelta(seconds=2),
        )
        leased = store.read_claimed_watchdog_incident(
            incident["id"],
            claim,
            not_after=T0 + timedelta(seconds=3),
        )
        assert leased is not None
        store.mark_watchdog_incident_notified(
            incident["id"], claim, now=T0 + timedelta(seconds=4)
        )
        generations = store.connection.execute(
            """
            SELECT id, generation, superseded_at
            FROM protection_scope_generations
            ORDER BY id
            """
        ).fetchall()

    assert len(generations) == 3
    assert generations[0]["generation"] == first_scope["watchdog_generation"]
    assert generations[-1]["generation"] == latest_scope["watchdog_generation"]
    assert [row["id"] for row in generations] == [1, 2, 3]
    assert generations[0]["superseded_at"] is not None
    assert generations[1]["superseded_at"] is not None
    assert generations[2]["superseded_at"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", sqlite3.Binary(b"not-a-generation")),
        ("activated_at", sqlite3.Binary(b"not-a-timestamp")),
        ("superseded_at", (T0 + timedelta(days=1)).isoformat()),
    ],
)
def test_scope_generation_ledger_corruption_is_quarantined_and_repaired(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    repaired_at = T0 + timedelta(minutes=2)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
            now=T0 + timedelta(minutes=1),
        )
        store.connection.execute(
            f"""
            UPDATE protection_scope_generations
            SET {field} = ? WHERE id = 1
            """,  # noqa: S608
            (value,),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.get_protection_scope(not_after=repaired_at)

        repaired, digest, _snapshot, _event = (
            store.repair_corrupt_protection_scope(
                ["US"],
                enabled_instruments=2,
                enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
                now=repaired_at,
            )
        )
        rows = store.connection.execute(
            "SELECT generation, superseded_at FROM protection_scope_generations"
        ).fetchall()
        quarantine = store.connection.execute(
            """
            SELECT payload_sha256, raw_payload
            FROM protection_scope_quarantine ORDER BY id DESC LIMIT 1
            """
        ).fetchone()

    assert len(digest) == 64
    assert repaired["watchdog_generation"] == rows[0]["generation"]
    assert len(rows) == 1 and rows[0]["superseded_at"] is None
    assert quarantine["payload_sha256"] == digest
    assert '"generations"' in quarantine["raw_payload"]


def test_scope_generation_repair_quarantines_untrusted_watchdog_outbox(
    tmp_path: Path,
) -> None:
    repaired_at = T0 + timedelta(minutes=2)
    with StateStore(tmp_path / "state.db") as store:
        scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        incident = store.observe_watchdog_incident(
            scope_generation=scope["watchdog_generation"],
            enabled_instruments=1,
            affected_tickers=("AAPL",),
            markets=("US",),
            window_keys=("US:2026-01-05",),
            first_seen_at=T0,
            delivery_status="pending",
            now=T0,
        )
        claim = store.claim_watchdog_incident_notification(
            incident["id"],
            now=T0 + timedelta(minutes=1),
        )
        assert claim is not None
        store.connection.execute(
            """
            UPDATE protection_scope_generations
            SET generation = ? WHERE id = 1
            """,
            (sqlite3.Binary(b"untrusted-generation"),),
        )

        repaired, digest, snapshot, _event = (
            store.repair_corrupt_protection_scope(
                ["US"],
                enabled_instruments=1,
                enabled_instruments_by_market={"US": ("AAPL",)},
                now=repaired_at,
            )
        )
        watchdog_count = store.connection.execute(
            "SELECT COUNT(*) FROM watchdog_incidents"
        ).fetchone()[0]
        claim_count = store.connection.execute(
            """
            SELECT COUNT(*) FROM notification_claims
            WHERE claim_key LIKE 'watchdog:%'
            """
        ).fetchone()[0]
        quarantine = store.connection.execute(
            """
            SELECT raw_payload FROM protection_scope_quarantine
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()["raw_payload"]
        first, _ = store.observe_protection(
            BlindnessObservation(
                scope="global",
                observation_id="post-repair-full-1",
                observed_at=repaired_at + timedelta(minutes=1),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )
        second, _ = store.observe_protection(
            BlindnessObservation(
                scope="global",
                observation_id="post-repair-full-2",
                observed_at=repaired_at + timedelta(minutes=2),
                enabled_instruments=1,
                usable_instruments=1,
                full_coverage_scan=True,
            )
        )

    assert len(digest) == 64
    assert repaired["watchdog_generation"] is not None
    assert snapshot.state.value == "BLIND"
    assert watchdog_count == 0
    assert claim_count == 0
    assert '"watchdog_incidents"' in quarantine
    assert '"watchdog_claims"' in quarantine
    assert first.snapshot.state.value == "RECOVERING"
    assert second.snapshot.state.value == "HEALTHY"


@pytest.mark.parametrize(
    "orphan_scope",
    [
        "https://watchdog.invalid/private-generation-token",
        sqlite3.Binary(b"\x00\xff"),
    ],
)
def test_scope_generation_full_table_rejects_orphan_without_leaking(
    tmp_path: Path,
    orphan_scope: object,
) -> None:
    secret = "private-generation-token"
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        store.connection.execute(
            """
            INSERT INTO protection_scope_generations (
                scope_key, generation, activated_at, superseded_at
            ) VALUES (?, ?, ?, NULL)
            """,
            (orphan_scope, "f" * 64, T0.isoformat()),
        )
        with pytest.raises(CorruptProtectionStateError) as caught:
            store.get_protection_scope(not_after=T0)

    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    ("orphan_scope", "orphan_activated_at"),
    [
        (
            "https://watchdog.invalid/private-orphan-token",
            T0.isoformat(),
        ),
        (
            sqlite3.Binary(b"private-orphan-token"),
            T0.isoformat(),
        ),
        (
            "future_orphan",
            (T0 + timedelta(days=1)).isoformat(),
        ),
    ],
    ids=("text", "blob", "future"),
)
def test_scope_generation_global_repair_sanitizes_orphan_row(
    tmp_path: Path,
    orphan_scope: object,
    orphan_activated_at: str,
) -> None:
    repaired_at = T0 + timedelta(minutes=1)
    secret = "private-orphan-token"
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        store.connection.execute(
            """
            INSERT INTO protection_scope_generations (
                scope_key, generation, activated_at, superseded_at
            ) VALUES (?, ?, ?, NULL)
            """,
            (orphan_scope, "f" * 64, orphan_activated_at),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.get_protection_scope(not_after=repaired_at)

        repaired, digest, snapshot, event_id = (
            store.repair_corrupt_protection_scope(
                ["US"],
                enabled_instruments=1,
                enabled_instruments_by_market={"US": ("AAPL",)},
                now=repaired_at,
            )
        )
        reloaded = store.get_protection_scope(not_after=repaired_at)
        generation_count = store.connection.execute(
            "SELECT COUNT(*) FROM protection_scope_generations"
        ).fetchone()[0]
        quarantine = store.connection.execute(
            """
            SELECT raw_payload FROM protection_scope_quarantine
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()

    outward = str((repaired, digest, snapshot, event_id))
    assert reloaded == repaired
    assert generation_count == 1
    assert quarantine is not None
    assert snapshot.state.value == "BLIND"
    assert secret not in outward


def test_global_repair_rebuilds_foreign_generation_without_losing_global_history(
    tmp_path: Path,
) -> None:
    repaired_at = T0 + timedelta(minutes=3)
    with StateStore(tmp_path / "state.db") as store:
        store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        global_scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
            now=T0 + timedelta(minutes=1),
        )
        foreign_scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            scope="market:US",
            now=T0 + timedelta(minutes=2),
        )
        global_rows_before = [
            tuple(row)
            for row in store.connection.execute(
                """
                SELECT id, scope_key, generation, activated_at, superseded_at
                FROM protection_scope_generations
                WHERE scope_key = 'global' ORDER BY id
                """
            )
        ]
        store.connection.execute(
            """
            UPDATE protection_scope_generations
            SET generation = ? WHERE scope_key = 'market:US'
            """,
            ("e" * 64,),
        )

        with pytest.raises(CorruptProtectionStateError):
            store.get_protection_scope("global", not_after=repaired_at)

        repaired, digest, _snapshot, _event = (
            store.repair_corrupt_protection_scope(
                ["US"],
                enabled_instruments=2,
                enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
                now=repaired_at,
            )
        )
        reloaded_global = store.get_protection_scope(
            "global", not_after=repaired_at
        )
        reloaded_foreign = store.get_protection_scope(
            "market:US", not_after=repaired_at
        )
        global_rows_after = [
            tuple(row)
            for row in store.connection.execute(
                """
                SELECT id, scope_key, generation, activated_at, superseded_at
                FROM protection_scope_generations
                WHERE scope_key = 'global' ORDER BY id
                """
            )
        ]
        foreign_rows = store.connection.execute(
            """
            SELECT generation, superseded_at
            FROM protection_scope_generations
            WHERE scope_key = 'market:US'
            """
        ).fetchall()

    assert len(digest) == 64
    assert repaired == global_scope
    assert reloaded_global == global_scope
    assert reloaded_foreign == foreign_scope
    assert global_rows_after == global_rows_before
    assert len(foreign_rows) == 1
    assert foreign_rows[0]["generation"] == foreign_scope["watchdog_generation"]
    assert foreign_rows[0]["superseded_at"] is None


@pytest.mark.parametrize(
    "unsupported_scope",
    [
        "unsupported_private_scope",
        sqlite3.Binary(b"private-scope-secret"),
    ],
    ids=("text", "blob"),
)
def test_global_repair_quarantines_unaddressable_scope_rows(
    tmp_path: Path,
    unsupported_scope: object,
) -> None:
    repaired_at = T0 + timedelta(minutes=1)
    with StateStore(tmp_path / "state.db") as store:
        global_scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL",)},
            now=T0,
        )
        global_row = store.connection.execute(
            "SELECT * FROM protection_scope WHERE scope_key = 'global'"
        ).fetchone()
        store.connection.execute(
            """
            INSERT INTO protection_scope (
                scope_key, activated_at, enabled_markets_json,
                market_epochs_json, market_instrument_hashes_json,
                market_contract_hashes_json, watchdog_generation,
                paused, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unsupported_scope,
                global_row["activated_at"],
                global_row["enabled_markets_json"],
                global_row["market_epochs_json"],
                global_row["market_instrument_hashes_json"],
                global_row["market_contract_hashes_json"],
                global_row["watchdog_generation"],
                global_row["paused"],
                global_row["updated_at"],
            ),
        )
        store.connection.execute(
            """
            INSERT INTO protection_scope_generations (
                scope_key, generation, activated_at, superseded_at
            ) VALUES (?, ?, ?, NULL)
            """,
            (
                unsupported_scope,
                global_scope["watchdog_generation"],
                T0.isoformat(),
            ),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.get_protection_scope("global", not_after=repaired_at)

        repaired, digest, snapshot, event_id = (
            store.repair_corrupt_protection_scope(
                ["US"],
                enabled_instruments=1,
                enabled_instruments_by_market={"US": ("AAPL",)},
                now=repaired_at,
            )
        )
        reloaded = store.get_protection_scope(
            "global", not_after=repaired_at
        )
        scope_count = store.connection.execute(
            "SELECT COUNT(*) FROM protection_scope"
        ).fetchone()[0]
        generation_count = store.connection.execute(
            "SELECT COUNT(*) FROM protection_scope_generations"
        ).fetchone()[0]
        quarantine = store.connection.execute(
            """
            SELECT raw_payload FROM protection_scope_quarantine
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()

    outward = str((repaired, digest, snapshot, event_id))
    assert reloaded == global_scope
    assert scope_count == 1
    assert generation_count == 1
    assert quarantine is not None
    assert "private-scope-secret" not in outward
    assert "unsupported_private_scope" not in outward


def test_watchdog_incident_recovers_once_and_new_window_opens_new_generation(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        first = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(),
            delivery_status="pending",
            now=T0,
        )
        recovered = store.resolve_watchdog_incident(
            delivery_status="pending",
            now=T0 + timedelta(minutes=5),
        )
        replay = store.resolve_watchdog_incident(
            delivery_status="pending",
            now=T0 + timedelta(minutes=6),
        )
        second = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(
                window_keys=("US:2026-01-06",),
            ),
            delivery_status="pending",
            now=T0 + timedelta(days=1),
        )

    assert first["generation"] == 1
    assert recovered is not None and recovered["state"] == "RECOVERED"
    assert recovered["active"] is False
    assert replay is None
    assert second["generation"] == 2
    assert second["active"] is True


def test_watchdog_incident_failed_claim_retries_same_generation(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
            now=T0,
        )
        incident = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(
                scope_generation=scope["watchdog_generation"]
            ),
            delivery_status="pending",
            now=T0,
        )
        first_claim = store.claim_watchdog_incident_notification(
            incident["id"], now=T0 + timedelta(seconds=1)
        )
        assert first_claim is not None
        assert store.release_notification_claim(
            f"watchdog:{incident['id']}", first_claim
        )

    with StateStore(state_path) as reopened:
        pending = reopened.pending_watchdog_incident()
        second_claim = reopened.claim_watchdog_incident_notification(
            incident["id"], now=T0 + timedelta(seconds=2)
        )
        assert pending is not None and pending["id"] == incident["id"]
        assert second_claim is not None and second_claim != first_claim
        reopened.mark_watchdog_incident_notified(
            incident["id"],
            second_claim,
            now=T0 + timedelta(seconds=3),
        )
        assert reopened.pending_watchdog_incident() is None


def test_watchdog_incident_validates_resolved_history_before_active_filter(
    tmp_path: Path,
) -> None:
    secret = "https://watcher.invalid/private-token"
    with StateStore(tmp_path / "state.db") as store:
        store.observe_watchdog_incident(
            **_watchdog_incident_evidence(),
            delivery_status="suppressed",
            now=T0,
        )
        store.resolve_watchdog_incident(
            delivery_status="suppressed",
            now=T0 + timedelta(minutes=1),
        )
        store.connection.execute(
            "UPDATE watchdog_incidents SET payload_json = ?",
            (json.dumps({"secret": secret}),),
        )

        with pytest.raises(CorruptProtectionStateError):
            store.watchdog_incidents(active_only=True)


def test_watchdog_active_evidence_expands_same_sent_generation_without_resend(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.db"
    with StateStore(state_path) as store:
        scope = store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={
                "US": ("AAPL",),
                "HK": ("00700",),
            },
            now=T0,
        )
        first = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(
                scope_generation=scope["watchdog_generation"]
            ),
            delivery_status="pending",
            now=T0,
        )
        claim = store.claim_watchdog_incident_notification(
            first["id"], now=T0 + timedelta(seconds=1)
        )
        assert claim is not None
        store.mark_watchdog_incident_notified(
            first["id"], claim, now=T0 + timedelta(seconds=2)
        )
        expanded = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(
                scope_generation=scope["watchdog_generation"],
                window_keys=("HK:2026-01-05", "US:2026-01-05"),
                affected_tickers=("00700", "AAPL"),
            ),
            delivery_status="pending",
            now=T0 + timedelta(minutes=1),
        )

        assert expanded["id"] == first["id"]
        assert expanded["generation"] == 1
        assert expanded["delivery_status"] == "sent"
        assert store.pending_watchdog_incident() is None

    with StateStore(state_path) as reopened:
        persisted = reopened.watchdog_incidents(active_only=True)[0]
        assert persisted["payload"]["markets"] == ["HK", "US"]
        assert persisted["payload"]["affected_tickers"] == ["00700", "AAPL"]
        assert persisted["payload"]["window_keys"] == [
            "HK:2026-01-05",
            "US:2026-01-05",
        ]


def test_watchdog_preview_expansion_needs_one_explicit_activation(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        first = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(),
            delivery_status="suppressed",
            now=T0,
        )
        expanded = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(
                window_keys=("HK:2026-01-05", "US:2026-01-05"),
                affected_tickers=("00700", "AAPL"),
            ),
            delivery_status="pending",
            now=T0 + timedelta(minutes=1),
        )
        assert expanded["id"] == first["id"]
        assert expanded["delivery_status"] == "suppressed"
        assert store.pending_watchdog_incident() is None

        activated = store.ensure_current_watchdog_incident_pending(
            now=T0 + timedelta(minutes=2)
        )
        assert activated == first["id"]
        assert store.pending_watchdog_incident()["id"] == first["id"]
        assert len(store.watchdog_incidents()) == 1


def test_watchdog_evidence_cannot_shrink_but_scope_change_starts_new_generation(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        first = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(
                window_keys=("HK:2026-01-05", "US:2026-01-05"),
                affected_tickers=("00700", "AAPL"),
            ),
            delivery_status="pending",
            now=T0,
        )
        with pytest.raises(ValueError, match="cannot shrink"):
            store.observe_watchdog_incident(
                **_watchdog_incident_evidence(),
                delivery_status="pending",
                now=T0 + timedelta(minutes=1),
            )
        second = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(
                scope_generation="b" * 64,
                enabled_instruments=1,
            ),
            delivery_status="pending",
            now=T0 + timedelta(minutes=2),
        )
        history = store.watchdog_incidents()

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert history[1]["state"] == "RECOVERED"
    assert history[1]["delivery_status"] == "suppressed"


def test_watchdog_preview_recovery_is_suppressed_and_inactive_blind_is_corrupt(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        incident = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(),
            delivery_status="suppressed",
            now=T0,
        )
        recovered = store.resolve_watchdog_incident(
            delivery_status="pending",
            now=T0 + timedelta(minutes=1),
        )
        assert recovered is not None
        assert recovered["delivery_status"] == "suppressed"
        assert store.pending_watchdog_incident() is None
        store.connection.execute(
            """
            UPDATE watchdog_incidents
            SET state = 'BLIND'
            WHERE id = ?
            """,
            (incident["id"],),
        )
        with pytest.raises(CorruptProtectionStateError):
            store.watchdog_incidents(active_only=True)


@pytest.mark.parametrize(
    "field",
    [
        "first_seen_at",
        "last_seen_at",
        "resolved_at",
        "detected_notified_at",
        "notified_at",
    ],
)
def test_watchdog_incident_rejects_future_evidence_before_filter(
    tmp_path: Path,
    field: str,
) -> None:
    cutoff = T0 + timedelta(minutes=10)
    future = T0 + timedelta(days=1)
    with StateStore(tmp_path / "state.db") as store:
        scope = store.set_protection_scope(
            ["US"],
            enabled_instruments_by_market={"US": ("AAPL", "MSFT")},
            now=T0,
        )
        incident = store.observe_watchdog_incident(
            **_watchdog_incident_evidence(
                scope_generation=scope["watchdog_generation"]
            ),
            delivery_status="pending",
            now=T0,
        )
        if field in {"notified_at", "detected_notified_at"}:
            claim = store.claim_watchdog_incident_notification(
                incident["id"], now=T0 + timedelta(seconds=1)
            )
            assert claim is not None
            store.mark_watchdog_incident_notified(
                incident["id"], claim, now=T0 + timedelta(seconds=2)
            )
        if field in {"resolved_at", "detected_notified_at"}:
            store.resolve_watchdog_incident(now=T0 + timedelta(seconds=3))
        store.connection.execute(
            f"UPDATE watchdog_incidents SET {field} = ?",  # noqa: S608
            (future.isoformat(),),
        )

        with pytest.raises(CorruptProtectionStateError):
            store.watchdog_incidents(active_only=True, not_after=cutoff)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payload_json", sqlite3.Binary(b"\x00\xff")),
        ("payload_json", '{"secret":"https://watcher.invalid/private"}'),
        ("evidence_sha256", sqlite3.Binary(b"not-a-digest")),
        ("last_seen_at", sqlite3.Binary(b"not-a-time")),
    ],
)
def test_watchdog_incident_tamper_is_typed_and_never_echoes_raw(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.observe_watchdog_incident(
            **_watchdog_incident_evidence(),
            delivery_status="suppressed",
            now=T0,
        )
        store.connection.execute(
            f"UPDATE watchdog_incidents SET {field} = ?",  # noqa: S608
            (value,),
        )

        with pytest.raises(CorruptProtectionStateError) as caught:
            store.watchdog_incidents(active_only=True, not_after=T0)

    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "tamper_sql",
    [
        "UPDATE protection_windows SET enabled_instruments = 0",
        "UPDATE protection_windows SET coverage_ratio = 0.5",
        "UPDATE protection_windows SET affected_json = '{broken'",
        "UPDATE protection_windows SET expected_at = '2026-01-05T08:00:00'",
        "UPDATE protection_windows SET market = 'HK'",
        "UPDATE protection_windows SET window_key = 'US:2026-01-06'",
        (
            "UPDATE protection_windows "
            "SET reasons_json = '[\"https://secret.invalid/token\"]'"
        ),
    ],
)
def test_protection_window_tamper_is_typed_fail_closed(
    tmp_path: Path,
    tamper_sql: str,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.record_protection_window(
            "US:2026-01-05",
            "US",
            T0,
            T0 + timedelta(minutes=15),
            "good",
            actual_at=T0 + timedelta(minutes=1),
            enabled_instruments=1,
            usable_instruments=1,
            now=T0 + timedelta(minutes=1),
        )
        store.connection.execute(tamper_sql)

        with pytest.raises(CorruptProtectionStateError):
            store.protection_windows()


@pytest.mark.parametrize(
    ("field", "value"),
    [("market", 7), ("market", None), ("window_key", 7)],
)
def test_window_validator_never_stringifies_non_text_identity(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.record_protection_window(
            "US:2026-01-05",
            "US",
            T0,
            T0 + timedelta(minutes=15),
            "good",
            actual_at=T0 + timedelta(minutes=1),
            enabled_instruments=1,
            usable_instruments=1,
            now=T0 + timedelta(minutes=1),
        )
        row = dict(
            store.connection.execute(
                "SELECT * FROM protection_windows"
            ).fetchone()
        )
    row[field] = value

    with pytest.raises(CorruptProtectionStateError):
        _validated_protection_window(row)


def test_window_filter_validates_corrupt_deadline_outside_requested_range(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.record_protection_window(
            "US:2026-01-05",
            "US",
            T0,
            T0 + timedelta(minutes=15),
            "good",
            actual_at=T0 + timedelta(minutes=1),
            enabled_instruments=1,
            usable_instruments=1,
            now=T0 + timedelta(minutes=1),
        )
        store.connection.execute(
            "UPDATE protection_windows SET deadline_at = '0000-hidden-corrupt'"
        )

        with pytest.raises(CorruptProtectionStateError):
            store.protection_windows(since=T0 + timedelta(days=365))


@pytest.mark.parametrize(
    "change",
    [
        "ttl",
        "future_tolerance",
        "fresh_cache",
        "stale_if_error",
        "required_field",
        "threshold",
        "cost_basis",
        "rule_group",
    ],
)
def test_protection_contract_policy_or_rule_semantics_change_version(
    change: str,
) -> None:
    base = _enabled_contract_rules()
    changed = base
    if change in {"ttl", "future_tolerance"}:
        freshness = base.reliability.freshness
        if change == "ttl":
            fields = dict(freshness.fields)
            fields["price"] = fields["price"].model_copy(
                update={
                    "max_observation_age_seconds": (
                        fields["price"].max_observation_age_seconds + 1
                    )
                }
            )
            freshness = freshness.model_copy(update={"fields": fields})
        else:
            freshness = freshness.model_copy(
                update={
                    "future_tolerance_seconds": (
                        freshness.future_tolerance_seconds + 1
                    )
                }
            )
        changed = base.model_copy(
            update={
                "reliability": base.reliability.model_copy(
                    update={"freshness": freshness}
                )
            }
        )
    elif change in {"fresh_cache", "stale_if_error"}:
        provider = base.reliability.provider
        provider = provider.model_copy(
            update=(
                {"fresh_cache_seconds": provider.fresh_cache_seconds + 1}
                if change == "fresh_cache"
                else {"stale_if_error_seconds": provider.stale_if_error_seconds + 1}
            )
        )
        changed = base.model_copy(
            update={
                "reliability": base.reliability.model_copy(
                    update={"provider": provider}
                )
            }
        )
    elif change in {"required_field", "threshold"}:
        instrument = base.watchlist["AAPL"]
        buy_rules = list(instrument.buy_rules)
        buy_rules[0] = buy_rules[0].model_copy(
            update=(
                {"type": "pe_below"}
                if change == "required_field"
                else {"value": buy_rules[0].value + 1}
            )
        )
        changed = base.model_copy(
            update={
                "watchlist": {
                    **base.watchlist,
                    "AAPL": instrument.model_copy(update={"buy_rules": buy_rules}),
                }
            }
        )
    elif change == "cost_basis":
        instrument = base.watchlist["AAPL"]
        assert instrument.cost_basis is not None
        changed = base.model_copy(
            update={
                "watchlist": {
                    **base.watchlist,
                    "AAPL": instrument.model_copy(
                        update={"cost_basis": instrument.cost_basis + 1}
                    ),
                }
            }
        )
    else:
        instrument = base.watchlist["AAPL"]
        moved = instrument.buy_rules[0]
        changed = base.model_copy(
            update={
                "watchlist": {
                    **base.watchlist,
                    "AAPL": instrument.model_copy(
                        update={
                            "buy_rules": instrument.buy_rules[1:],
                            "sell_rules": [*instrument.sell_rules, moved],
                        }
                    ),
                }
            }
        )

    before = _protection_contract_version(base, "US")
    after = _protection_contract_version(changed, "US")
    assert before != after


@pytest.mark.parametrize(
    "change",
    [
        "name",
        "note",
        "cooldown",
        "unused_cost_basis",
        "rule_order",
        "provider_timeout",
        "provider_retry",
    ],
)
def test_protection_contract_ignores_presentation_and_delivery_changes(
    change: str,
) -> None:
    base = _enabled_contract_rules()
    instrument = base.watchlist["AAPL"]
    if change in {"provider_timeout", "provider_retry"}:
        provider = base.reliability.provider
        provider = provider.model_copy(
            update=(
                {"request_timeout_seconds": provider.request_timeout_seconds + 1}
                if change == "provider_timeout"
                else {"max_attempts": provider.max_attempts + 1}
            )
        )
        changed = base.model_copy(
            update={
                "reliability": base.reliability.model_copy(
                    update={"provider": provider}
                )
            }
        )
        assert _protection_contract_version(base, "US") == (
            _protection_contract_version(changed, "US")
        )
        return
    if change == "name":
        updated_instrument = instrument.model_copy(update={"name": "Apple renamed"})
    elif change == "cooldown":
        updated_instrument = instrument.model_copy(
            update={"alert_cooldown_hours": 72.0}
        )
    elif change == "note":
        sell_rules = list(instrument.sell_rules)
        sell_rules[0] = sell_rules[0].model_copy(
            update={"note": "presentation text changed"}
        )
        updated_instrument = instrument.model_copy(
            update={"sell_rules": sell_rules}
        )
    elif change == "rule_order":
        updated_instrument = instrument.model_copy(
            update={
                "buy_rules": list(reversed(instrument.buy_rules)),
                "sell_rules": list(reversed(instrument.sell_rules)),
            }
        )
    else:
        without_drop = [
            rule for rule in instrument.sell_rules if rule.type != "price_drop_pct"
        ]
        updated_instrument = instrument.model_copy(
            update={
                "cost_basis": (instrument.cost_basis or 1) + 1,
                "sell_rules": without_drop,
            }
        )
        instrument = instrument.model_copy(update={"sell_rules": without_drop})
        base = base.model_copy(
            update={
                "watchlist": {**base.watchlist, "AAPL": instrument}
            }
        )
    changed = base.model_copy(
        update={
            "watchlist": {**base.watchlist, "AAPL": updated_instrument}
        }
    )

    assert _protection_contract_version(base, "US") == (
        _protection_contract_version(changed, "US")
    )


def test_protection_contract_schema_version_changes_hash(monkeypatch) -> None:
    import state.contract as contract

    rules = _enabled_contract_rules()
    before = contract.protection_contract_version(rules, "US")
    monkeypatch.setattr(contract, "PROTECTION_CONTRACT_SCHEMA_VERSION", 2)

    assert contract.protection_contract_version(rules, "US") != before


def test_protection_contract_change_resets_only_changed_market_epoch(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        initial = store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            market_contract_hashes={"US": "a" * 64, "HK": "b" * 64},
            now=T0,
        )
        changed = store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            market_contract_hashes={"US": "c" * 64, "HK": "b" * 64},
            now=T0 + timedelta(hours=1),
        )

    assert changed["market_epochs"]["US"] == (
        T0 + timedelta(hours=1)
    ).isoformat(timespec="microseconds")
    assert changed["market_epochs"]["HK"] == initial["market_epochs"]["HK"]
    assert changed["market_contract_hashes"] == {
        "US": "c" * 64,
        "HK": "b" * 64,
    }


def test_contract_scope_delete_and_readd_resets_only_readded_market(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        initial = store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            market_contract_hashes={"US": "a" * 64, "HK": "b" * 64},
            now=T0,
        )
        removed = store.set_protection_scope(
            ["HK"],
            enabled_instruments_by_market={"HK": ("00700",)},
            market_contract_hashes={"HK": "b" * 64},
            now=T0 + timedelta(hours=1),
        )
        readded = store.set_protection_scope(
            ["US", "HK"],
            enabled_instruments_by_market={"US": ("AAPL",), "HK": ("00700",)},
            market_contract_hashes={"US": "a" * 64, "HK": "b" * 64},
            now=T0 + timedelta(hours=2),
        )

    assert removed["market_epochs"]["HK"] == initial["market_epochs"]["HK"]
    assert readded["market_epochs"]["HK"] == initial["market_epochs"]["HK"]
    assert readded["market_epochs"]["US"] == (
        T0 + timedelta(hours=2)
    ).isoformat(timespec="microseconds")
