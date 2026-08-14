from __future__ import annotations

from dataclasses import asdict
import json

import pytest
import requests
from pydantic import ValidationError

from config import Settings
from notifier.whatsapp import (
    MAX_RESPONSE_BYTES,
    WhatsAppNotifier,
    WhatsAppPayloadError,
    render_template_payload,
    render_text_payload,
)


TOKEN = "EAAB-private-access-token-123456"
PHONE_NUMBER_ID = "106540352242922"
RECIPIENT = "+16505551234"
MESSAGE_ID = "wamid.HBgLMTY0NjcwNDM1OTUVAgARGBI4MjZGRA"


def _settings(**updates) -> Settings:
    values = {
        "whatsapp_enabled": True,
        "whatsapp_access_token": TOKEN,
        "whatsapp_phone_number_id": PHONE_NUMBER_ID,
        "whatsapp_default_to": RECIPIENT,
        "whatsapp_signal_template_name": "alpha_guard_signal",
        "whatsapp_incident_template_name": "alpha_guard_incident",
        "whatsapp_trust_template_name": "alpha_guard_trust",
    }
    values.update(updates)
    return Settings(_env_file=None, **values)


class _Response:
    def __init__(self, status_code: int, body: bytes = b"") -> None:
        self.status_code = status_code
        self.body = body
        self.closed = False
        self.body_read = False

    @property
    def text(self) -> str:  # pragma: no cover - forbidden transport API
        raise AssertionError("raw response text must never be read")

    def iter_content(self, chunk_size: int = 4096):
        self.body_read = True
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


def _accepted_response(status_code: int = 200) -> _Response:
    return _Response(
        status_code,
        json.dumps({"messages": [{"id": MESSAGE_ID}]}).encode(),
    )


def test_whatsapp_is_independently_disabled_by_default() -> None:
    settings = Settings(_env_file=None)
    called = False

    def request(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("disabled channel must not use the network")

    result = WhatsAppNotifier(settings, request=request).send_template(
        template_name="alpha_guard_signal"
    )

    assert settings.whatsapp_enabled is False
    assert result.category == "disabled"
    assert result.success is False
    assert called is False


def test_enabled_configuration_requires_credentials_and_operational_templates() -> None:
    for missing in (
        "whatsapp_access_token",
        "whatsapp_phone_number_id",
        "whatsapp_default_to",
        "whatsapp_signal_template_name",
        "whatsapp_incident_template_name",
        "whatsapp_trust_template_name",
    ):
        values = {
            "whatsapp_enabled": True,
            "whatsapp_access_token": TOKEN,
            "whatsapp_phone_number_id": PHONE_NUMBER_ID,
            "whatsapp_default_to": RECIPIENT,
            "whatsapp_signal_template_name": "alpha_guard_signal",
            "whatsapp_incident_template_name": "alpha_guard_incident",
            "whatsapp_trust_template_name": "alpha_guard_trust",
        }
        values[missing] = None
        with pytest.raises(ValidationError, match=missing.upper()):
            Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("whatsapp_phone_number_id", "10/secret"),
        ("whatsapp_default_to", "javascript:alert(1)"),
        ("whatsapp_graph_api_version", "latest"),
        ("whatsapp_template_language_code", "../../en_US"),
        ("whatsapp_signal_template_name", "Alpha Guard Signal"),
        ("whatsapp_incident_template_name", "incident/path"),
        ("whatsapp_trust_template_name", "信任模板"),
        ("whatsapp_timeout_seconds", 31),
    ],
)
def test_enabled_configuration_rejects_unsafe_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_configuration_validation_and_repr_never_reveal_token() -> None:
    secret = "ultra-private-token with-space"
    with pytest.raises(ValidationError) as exc_info:
        _settings(whatsapp_access_token=secret)

    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)
    assert TOKEN not in repr(_settings())
    assert TOKEN not in repr(WhatsAppNotifier(_settings()))


def test_pure_template_renderer_matches_meta_schema_and_detaches_components() -> None:
    components = [
        {"type": "body", "parameters": [{"type": "text", "text": "AAPL"}]}
    ]
    payload = render_template_payload(
        to=RECIPIENT,
        template_name="alpha_guard_signal",
        language_code="zh_CN",
        components=components,
    )
    components[0]["type"] = "mutated"

    assert payload == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "16505551234",
        "type": "template",
        "template": {
            "name": "alpha_guard_signal",
            "language": {"code": "zh_CN"},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": "AAPL"}],
                }
            ],
        },
    }


def test_pure_text_renderer_builds_only_service_message_schema() -> None:
    assert render_text_payload(to=RECIPIENT, text="Review AAPL", preview_url=True) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "16505551234",
        "type": "text",
        "text": {"preview_url": True, "body": "Review AAPL"},
    }


def test_payload_errors_are_fixed_and_do_not_reflect_untrusted_values() -> None:
    secret = "private-component-value"
    with pytest.raises(WhatsAppPayloadError) as exc_info:
        render_template_payload(
            to=RECIPIENT,
            template_name="bad/name",
            language_code="en_US",
            components=[{"secret": secret}],
        )

    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)


@pytest.mark.parametrize("status_code", [200, 201, 299])
def test_template_delivery_accepts_valid_2xx_and_uses_fixed_safe_request(
    status_code: int,
) -> None:
    response = _accepted_response(status_code)
    calls: list[tuple[str, dict]] = []

    def request(url: str, **kwargs):
        calls.append((url, kwargs))
        return response

    result = WhatsAppNotifier(_settings(), request=request).send_template(
        template_name="alpha_guard_signal",
        components=[
            {"type": "body", "parameters": [{"type": "text", "text": "AAPL"}]}
        ],
    )

    assert result.success is True
    assert result.accepted is True
    assert result.category == "accepted"
    assert result.status_code == status_code
    assert result.message_id == MESSAGE_ID
    assert response.closed is True
    assert calls[0][0] == (
        "https://graph.facebook.com/v26.0/106540352242922/messages"
    )
    kwargs = calls[0][1]
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert kwargs["timeout"] == (10.0, 10.0)
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert TOKEN not in calls[0][0]
    assert TOKEN not in repr(result)
    assert TOKEN not in json.dumps(asdict(result))


def test_text_requires_explicit_open_customer_service_window() -> None:
    calls = 0

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _accepted_response()

    notifier = WhatsAppNotifier(_settings(), request=request)
    denied = notifier.send_text(
        text="Review AAPL", customer_service_window_open=False
    )
    accepted = notifier.send_text(
        text="Review AAPL", customer_service_window_open=True
    )

    assert denied.category == "window_not_confirmed"
    assert accepted.category == "accepted"
    assert calls == 1


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_4xx_is_low_cardinality_and_does_not_read_malicious_body(
    status_code: int,
) -> None:
    response = _Response(status_code, f"{TOKEN} https://evil.example".encode())
    result = WhatsAppNotifier(
        _settings(), request=lambda *_args, **_kwargs: response
    ).send_template(template_name="alpha_guard_signal")

    assert result.category == "http_client"
    assert result.retryable is False
    assert result.status_code == status_code
    assert response.body_read is False
    assert response.closed is True
    assert TOKEN not in repr(result)
    assert "evil.example" not in repr(result)


def test_429_and_5xx_are_bounded_retryable_results() -> None:
    for status, category in ((429, "rate_limited"), (500, "http_server")):
        response = _Response(status, TOKEN.encode())
        result = WhatsAppNotifier(
            _settings(), request=lambda *_args, **_kwargs: response
        ).send_template(template_name="alpha_guard_signal")

        assert result.category == category
        assert result.retryable is True
        assert response.body_read is False
        assert TOKEN not in repr(result)


def test_redirect_is_never_followed_or_reflected() -> None:
    response = _Response(302, b"https://evil.example/" + TOKEN.encode())
    result = WhatsAppNotifier(
        _settings(), request=lambda *_args, **_kwargs: response
    ).send_template(template_name="alpha_guard_signal")

    assert result.category == "redirect"
    assert response.body_read is False
    assert TOKEN not in repr(result)


@pytest.mark.parametrize(
    ("exception", "category", "retryable"),
    [
        (requests.Timeout(f"https://evil.example/{TOKEN}"), "timeout", True),
        (requests.exceptions.SSLError(TOKEN), "tls", False),
        (requests.ConnectionError(TOKEN), "connection", True),
    ],
)
def test_transport_exceptions_are_mapped_without_secret_text(
    exception: Exception,
    category: str,
    retryable: bool,
) -> None:
    def request(*_args, **_kwargs):
        raise exception

    result = WhatsAppNotifier(_settings(), request=request).send_template(
        template_name="alpha_guard_signal"
    )

    assert result.category == category
    assert result.retryable is retryable
    assert TOKEN not in repr(result)
    assert "evil.example" not in repr(result)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json private-token https://evil.example",
        json.dumps({"messages": [{"id": TOKEN}]}).encode(),
        json.dumps({"messages": [{"id": f"wamid.{TOKEN}"}]}).encode(),
        b"x" * (MAX_RESPONSE_BYTES + 1),
    ],
)
def test_malicious_or_oversized_2xx_body_never_crosses_result_boundary(
    body: bytes,
) -> None:
    response = _Response(200, body)
    result = WhatsAppNotifier(
        _settings(), request=lambda *_args, **_kwargs: response
    ).send_template(template_name="alpha_guard_signal")

    assert result.category == "protocol_error"
    assert result.message_id is None
    assert response.closed is True
    assert TOKEN not in repr(result)
    assert "evil.example" not in repr(result)

