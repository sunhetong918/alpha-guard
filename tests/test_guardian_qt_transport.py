from __future__ import annotations

import struct
import uuid
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtNetwork import QLocalServer

from guardian.application import GuardianApplication, GuardianBackend
from guardian.protocol import (
    MAX_FRAME_BYTES,
    RpcRequest,
    RpcSuccessResponse,
    parse_request_json,
    serialize_response,
    success_response,
)
from guardian.transport_qt import (
    QtGuardianAsyncClient,
    QtGuardianServer,
    QtRequestTimeout,
    QtResponseIdMismatch,
    QtResponseTooLarge,
    QtTransportError,
)

TOKEN = "a" * 43


@pytest.fixture(scope="session")
def qt_app() -> QCoreApplication:
    application = QCoreApplication.instance()
    if application is None:
        application = QCoreApplication([])
    return application


def _server_name(label: str) -> str:
    # macOS QLocalServer expands names below a long per-user temporary path;
    # keep the leaf short enough for the Unix-domain socket path limit.
    return f"ag.{label}.{uuid.uuid4().hex[:8]}"


def _request(request_id: str = "req-1") -> RpcRequest:
    return RpcRequest.model_validate(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "health.get",
            "params": {},
            "auth_token": TOKEN,
        }
    )


def _run_loop_until_callback(
    start: Callable[
        [Callable[[Any], None], Callable[[QtTransportError], None]],
        None,
    ],
    *,
    outer_timeout_ms: int = 2_000,
) -> tuple[list[Any], list[QtTransportError]]:
    loop = QEventLoop()
    responses: list[Any] = []
    errors: list[QtTransportError] = []

    def on_response(response: Any) -> None:
        responses.append(response)
        loop.quit()

    def on_error(error: QtTransportError) -> None:
        errors.append(error)
        loop.quit()

    outer_timeout = QTimer()
    outer_timeout.setSingleShot(True)
    outer_timeout.timeout.connect(loop.quit)
    outer_timeout.start(outer_timeout_ms)
    start(on_response, on_error)
    loop.exec()
    outer_timeout.stop()
    return responses, errors


class _HealthBackend(GuardianBackend):
    def health_get(self, params, context):
        assert params == {}
        return {"status": "ready", "request_id": context.request_id}


def test_async_client_and_server_complete_in_same_python_process(
    qt_app: QCoreApplication,
) -> None:
    del qt_app
    name = _server_name("success")
    guardian = GuardianApplication(TOKEN, backend=_HealthBackend())
    server = QtGuardianServer(name, guardian.handle_json)
    client = QtGuardianAsyncClient(name, timeout_ms=500)
    ticks: list[bool] = []
    heartbeat = QTimer()
    heartbeat.setInterval(0)
    heartbeat.timeout.connect(lambda: ticks.append(True))
    heartbeat.start()
    try:
        server.start()
        handle_box: list[Any] = []

        def start(on_response, on_error) -> None:
            handle_box.append(
                client.request(
                    _request(),
                    on_response=on_response,
                    on_error=on_error,
                )
            )

        responses, errors = _run_loop_until_callback(start)

        assert errors == []
        assert len(responses) == 1
        response = responses[0]
        assert isinstance(response, RpcSuccessResponse)
        assert response.request_id == "req-1"
        assert response.result == {"status": "ready", "request_id": "req-1"}
        assert ticks, "Qt/Python event-loop timers must run while IPC is in flight"
        assert handle_box[0].active is False
        assert client.pending_request_ids == ()
    finally:
        heartbeat.stop()
        client.close()
        server.stop()


def test_async_timeout_does_not_interrupt_ui_event_loop_ticks(
    qt_app: QCoreApplication,
) -> None:
    del qt_app
    name = _server_name("timeout")
    raw_server = QLocalServer()
    QLocalServer.removeServer(name)
    assert raw_server.listen(name)
    accepted: list[Any] = []

    def accept_without_reply() -> None:
        while raw_server.hasPendingConnections():
            socket = raw_server.nextPendingConnection()
            if socket is not None:
                accepted.append(socket)

    raw_server.newConnection.connect(accept_without_reply)
    ticks: list[bool] = []
    heartbeat = QTimer()
    heartbeat.setInterval(5)
    heartbeat.timeout.connect(lambda: ticks.append(True))
    heartbeat.start()
    client = QtGuardianAsyncClient(name, timeout_ms=120)
    try:
        responses, errors = _run_loop_until_callback(
            lambda on_response, on_error: client.request(
                _request(),
                on_response=on_response,
                on_error=on_error,
            )
        )

        assert responses == []
        assert len(errors) == 1
        assert isinstance(errors[0], QtRequestTimeout)
        assert len(ticks) >= 5
        assert accepted
        assert client.pending_request_ids == ()
    finally:
        heartbeat.stop()
        client.close()
        for socket in accepted:
            socket.abort()
            socket.deleteLater()
        raw_server.close()
        raw_server.deleteLater()
        QLocalServer.removeServer(name)


def test_async_client_rejects_response_with_different_request_id(
    qt_app: QCoreApplication,
) -> None:
    del qt_app
    name = _server_name("mismatch")

    def mismatched_response(payload: bytes) -> bytes:
        request = parse_request_json(payload)
        assert request.request_id == "req-1"
        return serialize_response(success_response("req-other", {"status": "ready"}))

    server = QtGuardianServer(name, mismatched_response)
    client = QtGuardianAsyncClient(name, timeout_ms=500)
    try:
        server.start()
        responses, errors = _run_loop_until_callback(
            lambda on_response, on_error: client.request(
                _request(),
                on_response=on_response,
                on_error=on_error,
            )
        )

        assert responses == []
        assert len(errors) == 1
        assert isinstance(errors[0], QtResponseIdMismatch)
        assert client.pending_request_ids == ()
    finally:
        client.close()
        server.stop()


def test_async_client_rejects_oversized_declared_response_before_body(
    qt_app: QCoreApplication,
) -> None:
    del qt_app
    name = _server_name("oversize")
    raw_server = QLocalServer()
    QLocalServer.removeServer(name)
    assert raw_server.listen(name)
    accepted: list[Any] = []

    def send_oversized_header() -> None:
        while raw_server.hasPendingConnections():
            socket = raw_server.nextPendingConnection()
            if socket is None:
                continue
            accepted.append(socket)
            socket.write(struct.pack(">I", MAX_FRAME_BYTES + 1))
            socket.flush()

    raw_server.newConnection.connect(send_oversized_header)
    client = QtGuardianAsyncClient(name, timeout_ms=500)
    try:
        responses, errors = _run_loop_until_callback(
            lambda on_response, on_error: client.request(
                _request(),
                on_response=on_response,
                on_error=on_error,
            )
        )

        assert responses == []
        assert len(errors) == 1
        assert isinstance(errors[0], QtResponseTooLarge)
        assert client.pending_request_ids == ()
    finally:
        client.close()
        for socket in accepted:
            socket.abort()
            socket.deleteLater()
        raw_server.close()
        raw_server.deleteLater()
        QLocalServer.removeServer(name)
