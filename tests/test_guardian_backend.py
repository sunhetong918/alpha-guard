from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import Settings
from guardian.application import GuardianDispatchError, RequestContext
from guardian.backend import AlphaGuardBackend
from guardian.preferences import PreferencesStore

NOW = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)


class _Runtime:
    status = SimpleNamespace(state="RUNNING", error_code=None)

    def __init__(self) -> None:
        self.scans: list[str | None] = []
        self.channels: list[str] = []

    def submit_scan(self, market=None) -> str:
        self.scans.append(market)
        return "scan-0123456789abcdef"

    def submit_delivery_test(self, channel) -> str:
        self.channels.append(channel)
        return "delivery-0123456789abcdef"


def _context(method: str) -> RequestContext:
    return RequestContext(request_id="desktop-request-1", method=method)


def _backend(tmp_path: Path, **kwargs) -> AlphaGuardBackend:
    return AlphaGuardBackend(
        settings_loader=lambda: Settings(),
        clock=lambda: NOW,
        preferences_store=PreferencesStore(tmp_path / "preferences.json"),
        **kwargs,
    )


def _preferences(*, launch_at_login: bool = False) -> dict[str, object]:
    return {
        "timezone": "Asia/Shanghai",
        "language": "zh-CN",
        "launch_at_login": launch_at_login,
        "quiet_hours_enabled": False,
        "quiet_hours_start": "23:00",
        "quiet_hours_end": "07:30",
    }


def test_health_and_commands_use_background_runtime(tmp_path) -> None:
    runtime = _Runtime()
    backend = _backend(tmp_path, runtime=runtime)

    health = backend.health_get({}, _context("health.get"))
    assert health["guardian"]["state"] == "RUNNING"
    scan = backend.scan_trigger({"market": "US"}, _context("scan.trigger"))
    delivery = backend.delivery_test(
        {"channel": "whatsapp"}, _context("delivery.test")
    )
    assert scan["accepted"] is True
    assert delivery["accepted"] is True
    assert delivery["status"] == "queued"
    assert "提供者接受" not in delivery["message"]
    assert runtime.scans == ["US"]
    assert runtime.channels == ["whatsapp"]


def test_public_preferences_validate_apply_and_reload(tmp_path) -> None:
    calls: list[bool] = []
    backend = _backend(tmp_path, autostart_apply=calls.append)
    payload = {"revision": 0, "preferences": _preferences(launch_at_login=True)}

    assert backend.config_validate(payload, _context("config.validate")) == {
        "valid": True
    }
    applied = backend.config_apply(payload, _context("config.apply"))
    assert applied["revision"] == 1
    assert calls == [True]
    loaded = backend.config_get({}, _context("config.get"))
    assert loaded["revision"] == 1
    assert loaded["preferences"]["launch_at_login"] is True
    rendered = repr(loaded).lower()
    assert "token" not in rendered
    assert "chat_id" not in rendered


def test_stale_preference_revision_never_mutates_autostart(tmp_path) -> None:
    calls: list[bool] = []
    backend = _backend(tmp_path, autostart_apply=calls.append)
    payload = {"revision": 0, "preferences": _preferences(launch_at_login=True)}
    backend.config_apply(payload, _context("config.apply"))

    with pytest.raises(GuardianDispatchError) as captured:
        backend.config_apply(payload, _context("config.apply"))
    assert captured.value.kind == "conflict"
    assert calls == [True]


def test_invalid_preferences_return_low_cardinality_error(tmp_path) -> None:
    backend = _backend(tmp_path)
    payload = {
        "revision": 0,
        "preferences": {
            **_preferences(),
            "timezone": "https://private.invalid/secret-token",
        },
    }
    with pytest.raises(GuardianDispatchError) as captured:
        backend.config_validate(payload, _context("config.validate"))
    assert captured.value.kind == "invalid_params"
    assert "secret-token" not in str(captured.value)


def test_autostart_failure_does_not_persist_preference(tmp_path) -> None:
    def fail(_enabled: bool) -> None:
        raise OSError("private local path")

    backend = _backend(tmp_path, autostart_apply=fail)
    payload = {"revision": 0, "preferences": _preferences(launch_at_login=True)}
    with pytest.raises(GuardianDispatchError) as captured:
        backend.config_apply(payload, _context("config.apply"))
    assert captured.value.kind == "service_unavailable"
    assert backend.config_get({}, _context("config.get"))["revision"] == 0


def test_service_help_and_version_never_import_qt(monkeypatch, capsys) -> None:
    from guardian import service

    monkeypatch.setattr(service.sys, "argv", ["alpha-guard-guardian", "--help"])
    assert service.main() == 0
    assert "usage:" in capsys.readouterr().out

    monkeypatch.setattr(
        service.sys, "argv", ["alpha-guard-guardian", "--version"]
    )
    assert service.main() == 0
    assert capsys.readouterr().out.strip()
