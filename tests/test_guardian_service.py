from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from guardian import service
from guardian.transport_qt import GuardianInstanceAlreadyRunning


class _FakeCoreApplication:
    _instance = None

    def __init__(self, _argv) -> None:
        type(self)._instance = self

    @classmethod
    def instance(cls):
        return cls._instance

    def exec(self) -> int:
        return 0

    def quit(self) -> None:
        pass


class _FakeTimer:
    @staticmethod
    def singleShot(_delay: int, callback) -> None:  # noqa: N802
        callback()


@pytest.fixture(autouse=True)
def _reset_qt_instance() -> None:
    _FakeCoreApplication._instance = None


def _install_fake_qt(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("PySide6.QtCore")
    module.QCoreApplication = _FakeCoreApplication
    module.QTimer = _FakeTimer
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", module)


def _patch_startup_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[str],
    duplicate: bool = False,
) -> None:
    class TokenStore:
        def load_or_create(self) -> str:
            return "a" * 43

    class Runtime:
        def __init__(self, *, notify: bool) -> None:
            del notify

        def start(self) -> None:
            events.append("runtime.start")

        def stop(self) -> None:
            events.append("runtime.stop")

    class Server:
        def __init__(self, _name: str, _handler) -> None:
            pass

        def start(self) -> None:
            events.append("server.start")
            if duplicate:
                raise GuardianInstanceAlreadyRunning("already running")

        def stop(self) -> None:
            events.append("server.stop")

    monkeypatch.setattr(service, "GuardianTokenStore", TokenStore)
    monkeypatch.setattr(service, "GuardianSupervisor", Runtime)
    monkeypatch.setattr(service, "AlphaGuardBackend", lambda **_kwargs: object())
    monkeypatch.setattr(
        service,
        "GuardianApplication",
        lambda _token, *, backend: SimpleNamespace(handle_json=lambda _raw: b""),
    )
    monkeypatch.setattr(service, "QtGuardianServer", Server)
    monkeypatch.setattr(
        service,
        "build_instance_names",
        lambda: SimpleNamespace(guardian="alpha-guard-test"),
    )
    monkeypatch.setattr(service, "_notifications_configured", lambda: False)


def test_guardian_claims_single_instance_before_runtime_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_qt(monkeypatch)
    events: list[str] = []
    _patch_startup_dependencies(monkeypatch, events=events, duplicate=True)
    monkeypatch.setattr(sys, "argv", ["alpha-guard-guardian"])

    assert service.main() == 0
    assert events == ["server.start"]


def test_guardian_normal_start_orders_endpoint_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_qt(monkeypatch)
    events: list[str] = []
    _patch_startup_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(sys, "argv", ["alpha-guard-guardian"])

    assert service.main() == 0
    assert events == [
        "server.start",
        "runtime.start",
        "server.stop",
        "runtime.stop",
    ]
