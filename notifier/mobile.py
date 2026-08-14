"""Durable, independent fan-out across Telegram and WhatsApp."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from config import Settings
from state import StateStore

from .heartbeat import delivery_config_fingerprints
from .telegram_bot import send_incident, send_message, send_news_alert, send_signal
from .whatsapp import WhatsAppDeliveryResult, WhatsAppNotifier

MobileChannel = Literal["telegram", "whatsapp"]
MobileMessageKind = Literal["signal", "incident", "news", "summary", "trust"]
TelegramSender = Callable[[], Awaitable[None]]
WhatsAppSender = Callable[[], WhatsAppDeliveryResult]


@dataclass(frozen=True, slots=True)
class ChannelDelivery:
    channel: MobileChannel
    attempted: bool
    accepted: bool
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class MobileDeliveryReport:
    business_key: str
    required_channels: tuple[MobileChannel, ...]
    channels: tuple[ChannelDelivery, ...]

    @property
    def accepted(self) -> bool:
        return bool(self.required_channels) and all(
            item.accepted for item in self.channels
        )

    @property
    def attempted(self) -> bool:
        return any(item.attempted for item in self.channels)

    @property
    def errors(self) -> dict[str, str]:
        return {
            item.channel: item.error_code
            for item in self.channels
            if item.error_code is not None
        }


def configured_mobile_channels(settings: Settings) -> tuple[MobileChannel, ...]:
    channels: list[MobileChannel] = []
    if (
        settings.notifications_enabled
        and settings.telegram_bot_token is not None
        and bool((settings.telegram_chat_id or "").strip())
    ):
        channels.append("telegram")
    if settings.whatsapp_enabled:
        channels.append("whatsapp")
    return tuple(channels)


async def deliver_mobile(
    store: StateStore,
    *,
    business_key: str,
    kind: MobileMessageKind,
    payload: Mapping[str, Any] | str,
    settings: Settings,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
    telegram_sender: TelegramSender | None = None,
    whatsapp_sender: WhatsAppSender | None = None,
    record_delivery_state: bool = True,
) -> MobileDeliveryReport:
    """Deliver one business edge exactly once per enabled channel generation."""

    started_at = _aware(now)
    # A caller-supplied historical/replay clock must never be mixed with the
    # wall clock because claim chronology is validated transactionally.
    runtime_clock = clock or (lambda: started_at)
    fingerprints = delivery_config_fingerprints(settings)
    required = configured_mobile_channels(settings)
    results: list[ChannelDelivery] = []
    for channel in required:
        fingerprint = fingerprints[channel]
        claim = store.claim_outbound_delivery(
            business_key,
            channel,
            fingerprint,
            now=started_at,
        )
        if claim is None:
            existing = {
                item["channel"]: item
                for item in store.outbound_deliveries(
                    business_key=business_key,
                    not_after=started_at,
                )
            }.get(channel)
            results.append(
                ChannelDelivery(
                    channel,
                    attempted=False,
                    accepted=bool(existing and existing["status"] == "sent"),
                )
            )
            continue

        error_code: str | None = None
        try:
            if channel == "telegram":
                telegram_delivery = telegram_sender or _telegram_sender(
                    kind, payload, settings
                )
                await telegram_delivery()
            else:
                whatsapp_delivery = whatsapp_sender or _whatsapp_sender(
                    kind, settings
                )
                delivery = await asyncio.to_thread(whatsapp_delivery)
                if not delivery.success:
                    error_code = delivery.category
                    raise _ChannelRejected
        except Exception as exc:  # noqa: BLE001 - secret-safe channel boundary
            if error_code is None:
                error_code = _error_code(exc)
            finished_at = _aware(runtime_clock())
            store.mark_outbound_delivery_failed(
                business_key,
                channel,
                fingerprint,
                claim,
                error_code,
                now=finished_at,
            )
            if record_delivery_state:
                store.record_delivery_state(
                    channel,
                    config_fingerprint=fingerprint,
                    configured=True,
                    mode="active",
                    attempted_at=finished_at,
                    success=False,
                    error_code=error_code,
                    now=finished_at,
                )
            results.append(
                ChannelDelivery(channel, True, False, error_code)
            )
        else:
            finished_at = _aware(runtime_clock())
            store.mark_outbound_delivery_sent(
                business_key,
                channel,
                fingerprint,
                claim,
                now=finished_at,
            )
            if record_delivery_state:
                store.record_delivery_state(
                    channel,
                    config_fingerprint=fingerprint,
                    configured=True,
                    mode="active",
                    attempted_at=finished_at,
                    success=True,
                    now=finished_at,
                )
            results.append(ChannelDelivery(channel, True, True))

    return MobileDeliveryReport(business_key, required, tuple(results))


def _telegram_sender(
    kind: MobileMessageKind,
    payload: Mapping[str, Any] | str,
    settings: Settings,
) -> TelegramSender:
    if kind == "signal" and isinstance(payload, Mapping):
        return lambda: send_signal(payload, settings=settings)
    if kind == "incident" and isinstance(payload, Mapping):
        return lambda: send_incident(payload, settings=settings)
    if kind == "news" and isinstance(payload, Mapping):
        return lambda: send_news_alert(payload, settings=settings)
    if kind in {"summary", "trust"} and isinstance(payload, str):
        return lambda: send_message(payload, settings=settings)
    raise ValueError("mobile payload does not match message kind")


def _whatsapp_sender(
    kind: MobileMessageKind,
    settings: Settings,
) -> WhatsAppSender:
    template = {
        "signal": settings.whatsapp_signal_template_name,
        "incident": settings.whatsapp_incident_template_name,
        # News has its own optional approved template.  Installations that do
        # not define it still fan out through the required incident template,
        # rather than silently turning WhatsApp into a best-effort channel.
        "news": (
            settings.whatsapp_news_template_name
            or settings.whatsapp_incident_template_name
        ),
        # Daily summaries use the always-required trust template.  WhatsApp
        # templates are deliberately static here: arbitrary text is not legal
        # outside Meta's 24-hour customer-service window.
        "summary": settings.whatsapp_trust_template_name,
        "trust": settings.whatsapp_trust_template_name,
    }[kind]
    if template is None:
        return lambda: WhatsAppDeliveryResult(
            False,
            "invalid_configuration",
        )
    notifier = WhatsAppNotifier(settings)
    return lambda: notifier.send_template(template_name=template)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("delivery time must be timezone-aware")
    return value.astimezone(UTC)


class _ChannelRejected(RuntimeError):
    pass


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "connection"
    raw = type(exc).__name__.lower()
    normalized = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in raw
    ).strip("_")
    return (normalized or "unknown")[:64]


__all__ = [
    "ChannelDelivery",
    "MobileDeliveryReport",
    "configured_mobile_channels",
    "deliver_mobile",
]
