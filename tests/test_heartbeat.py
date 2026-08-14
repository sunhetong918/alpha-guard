from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
import requests
from pydantic import SecretStr

from config import Settings
from notifier.heartbeat import (
    delivery_config_fingerprints,
    heartbeat_eligible,
    ping_heartbeat,
)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.closed = False

    @property
    def text(self) -> str:  # pragma: no cover - must never be touched
        raise AssertionError("heartbeat response body must not be read")

    def close(self) -> None:
        self.closed = True


def _settings(url: str = "https://hc.example/ping/private-token") -> Settings:
    return Settings(
        heartbeat_enabled=True,
        heartbeat_url=url,
        heartbeat_timeout_seconds=2.5,
    )


def _healthy_receipt() -> dict:
    return {
        "delivery_mode": "ACTIVE",
        "state": "HEALTHY",
        "overall_color": "GREEN",
        "reason_codes": [],
        "schedule": {
            "markets": [
                {
                    "market": "US",
                    "deadline_state": "completed",
                }
            ]
        },
        "silence": {
            "state": "HEALTHY",
            "enabled": 1,
            "usable": 1,
            "fresh_data": {
                "known": True,
                "enabled": 1,
                "usable": 1,
                "ratio": 1.0,
                "affected": [],
            },
            "trusted_decision": {
                "known": True,
                "enabled": 1,
                "usable": 1,
                "ratio": 1.0,
                "affected": [],
            },
        },
        "delivery": {
            "telegram": {
                "configured": True,
                "mode": "ACTIVE",
                "last_attempt_at": "2026-08-10T13:26:00+00:00",
                "last_success_at": "2026-08-10T13:26:00+00:00",
                "success": True,
                "error_code": None,
            },
            "whatsapp": {
                "configured": False,
                "mode": "PREVIEW",
                "last_attempt_at": None,
                "last_success_at": None,
                "success": None,
                "error_code": None,
            },
            "external_watcher": {
                "configured": True,
                "mode": "ACTIVE",
                "last_attempt_at": None,
                "last_success_at": None,
                "success": None,
                "error_code": None,
            },
        },
        "watchdog": {
            "state": "RECOVERED",
            "generation": 1,
            "active": False,
            "affected": ["AAPL"],
            "markets": ["US"],
            "window_count": 1,
            "first_seen_at": "2026-08-10T13:20:00+00:00",
            "resolved_at": "2026-08-10T13:25:00+00:00",
            "delivery_status": "sent",
        },
    }


def test_heartbeat_transport_posts_https_without_redirect_or_secret_output() -> None:
    secret = "https://hc.example/ping/private-token"
    calls: list[tuple[str, dict]] = []

    response = _Response(204)

    def request(url: str, **kwargs):
        calls.append((url, kwargs))
        return response

    result = ping_heartbeat(_settings(secret), request=request)

    assert result.success is True
    assert result.error_code is None
    assert calls == [
        (
            secret,
            {
                "data": b"",
                "timeout": 2.5,
                "allow_redirects": False,
                "stream": True,
            },
        )
    ]
    assert response.closed is True
    assert secret not in repr(result)


def test_heartbeat_transport_allows_explicit_self_hosted_http_endpoint() -> None:
    endpoint = "http://healthchecks.internal/ping/private-token"
    calls: list[str] = []

    result = ping_heartbeat(
        _settings(endpoint),
        request=lambda url, **_kwargs: (calls.append(url) or _Response(200)),
    )

    assert result.success is True
    assert calls == [endpoint]
    assert endpoint not in repr(result)


def test_delivery_configuration_fingerprints_are_secret_free_and_domain_separated() -> None:
    original = Settings(
        notifications_enabled=True,
        telegram_bot_token="private-token-a",
        telegram_chat_id="chat-a",
        heartbeat_enabled=True,
        heartbeat_url="https://hc.example/private-url-a",
    )
    fingerprints = delivery_config_fingerprints(original)

    assert set(fingerprints) == {"telegram", "whatsapp", "heartbeat"}
    assert all(
        len(value) == 64
        and set(value) <= set("0123456789abcdef")
        for value in fingerprints.values()
    )
    assert fingerprints["telegram"] != fingerprints["heartbeat"]
    assert fingerprints["whatsapp"] not in {
        fingerprints["telegram"],
        fingerprints["heartbeat"],
    }
    assert "private" not in repr(fingerprints)
    assert delivery_config_fingerprints(
        original.model_copy(update={"telegram_chat_id": "chat-b"})
    )["telegram"] != fingerprints["telegram"]
    assert delivery_config_fingerprints(
        original.model_copy(update={"telegram_bot_token": "private-token-b"})
    )["telegram"] != fingerprints["telegram"]
    assert delivery_config_fingerprints(
        original.model_copy(
            update={"heartbeat_url": "https://hc.example/private-url-b"}
        )
    )["heartbeat"] != fingerprints["heartbeat"]
    assert delivery_config_fingerprints(
        original.model_copy(update={"notifications_enabled": False})
    )["telegram"] != fingerprints["telegram"]
    assert delivery_config_fingerprints(
        original.model_copy(update={"heartbeat_enabled": False})
    )["heartbeat"] != fingerprints["heartbeat"]


@pytest.mark.parametrize("status_code", [199, 300, 302, 500])
def test_heartbeat_transport_accepts_only_2xx(status_code: int) -> None:
    result = ping_heartbeat(
        _settings(),
        request=lambda *_args, **_kwargs: _Response(status_code),
    )

    assert result.success is False
    assert result.error_code == "http_status"


@pytest.mark.parametrize(
    ("exception", "expected_code"),
    [
        (requests.Timeout("private-token"), "timeout"),
        (requests.exceptions.SSLError("private-token"), "tls"),
        (requests.ConnectionError("private-token"), "connection"),
    ],
)
def test_heartbeat_transport_maps_exceptions_without_text(
    exception: Exception,
    expected_code: str,
) -> None:
    def request(*_args, **_kwargs):
        raise exception

    result = ping_heartbeat(_settings(), request=request)

    assert result.success is False
    assert result.error_code == expected_code
    assert "private-token" not in repr(result)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@hc.example/ping/token",
        "https://hc.example/ping/token#private-fragment",
        "not-a-url-private-token",
    ],
)
def test_heartbeat_transport_rejects_unsafe_url_without_network(url: str) -> None:
    called = False

    def request(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid heartbeat URL must not be requested")

    settings = _settings().model_copy(update={"heartbeat_url": SecretStr(url)})
    result = ping_heartbeat(settings, request=request)

    assert result.success is False
    assert result.error_code == "invalid_url"
    assert called is False
    assert url not in repr(result)


@pytest.mark.parametrize(
    "watcher_update",
    [
        {},
        {
            "last_attempt_at": "2026-08-10T13:27:00+00:00",
            "success": False,
            "error_code": "timeout",
        },
    ],
)
def test_heartbeat_eligibility_ignores_watcher_unproven_or_sticky_failure(
    watcher_update: dict,
) -> None:
    receipt = _healthy_receipt()
    receipt["delivery"]["external_watcher"].update(watcher_update)
    receipt["state"] = "BLIND"
    receipt["overall_color"] = "RED"
    receipt["reason_codes"] = ["watcher_unavailable"]

    assert heartbeat_eligible(
        receipt,
        at=datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
    ) is True


def test_heartbeat_eligibility_rejects_active_watchdog_until_resolved() -> None:
    active = _healthy_receipt()
    active["watchdog"].update(
        {"state": "BLIND", "active": True, "resolved_at": None}
    )

    assert heartbeat_eligible(active) is False
    assert heartbeat_eligible(
        _healthy_receipt(),
        at=datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
    ) is True


def test_heartbeat_eligibility_requires_configured_active_whatsapp_proof() -> None:
    receipt = _healthy_receipt()
    receipt["delivery"]["whatsapp"].update(
        {"configured": True, "mode": "ACTIVE"}
    )

    assert heartbeat_eligible(receipt) is False

    receipt["delivery"]["whatsapp"].update(
        {
            "last_attempt_at": "2026-08-10T13:27:00+00:00",
            "last_success_at": "2026-08-10T13:27:00+00:00",
            "success": True,
        }
    )
    assert heartbeat_eligible(
        receipt,
        at=datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
    ) is True


def test_heartbeat_eligibility_rejects_stale_mobile_trust_proof() -> None:
    receipt = _healthy_receipt()
    last_success = datetime(2026, 8, 10, 13, 26, tzinfo=UTC)

    assert heartbeat_eligible(
        receipt,
        at=last_success + timedelta(hours=24),
    ) is True
    assert heartbeat_eligible(
        receipt,
        at=last_success + timedelta(hours=24, seconds=1),
    ) is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update({"delivery_mode": "PREVIEW"}),
        lambda item: item["silence"].update({"state": "RECOVERING"}),
        lambda item: item["silence"].update({"enabled": 0, "usable": 0}),
        lambda item: item["silence"]["fresh_data"].update({"known": False}),
        lambda item: item["silence"]["fresh_data"].update({"usable": 0}),
        lambda item: item["silence"]["trusted_decision"].update({"usable": 0}),
        lambda item: item["schedule"]["markets"][0].update(
            {"deadline_state": "missing"}
        ),
        lambda item: item["schedule"].update({"markets": []}),
        lambda item: item["schedule"]["markets"][0].update(
            {"deadline_state": "banana"}
        ),
        lambda item: item.update({"reason_codes": ["state_corrupt"]}),
        lambda item: item["delivery"]["telegram"].update(
            {"last_success_at": None, "success": None}
        ),
        lambda item: item["delivery"]["telegram"].update(
            {"success": False, "error_code": "timeout"}
        ),
        lambda item: item["delivery"]["telegram"].update(
            {"configured": False}
        ),
    ],
)
def test_heartbeat_eligibility_fails_closed_for_untrusted_prerequisite(
    mutate,
) -> None:
    receipt = deepcopy(_healthy_receipt())
    mutate(receipt)

    assert heartbeat_eligible(receipt) is False
