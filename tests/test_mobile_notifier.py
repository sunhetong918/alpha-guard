from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from config import Settings
from notifier.mobile import deliver_mobile
from notifier.whatsapp import WhatsAppDeliveryResult
from state import StateStore


NOW = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        notifications_enabled=True,
        telegram_bot_token="telegram-secret",
        telegram_chat_id="chat-id",
        whatsapp_enabled=True,
        whatsapp_access_token="whatsapp-secret",
        whatsapp_phone_number_id="123456789",
        whatsapp_default_to="8613800000000",
        whatsapp_signal_template_name="alpha_guard_signal",
        whatsapp_incident_template_name="alpha_guard_incident",
        whatsapp_trust_template_name="alpha_guard_trust",
    )


def test_fanout_retries_only_failed_channel_after_restart(tmp_path) -> None:
    calls = {"telegram": 0, "whatsapp": 0}

    async def telegram() -> None:
        calls["telegram"] += 1

    def whatsapp_fail() -> WhatsAppDeliveryResult:
        calls["whatsapp"] += 1
        return WhatsAppDeliveryResult(False, "timeout", retryable=True)

    path = tmp_path / "state.db"
    with StateStore(path) as store:
        first = asyncio.run(deliver_mobile(
            store,
            business_key="signal:AAPL:buy:edge1",
            kind="signal",
            payload={"ticker": "AAPL"},
            settings=_settings(),
            now=NOW,
            clock=lambda: NOW + timedelta(seconds=1),
            telegram_sender=telegram,
            whatsapp_sender=whatsapp_fail,
        ))
    assert first.accepted is False
    assert first.errors == {"whatsapp": "timeout"}

    def whatsapp_success() -> WhatsAppDeliveryResult:
        calls["whatsapp"] += 1
        return WhatsAppDeliveryResult(True, "accepted", status_code=200)

    with StateStore(path) as restarted:
        second = asyncio.run(deliver_mobile(
            restarted,
            business_key="signal:AAPL:buy:edge1",
            kind="signal",
            payload={"ticker": "AAPL"},
            settings=_settings(),
            now=NOW + timedelta(minutes=1),
            clock=lambda: NOW + timedelta(minutes=1, seconds=1),
            telegram_sender=telegram,
            whatsapp_sender=whatsapp_success,
        ))
    assert second.accepted is True
    assert calls == {"telegram": 1, "whatsapp": 2}


def test_configuration_rotation_reproves_only_rotated_channel(tmp_path) -> None:
    calls = {"telegram": 0, "whatsapp": 0}

    async def telegram() -> None:
        calls["telegram"] += 1

    def whatsapp() -> WhatsAppDeliveryResult:
        calls["whatsapp"] += 1
        return WhatsAppDeliveryResult(True, "accepted", status_code=200)

    path = tmp_path / "state.db"
    original = _settings()
    with StateStore(path) as store:
        assert (
            asyncio.run(deliver_mobile(
                store,
                business_key="incident:42",
                kind="incident",
                payload={"state": "BLIND"},
                settings=original,
                now=NOW,
                clock=lambda: NOW + timedelta(seconds=1),
                telegram_sender=telegram,
                whatsapp_sender=whatsapp,
            ))
        ).accepted
    rotated = original.model_copy(
        update={"whatsapp_default_to": "8613900000000"}
    )
    with StateStore(path) as store:
        assert (
            asyncio.run(deliver_mobile(
                store,
                business_key="incident:42",
                kind="incident",
                payload={"state": "BLIND"},
                settings=rotated,
                now=NOW + timedelta(minutes=1),
                clock=lambda: NOW + timedelta(minutes=1, seconds=1),
                telegram_sender=telegram,
                whatsapp_sender=whatsapp,
            ))
        ).accepted
    assert calls == {"telegram": 1, "whatsapp": 2}


def test_no_configured_mobile_channel_never_claims_success(tmp_path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        report = asyncio.run(deliver_mobile(
            store,
            business_key="trust:preview",
            kind="trust",
            payload="trust",
            settings=Settings(),
            now=NOW,
        ))
        assert report.accepted is False
        assert report.required_channels == ()
        assert store.outbound_deliveries() == []


def test_whatsapp_only_configuration_is_a_complete_mobile_delivery(tmp_path) -> None:
    settings = _settings().model_copy(
        update={
            "notifications_enabled": False,
            "telegram_bot_token": None,
            "telegram_chat_id": None,
        }
    )
    calls = 0

    def whatsapp() -> WhatsAppDeliveryResult:
        nonlocal calls
        calls += 1
        return WhatsAppDeliveryResult(True, "accepted", status_code=200)

    with StateStore(tmp_path / "state.db") as store:
        report = asyncio.run(
            deliver_mobile(
                store,
                business_key="trust:whatsapp-only",
                kind="trust",
                payload="trust",
                settings=settings,
                now=NOW,
                whatsapp_sender=whatsapp,
            )
        )

    assert report.accepted is True
    assert report.required_channels == ("whatsapp",)
    assert calls == 1
