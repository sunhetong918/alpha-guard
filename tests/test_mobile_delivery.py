"""Durable, independent Telegram and WhatsApp delivery proofs."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from state import CorruptProtectionStateError, StateStore


NOW = datetime(2026, 8, 12, 2, 0, tzinfo=UTC)
TELEGRAM_V1 = "a" * 64
WHATSAPP_V1 = "b" * 64
WHATSAPP_V2 = "c" * 64


def test_mobile_channels_retry_independently_without_duplicate_send(
    tmp_path,
) -> None:
    path = tmp_path / "state.db"
    with StateStore(path) as store:
        telegram_claim = store.claim_outbound_delivery(
            "signal:AAPL:buy:edge1",
            "telegram",
            TELEGRAM_V1,
            now=NOW,
        )
        whatsapp_claim = store.claim_outbound_delivery(
            "signal:AAPL:buy:edge1",
            "whatsapp",
            WHATSAPP_V1,
            now=NOW,
        )
        assert telegram_claim is not None
        assert whatsapp_claim is not None

        store.mark_outbound_delivery_sent(
            "signal:AAPL:buy:edge1",
            "telegram",
            TELEGRAM_V1,
            telegram_claim,
            now=NOW + timedelta(seconds=1),
        )
        store.mark_outbound_delivery_failed(
            "signal:AAPL:buy:edge1",
            "whatsapp",
            WHATSAPP_V1,
            whatsapp_claim,
            "timeout",
            now=NOW + timedelta(seconds=2),
        )

    with StateStore(path) as restarted:
        assert (
            restarted.claim_outbound_delivery(
                "signal:AAPL:buy:edge1",
                "telegram",
                TELEGRAM_V1,
                now=NOW + timedelta(minutes=1),
            )
            is None
        )
        retry = restarted.claim_outbound_delivery(
            "signal:AAPL:buy:edge1",
            "whatsapp",
            WHATSAPP_V1,
            now=NOW + timedelta(minutes=1),
        )
        assert retry is not None
        restarted.mark_outbound_delivery_sent(
            "signal:AAPL:buy:edge1",
            "whatsapp",
            WHATSAPP_V1,
            retry,
            now=NOW + timedelta(minutes=1, seconds=1),
        )
        assert [
            item["status"]
            for item in restarted.outbound_deliveries(
                business_key="signal:AAPL:buy:edge1"
            )
        ] == ["sent", "sent"]


def test_channel_configuration_rotation_resets_only_that_channel(tmp_path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        for channel, fingerprint in (
            ("telegram", TELEGRAM_V1),
            ("whatsapp", WHATSAPP_V1),
        ):
            claim = store.claim_outbound_delivery(
                "incident:42", channel, fingerprint, now=NOW
            )
            assert claim is not None
            store.mark_outbound_delivery_sent(
                "incident:42",
                channel,
                fingerprint,
                claim,
                now=NOW + timedelta(seconds=1),
            )

        assert (
            store.claim_outbound_delivery(
                "incident:42",
                "telegram",
                TELEGRAM_V1,
                now=NOW + timedelta(minutes=1),
            )
            is None
        )
        assert (
            store.claim_outbound_delivery(
                "incident:42",
                "whatsapp",
                WHATSAPP_V2,
                now=NOW + timedelta(minutes=1),
            )
            is not None
        )
        rows = {
            item["channel"]: item for item in store.outbound_deliveries()
        }
        assert rows["telegram"]["status"] == "sent"
        assert rows["whatsapp"]["status"] == "pending"
        assert rows["whatsapp"]["sent_at"] is None


def test_outbound_claim_is_atomic_across_connections(tmp_path) -> None:
    path = tmp_path / "state.db"
    with StateStore(path) as first, StateStore(path) as second:
        claim = first.claim_outbound_delivery(
            "news:article1", "telegram", TELEGRAM_V1, now=NOW
        )
        assert claim is not None
        assert (
            second.claim_outbound_delivery(
                "news:article1", "telegram", TELEGRAM_V1, now=NOW
            )
            is None
        )


def test_outbound_reader_validates_rows_before_filtering(tmp_path) -> None:
    path = tmp_path / "state.db"
    with StateStore(path) as store:
        store.claim_outbound_delivery(
            "signal:safe", "telegram", TELEGRAM_V1, now=NOW
        )
        store.claim_outbound_delivery(
            "signal:hidden", "whatsapp", WHATSAPP_V1, now=NOW
        )
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            UPDATE outbound_deliveries SET error_code = ?
            WHERE business_key = 'signal:hidden'
            """,
            ("https://watcher.example/private-token",),
        )
        connection.commit()
    finally:
        connection.close()

    with StateStore(path) as store:
        with pytest.raises(CorruptProtectionStateError):
            store.outbound_deliveries(business_key="signal:safe")


@pytest.mark.parametrize(
    "business_key",
    ["", "has space", "秘密", "x" * 241],
)
def test_outbound_business_key_is_bounded_ascii(tmp_path, business_key) -> None:
    with StateStore(tmp_path / "state.db") as store:
        with pytest.raises(ValueError):
            store.claim_outbound_delivery(
                business_key,
                "telegram",
                TELEGRAM_V1,
                now=NOW,
            )
