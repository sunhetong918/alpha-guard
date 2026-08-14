"""Secret-safe HTTP(S) dead-man heartbeat transport and eligibility policy."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests
from pydantic import SecretStr

from config import MOBILE_TRUST_PROOF_MAX_AGE_SECONDS, Settings


class _ResponseLike(Protocol):
    status_code: int

    def close(self) -> None: ...


HeartbeatRequest = Callable[..., _ResponseLike]
MOBILE_TRUST_PROOF_MAX_AGE = timedelta(
    seconds=MOBILE_TRUST_PROOF_MAX_AGE_SECONDS
)


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    """Bounded transport result that can safely cross logging/storage boundaries."""

    success: bool
    error_code: str | None = None


def _heartbeat_url(settings: Settings) -> str | None:
    if not settings.heartbeat_enabled or settings.heartbeat_url is None:
        return None
    # The URL path is normally the dead-man secret.  Unwrap it only inside
    # this transport boundary and never include it in a result or exception.
    candidate = settings.heartbeat_url.get_secret_value()
    try:
        parsed = urlsplit(candidate)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(character.isspace() for character in candidate)
        ):
            return None
        # Accessing port performs urllib's range/format validation.
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    return candidate


def _config_digest(channel: str, components: tuple[str | None, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(b"alpha-guard:delivery-config:v1\x00")
    digest.update(channel.encode("ascii"))
    for component in components:
        digest.update(b"\x00none" if component is None else b"\x00text")
        if component is not None:
            encoded = component.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def delivery_config_fingerprints(settings: Settings) -> dict[str, str]:
    """Hash delivery generations without returning configured secrets."""

    def secret_value(value: SecretStr | str | None) -> str | None:
        return value.get_secret_value() if isinstance(value, SecretStr) else value

    telegram_token = secret_value(settings.telegram_bot_token)
    whatsapp_token = secret_value(settings.whatsapp_access_token)
    heartbeat_url = secret_value(settings.heartbeat_url)
    return {
        "telegram": _config_digest(
            "telegram",
            (
                "enabled" if settings.notifications_enabled else "disabled",
                telegram_token,
                settings.telegram_chat_id,
            ),
        ),
        "whatsapp": _config_digest(
            "whatsapp",
            (
                "enabled" if settings.whatsapp_enabled else "disabled",
                whatsapp_token,
                settings.whatsapp_phone_number_id,
                settings.whatsapp_default_to,
                settings.whatsapp_graph_api_version,
                settings.whatsapp_template_language_code,
                settings.whatsapp_signal_template_name,
                settings.whatsapp_incident_template_name,
                settings.whatsapp_news_template_name,
                settings.whatsapp_trust_template_name,
            ),
        ),
        "heartbeat": _config_digest(
            "heartbeat",
            (
                "enabled" if settings.heartbeat_enabled else "disabled",
                heartbeat_url,
            ),
        ),
    }


def ping_heartbeat(
    settings: Settings,
    *,
    request: HeartbeatRequest = requests.post,
) -> HeartbeatResult:
    """POST an empty HTTP(S) heartbeat without redirects or response-body reads."""

    url = _heartbeat_url(settings)
    if url is None:
        return HeartbeatResult(False, "invalid_url")
    try:
        response = request(
            url,
            data=b"",
            timeout=settings.heartbeat_timeout_seconds,
            allow_redirects=False,
            stream=True,
        )
    except requests.Timeout:
        return HeartbeatResult(False, "timeout")
    except requests.exceptions.SSLError:
        return HeartbeatResult(False, "tls")
    except requests.ConnectionError:
        return HeartbeatResult(False, "connection")
    except requests.exceptions.InvalidURL:
        return HeartbeatResult(False, "invalid_url")
    except (requests.RequestException, Exception):
        # Do not propagate exception text: requests errors often embed the URL.
        return HeartbeatResult(False, "connection")
    try:
        status_code = response.status_code
    finally:
        try:
            response.close()
        except Exception:
            # The bounded HTTP result is already known; close errors can
            # include the secret URL and must never escape this boundary.
            pass
    if 200 <= status_code < 300:
        return HeartbeatResult(True)
    return HeartbeatResult(False, "http_status")


def _complete_coverage(projection: Mapping[str, Any], enabled: int) -> bool:
    return (
        projection.get("known") is True
        and projection.get("enabled") == enabled
        and projection.get("usable") == enabled
        and projection.get("ratio") == 1.0
        and projection.get("affected") == []
    )


def heartbeat_eligible(
    receipt: Mapping[str, Any],
    *,
    at: datetime | None = None,
) -> bool:
    """Return whether current local evidence permits an external heartbeat.

    Deliberately ignore top-level readiness and the watcher's own prior result:
    those include watcher_unproven/watcher_unavailable and would otherwise make
    a first ping or recovery retry impossible.
    """

    try:
        if receipt.get("delivery_mode") != "ACTIVE":
            return False
        watchdog = receipt["watchdog"]
        if (
            not isinstance(watchdog, Mapping)
            or watchdog.get("active") is not False
            or watchdog.get("state") not in {None, "RECOVERED"}
        ):
            return False
        silence = receipt["silence"]
        enabled = silence["enabled"]
        if (
            isinstance(enabled, bool)
            or not isinstance(enabled, int)
            or enabled <= 0
            or silence.get("state") != "HEALTHY"
            or silence.get("usable") != enabled
            or not _complete_coverage(silence["fresh_data"], enabled)
            or not _complete_coverage(silence["trusted_decision"], enabled)
        ):
            return False
        reason_codes = receipt.get("reason_codes", [])
        if any(
            reason == "deadline_evidence_unresolved"
            or "corrupt" in reason
            or reason.startswith("integrity_")
            for reason in reason_codes
        ):
            return False
        schedule = receipt["schedule"]["markets"]
        allowed_deadline_states = {
            "completed",
            "within_grace",
            "outside_activation",
            "bad",
        }
        if not schedule or any(
            item.get("deadline_state") not in allowed_deadline_states
            for item in schedule
        ):
            return False
        delivery = receipt["delivery"]
        mobile_channels = [
            item
            for name in ("telegram", "whatsapp")
            if isinstance((item := delivery.get(name)), Mapping)
            and item.get("mode") == "ACTIVE"
            and item.get("configured") is True
        ]
        receipt_time = receipt.get("generated_at")
        if at is None and isinstance(receipt_time, str):
            try:
                at = datetime.fromisoformat(receipt_time)
            except ValueError:
                return False
        evaluation_time = at or datetime.now(UTC)
        if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
            return False
        evaluated_at = evaluation_time.astimezone(UTC)
        if not mobile_channels:
            return False
        for item in mobile_channels:
            raw_success_at = item.get("last_success_at")
            if (
                not isinstance(raw_success_at, str)
                or item.get("success") is not True
                or item.get("error_code") is not None
            ):
                return False
            try:
                success_at = datetime.fromisoformat(raw_success_at)
            except ValueError:
                return False
            if success_at.tzinfo is None or success_at.utcoffset() is None:
                return False
            proof_age = evaluated_at - success_at.astimezone(UTC)
            if proof_age < timedelta(0) or proof_age > MOBILE_TRUST_PROOF_MAX_AGE:
                return False
        watcher = delivery["external_watcher"]
        return (
            watcher.get("mode") == "ACTIVE"
            and watcher.get("configured") is True
        )
    except (KeyError, TypeError, AttributeError):
        return False


__all__ = [
    "HeartbeatResult",
    "MOBILE_TRUST_PROOF_MAX_AGE",
    "delivery_config_fingerprints",
    "heartbeat_eligible",
    "ping_heartbeat",
]
