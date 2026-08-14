"""Secret-safe WhatsApp Cloud API transport.

Meta distinguishes pre-approved template messages from free-form service
messages.  Service messages are permitted only while the recipient's 24-hour
customer service window is open, so :meth:`WhatsAppNotifier.send_text` requires
an explicit, per-call confirmation of that fact.

A successful Messages API response means Meta accepted the request.  It is not
proof of delivery; delivered/read outcomes arrive through WhatsApp webhooks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import re
from typing import Any, Literal, Protocol, TypeAlias

import requests

from config import Settings

GRAPH_API_ORIGIN = "https://graph.facebook.com"
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 65_536
MAX_TEXT_LENGTH = 4_096

_API_VERSION = re.compile(r"^v[1-9][0-9]?\.0$")
_TEMPLATE_NAME = re.compile(r"^[a-z0-9_]{1,512}$")
_LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}(?:_[A-Z]{2})?$")
_MESSAGE_ID = re.compile(r"^wamid\.[A-Za-z0-9._:-]{1,512}$")

WhatsAppResultCategory: TypeAlias = Literal[
    "accepted",
    "disabled",
    "invalid_configuration",
    "invalid_request",
    "window_not_confirmed",
    "redirect",
    "rate_limited",
    "http_client",
    "http_server",
    "timeout",
    "tls",
    "connection",
    "protocol_error",
]


class _ResponseLike(Protocol):
    status_code: int

    def iter_content(self, chunk_size: int = ...) -> Any: ...

    def close(self) -> None: ...


WhatsAppRequest = Callable[..., _ResponseLike]


class WhatsAppPayloadError(ValueError):
    """A deliberately context-free payload validation error."""


@dataclass(frozen=True, slots=True)
class WhatsAppDeliveryResult:
    """Bounded result safe for logs and persistence.

    ``success``/``accepted`` only mean the Cloud API accepted the request.
    ``message_id`` is excluded from ``repr`` and is validated before exposure.
    Delivery must be established independently from a webhook status event.
    """

    success: bool
    category: WhatsAppResultCategory
    status_code: int | None = None
    retryable: bool = False
    message_id: str | None = field(default=None, repr=False)

    @property
    def accepted(self) -> bool:
        return self.success


@dataclass(frozen=True, slots=True, repr=False)
class _Transport:
    endpoint: str
    token: str
    recipient: str
    timeout_seconds: float


def render_template_payload(
    *,
    to: str,
    template_name: str,
    language_code: str,
    components: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Purely build and validate a Meta template-message payload.

    Business copy belongs in the caller's pure renderer.  This function only
    applies the channel schema and creates a detached JSON-compatible value.
    """

    recipient = _normalize_recipient(to)
    if not isinstance(template_name, str) or not _TEMPLATE_NAME.fullmatch(
        template_name
    ):
        raise WhatsAppPayloadError("invalid WhatsApp template payload")
    if not isinstance(language_code, str) or not _LANGUAGE_CODE.fullmatch(
        language_code
    ):
        raise WhatsAppPayloadError("invalid WhatsApp template payload")
    if isinstance(components, (str, bytes)) or not isinstance(components, Sequence):
        raise WhatsAppPayloadError("invalid WhatsApp template payload")
    if not all(isinstance(component, Mapping) for component in components):
        raise WhatsAppPayloadError("invalid WhatsApp template payload")

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        },
    }
    if components:
        payload["template"]["components"] = list(components)
    return _detached_bounded_payload(payload, "invalid WhatsApp template payload")


def render_text_payload(
    *,
    to: str,
    text: str,
    preview_url: bool = False,
) -> dict[str, Any]:
    """Purely build a free-form service message payload."""

    recipient = _normalize_recipient(to)
    if (
        not isinstance(text, str)
        or not 1 <= len(text) <= MAX_TEXT_LENGTH
        or "\x00" in text
        or not isinstance(preview_url, bool)
    ):
        raise WhatsAppPayloadError("invalid WhatsApp text payload")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": preview_url, "body": text},
    }
    return _detached_bounded_payload(payload, "invalid WhatsApp text payload")


class WhatsAppNotifier:
    """Send through Meta's fixed-origin WhatsApp Cloud API endpoint."""

    __slots__ = ("_request", "_settings")

    def __init__(
        self,
        settings: Settings,
        *,
        request: WhatsAppRequest = requests.post,
    ) -> None:
        self._settings = settings
        self._request = request

    def __repr__(self) -> str:
        return "<WhatsAppNotifier>"

    def send_template(
        self,
        *,
        template_name: str,
        to: str | None = None,
        language_code: str | None = None,
        components: Sequence[Mapping[str, Any]] = (),
    ) -> WhatsAppDeliveryResult:
        """Send a pre-approved template, including outside the 24-hour window."""

        prepared = self._preflight(to)
        if isinstance(prepared, WhatsAppDeliveryResult):
            return prepared
        try:
            payload = render_template_payload(
                to=prepared.recipient,
                template_name=template_name,
                language_code=(
                    language_code
                    if language_code is not None
                    else self._settings.whatsapp_template_language_code
                ),
                components=components,
            )
        except WhatsAppPayloadError:
            return WhatsAppDeliveryResult(False, "invalid_request")
        return self._post(prepared, payload)

    def send_text(
        self,
        *,
        text: str,
        customer_service_window_open: bool,
        to: str | None = None,
        preview_url: bool = False,
    ) -> WhatsAppDeliveryResult:
        """Send text only after the caller confirms an open 24-hour window."""

        prepared = self._preflight(to)
        if isinstance(prepared, WhatsAppDeliveryResult):
            return prepared
        if customer_service_window_open is not True:
            return WhatsAppDeliveryResult(False, "window_not_confirmed")
        try:
            payload = render_text_payload(
                to=prepared.recipient,
                text=text,
                preview_url=preview_url,
            )
        except WhatsAppPayloadError:
            return WhatsAppDeliveryResult(False, "invalid_request")
        return self._post(prepared, payload)

    def _preflight(self, to: str | None) -> _Transport | WhatsAppDeliveryResult:
        settings = self._settings
        if not settings.whatsapp_enabled:
            return WhatsAppDeliveryResult(False, "disabled")

        token = (
            settings.whatsapp_access_token.get_secret_value()
            if settings.whatsapp_access_token is not None
            else ""
        )
        phone_number_id = settings.whatsapp_phone_number_id or ""
        api_version = settings.whatsapp_graph_api_version
        required_templates = (
            settings.whatsapp_signal_template_name,
            settings.whatsapp_incident_template_name,
            settings.whatsapp_trust_template_name,
        )
        if (
            not token
            or token != token.strip()
            or any(character.isspace() for character in token)
            or len(token) > 4_096
            or not phone_number_id.isdigit()
            or not 1 <= len(phone_number_id) <= 32
            or not _API_VERSION.fullmatch(api_version)
            or any(
                not isinstance(name, str) or not _TEMPLATE_NAME.fullmatch(name)
                for name in required_templates
            )
            or (
                settings.whatsapp_news_template_name is not None
                and not _TEMPLATE_NAME.fullmatch(settings.whatsapp_news_template_name)
            )
        ):
            return WhatsAppDeliveryResult(False, "invalid_configuration")
        recipient = to if to is not None else settings.whatsapp_default_to
        try:
            normalized_recipient = _normalize_recipient(recipient)
        except WhatsAppPayloadError:
            return WhatsAppDeliveryResult(False, "invalid_request")
        return _Transport(
            endpoint=f"{GRAPH_API_ORIGIN}/{api_version}/{phone_number_id}/messages",
            token=token,
            recipient=normalized_recipient,
            timeout_seconds=settings.whatsapp_timeout_seconds,
        )

    def _post(
        self,
        transport: _Transport,
        payload: Mapping[str, Any],
    ) -> WhatsAppDeliveryResult:
        try:
            response = self._request(
                transport.endpoint,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {transport.token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(transport.timeout_seconds, transport.timeout_seconds),
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout:
            return WhatsAppDeliveryResult(False, "timeout", retryable=True)
        except requests.exceptions.SSLError:
            return WhatsAppDeliveryResult(False, "tls")
        except requests.ConnectionError:
            return WhatsAppDeliveryResult(False, "connection", retryable=True)
        except (requests.RequestException, Exception):
            # Exception strings frequently contain the URL, headers, or body.
            return WhatsAppDeliveryResult(False, "connection", retryable=True)

        try:
            status_code = response.status_code
            if (
                not isinstance(status_code, int)
                or isinstance(status_code, bool)
                or not 100 <= status_code <= 599
            ):
                return WhatsAppDeliveryResult(False, "protocol_error")
            if 300 <= status_code < 400:
                return WhatsAppDeliveryResult(False, "redirect", status_code)
            if status_code == 429:
                return WhatsAppDeliveryResult(
                    False, "rate_limited", status_code, retryable=True
                )
            if 400 <= status_code < 500:
                return WhatsAppDeliveryResult(False, "http_client", status_code)
            if 500 <= status_code < 600:
                return WhatsAppDeliveryResult(
                    False, "http_server", status_code, retryable=True
                )
            if not 200 <= status_code < 300:
                return WhatsAppDeliveryResult(False, "protocol_error", status_code)

            decoded = _read_bounded_json(response)
            message_id = _accepted_message_id(decoded)
            if message_id is None or transport.token in message_id:
                return WhatsAppDeliveryResult(False, "protocol_error", status_code)
            return WhatsAppDeliveryResult(
                True,
                "accepted",
                status_code,
                message_id=message_id,
            )
        except requests.Timeout:
            return WhatsAppDeliveryResult(False, "timeout", retryable=True)
        except requests.exceptions.SSLError:
            return WhatsAppDeliveryResult(False, "tls")
        except requests.ConnectionError:
            return WhatsAppDeliveryResult(False, "connection", retryable=True)
        except Exception:
            # Parsing/decompression/adapter failures are intentionally opaque.
            return WhatsAppDeliveryResult(False, "protocol_error")
        finally:
            try:
                response.close()
            except Exception:
                pass


def send_whatsapp_template(
    settings: Settings,
    *,
    template_name: str,
    to: str | None = None,
    language_code: str | None = None,
    components: Sequence[Mapping[str, Any]] = (),
    request: WhatsAppRequest = requests.post,
) -> WhatsAppDeliveryResult:
    """Functional facade for :meth:`WhatsAppNotifier.send_template`."""

    return WhatsAppNotifier(settings, request=request).send_template(
        template_name=template_name,
        to=to,
        language_code=language_code,
        components=components,
    )


def send_whatsapp_text(
    settings: Settings,
    *,
    text: str,
    customer_service_window_open: bool,
    to: str | None = None,
    preview_url: bool = False,
    request: WhatsAppRequest = requests.post,
) -> WhatsAppDeliveryResult:
    """Functional facade for :meth:`WhatsAppNotifier.send_text`."""

    return WhatsAppNotifier(settings, request=request).send_text(
        text=text,
        customer_service_window_open=customer_service_window_open,
        to=to,
        preview_url=preview_url,
    )


def _normalize_recipient(value: Any) -> str:
    if not isinstance(value, str):
        raise WhatsAppPayloadError("invalid WhatsApp recipient")
    digits = value[1:] if value.startswith("+") else value
    if (
        not digits.isdigit()
        or not 7 <= len(digits) <= 15
        or digits.startswith("0")
    ):
        raise WhatsAppPayloadError("invalid WhatsApp recipient")
    return digits


def _detached_bounded_payload(payload: Mapping[str, Any], message: str) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise WhatsAppPayloadError(message)
        decoded = json.loads(encoded)
    except WhatsAppPayloadError:
        raise
    except Exception:
        raise WhatsAppPayloadError(message) from None
    if not isinstance(decoded, dict):
        raise WhatsAppPayloadError(message)
    return decoded


def _read_bounded_json(response: _ResponseLike) -> Mapping[str, Any] | None:
    body = bytearray()
    for chunk in response.iter_content(chunk_size=4_096):
        if not isinstance(chunk, (bytes, bytearray)):
            return None
        if not chunk:
            continue
        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
            return None
        body.extend(chunk)
    try:
        decoded = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _accepted_message_id(response: Mapping[str, Any] | None) -> str | None:
    if response is None:
        return None
    messages = response.get("messages")
    if (
        not isinstance(messages, list)
        or not messages
        or not isinstance(messages[0], Mapping)
    ):
        return None
    message_id = messages[0].get("id")
    if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
        return None
    return message_id


__all__ = [
    "WhatsAppDeliveryResult",
    "WhatsAppNotifier",
    "WhatsAppPayloadError",
    "WhatsAppResultCategory",
    "render_template_payload",
    "render_text_payload",
    "send_whatsapp_template",
    "send_whatsapp_text",
]
