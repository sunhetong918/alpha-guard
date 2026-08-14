from __future__ import annotations

import importlib

import pytest

from guardian.transport_qt import (
    QtGuardianClient,
    QtGuardianServer,
    QtTransportUnavailable,
    load_qt_bindings,
)


def test_transport_objects_do_not_import_qt_until_started(monkeypatch) -> None:
    attempted: list[str] = []

    def missing(name: str):
        attempted.append(name)
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(importlib, "import_module", missing)
    server = QtGuardianServer("alpha-guard.guardian.test", lambda payload: payload)
    client = QtGuardianClient("alpha-guard.guardian.test")

    assert attempted == []
    assert server.is_running is False
    with pytest.raises(QtTransportUnavailable, match="desktop extra"):
        server.start()
    with pytest.raises(QtTransportUnavailable, match="desktop extra"):
        client.request  # accessing the method still must not import Qt
        load_qt_bindings()
    assert attempted == ["PySide6.QtCore", "PySide6.QtCore"]


@pytest.mark.parametrize(
    "name",
    ["", "has space", "../socket", "x" * 97],
)
def test_transport_rejects_unsafe_or_overlong_server_names(name: str) -> None:
    with pytest.raises(ValueError):
        QtGuardianServer(name, lambda payload: payload)
    with pytest.raises(ValueError):
        QtGuardianClient(name)
