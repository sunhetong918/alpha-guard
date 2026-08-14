"""Optional PySide6 local-socket transport for Guardian.

Importing this module never imports Qt.  This preserves the CLI-only install
and makes protocol tests independent of a GUI runtime.  ``PySide6`` is loaded
only by ``start`` or ``request`` and absence produces an actionable error.

Desktop code must use :class:`QtGuardianAsyncClient`.  Its socket and timeout
are driven exclusively by Qt signals, so a Guardian server in the same process
and Python UI timers can continue running.  :class:`QtGuardianClient` is a
blocking compatibility client for command-line diagnostics against a Guardian
in another process; it must not be called from a desktop process or QThread.
"""

from __future__ import annotations

import importlib
import re
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable

from .protocol import (
    MAX_FRAME_BYTES,
    FrameDecoder,
    FrameTooLarge,
    InvalidFrame,
    ProtocolViolation,
    RpcErrorCode,
    RpcErrorKind,
    RpcRequest,
    RpcResponse,
    encode_frame,
    error_response,
    parse_response_json,
    serialize_request,
    serialize_response,
)

_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class QtTransportUnavailable(RuntimeError):
    pass


class QtTransportError(RuntimeError):
    pass


class QtConnectionError(QtTransportError):
    pass


class QtRequestTimeout(QtTransportError):
    pass


class QtRequestCancelled(QtTransportError):
    pass


class QtRequestConflict(QtTransportError):
    pass


class QtResponseIdMismatch(QtTransportError):
    pass


class QtResponseTooLarge(QtTransportError):
    pass


class QtResponseProtocolError(QtTransportError):
    pass


class GuardianInstanceAlreadyRunning(QtTransportError):
    pass


@dataclass(frozen=True, slots=True)
class QtBindings:
    QCoreApplication: Any
    QTimer: Any
    QLocalServer: Any
    QLocalSocket: Any


def load_qt_bindings() -> QtBindings:
    try:
        core = importlib.import_module("PySide6.QtCore")
        network = importlib.import_module("PySide6.QtNetwork")
    except (ImportError, ModuleNotFoundError) as exc:
        raise QtTransportUnavailable(
            "PySide6 is required for Guardian local IPC; install the desktop extra"
        ) from exc
    try:
        return QtBindings(
            QCoreApplication=core.QCoreApplication,
            QTimer=core.QTimer,
            QLocalServer=network.QLocalServer,
            QLocalSocket=network.QLocalSocket,
        )
    except AttributeError as exc:
        raise QtTransportUnavailable(
            "PySide6.QtNetwork does not provide QLocalServer/QLocalSocket"
        ) from exc


class QtGuardianServer:
    """Event-driven QLocalServer adapter around a bytes-to-bytes handler."""

    def __init__(
        self,
        server_name: str,
        request_handler: Callable[[bytes], bytes],
        *,
        max_clients: int = 16,
    ) -> None:
        self.server_name = _validated_server_name(server_name)
        if not callable(request_handler):
            raise TypeError("request_handler must be callable")
        if not 1 <= max_clients <= 128:
            raise ValueError("max_clients must be between 1 and 128")
        self._request_handler = request_handler
        self._max_clients = max_clients
        self._bindings: QtBindings | None = None
        self._server: Any | None = None
        self._clients: dict[int, tuple[Any, FrameDecoder]] = {}

    @property
    def is_running(self) -> bool:
        return bool(self._server is not None and self._server.isListening())

    def start(self) -> None:
        if self.is_running:
            return
        bindings = load_qt_bindings()
        if bindings.QCoreApplication.instance() is None:
            raise QtTransportError(
                "QCoreApplication must be created before Guardian IPC starts"
            )

        server = bindings.QLocalServer()
        user_access = _user_access_option(bindings.QLocalServer)
        if user_access is not None:
            server.setSocketOptions(user_access)
        server.setMaxPendingConnections(self._max_clients)
        if not server.listen(self.server_name):
            if _server_is_live(bindings, self.server_name):
                server.deleteLater()
                raise GuardianInstanceAlreadyRunning(
                    "another Guardian instance already owns the local endpoint"
                )
            # QLocalServer can leave a stale Unix-domain endpoint after an
            # unclean exit.  Remove it only after a connection probe failed;
            # the subsequent listen remains the single-instance arbiter.
            bindings.QLocalServer.removeServer(self.server_name)
            if not server.listen(self.server_name):
                server.deleteLater()
                if _server_is_live(bindings, self.server_name):
                    raise GuardianInstanceAlreadyRunning(
                        "another Guardian instance already owns the local endpoint"
                    )
                raise QtTransportError("Guardian local endpoint could not be opened")

        self._bindings = bindings
        self._server = server
        server.newConnection.connect(self._accept_connections)

    def stop(self) -> None:
        for key in tuple(self._clients):
            self._drop_client(key)
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
        if self._bindings is not None:
            self._bindings.QLocalServer.removeServer(self.server_name)
        self._server = None
        self._bindings = None

    def _accept_connections(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                return
            if len(self._clients) >= self._max_clients:
                socket.disconnectFromServer()
                socket.deleteLater()
                continue
            key = id(socket)
            self._clients[key] = (socket, FrameDecoder())
            socket.readyRead.connect(lambda key=key: self._read_client(key))
            socket.disconnected.connect(lambda key=key: self._drop_client(key))

    def _read_client(self, key: int) -> None:
        entry = self._clients.get(key)
        if entry is None:
            return
        socket, decoder = entry
        chunk = bytes(socket.readAll())
        try:
            frames = decoder.feed(chunk)
        except FrameTooLarge:
            self._write_protocol_error(
                socket,
                code=RpcErrorCode.FRAME_TOO_LARGE,
                kind="frame_too_large",
                message="Guardian frame exceeds the size limit",
                max_frame_bytes=MAX_FRAME_BYTES,
            )
            socket.disconnectFromServer()
            return
        except InvalidFrame:
            self._write_protocol_error(
                socket,
                code=RpcErrorCode.INVALID_REQUEST,
                kind="invalid_request",
                message="Guardian frame is invalid",
            )
            socket.disconnectFromServer()
            return

        for frame in frames:
            try:
                response = self._request_handler(frame)
                if not isinstance(response, bytes):
                    raise TypeError("request handler response must be bytes")
                framed = encode_frame(response)
            except Exception:  # noqa: BLE001 - transport trust boundary
                fallback = error_response(
                    None,
                    code=RpcErrorCode.INTERNAL_ERROR,
                    kind="internal_error",
                    message="Guardian request failed",
                )
                framed = encode_frame(serialize_response(fallback))
            socket.write(framed)
        socket.flush()

    def _write_protocol_error(
        self,
        socket: Any,
        *,
        code: RpcErrorCode,
        kind: RpcErrorKind,
        message: str,
        max_frame_bytes: int | None = None,
    ) -> None:
        response = error_response(
            None,
            code=code,
            kind=kind,
            message=message,
            max_frame_bytes=max_frame_bytes,
        )
        socket.write(encode_frame(serialize_response(response)))
        socket.flush()

    def _drop_client(self, key: int) -> None:
        entry = self._clients.pop(key, None)
        if entry is None:
            return
        socket, decoder = entry
        decoder.reset()
        socket.deleteLater()


class QtGuardianClient:
    """Blocking CLI compatibility client for a separate Guardian process.

    Do not call this client from a desktop process, including from a QThread.
    PySide's ``waitFor*`` methods can retain the GIL while waiting, preventing
    Python slots, timers, and an in-process Guardian server from executing.
    Desktop code must use :class:`QtGuardianAsyncClient`.
    """

    def __init__(self, server_name: str, *, timeout_ms: int = 5_000) -> None:
        self.server_name = _validated_server_name(server_name)
        if not 100 <= timeout_ms <= 60_000:
            raise ValueError("timeout_ms must be between 100 and 60000")
        self.timeout_ms = timeout_ms

    def request(self, request: RpcRequest) -> RpcResponse:
        bindings = load_qt_bindings()
        socket = bindings.QLocalSocket()
        decoder = FrameDecoder()
        deadline = time.monotonic() + (self.timeout_ms / 1_000)
        try:
            socket.connectToServer(self.server_name)
            if not socket.waitForConnected(self.timeout_ms):
                raise QtTransportError("Guardian local endpoint is unavailable")
            socket.write(encode_frame(serialize_request(request)))
            if not socket.waitForBytesWritten(_remaining_ms(deadline)):
                raise QtTransportError("Guardian request could not be written")
            while time.monotonic() < deadline:
                if socket.bytesAvailable() == 0 and not socket.waitForReadyRead(
                    _remaining_ms(deadline)
                ):
                    continue
                for frame in decoder.feed(bytes(socket.readAll())):
                    response = parse_response_json(frame)
                    if response.request_id != request.request_id:
                        raise QtTransportError(
                            "Guardian response request id does not match"
                        )
                    return response
            raise QtTransportError("Guardian request timed out")
        finally:
            socket.abort()
            socket.deleteLater()


ResponseCallback = Callable[[RpcResponse], None]
ErrorCallback = Callable[[QtTransportError], None]


class QtPendingRequest:
    """Cancelable handle returned by :meth:`QtGuardianAsyncClient.request`."""

    def __init__(self, client: QtGuardianAsyncClient, request_id: str) -> None:
        self.request_id = request_id
        self._client = weakref.ref(client)
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def cancel(self) -> bool:
        client = self._client()
        if not self._active or client is None:
            return False
        return client._cancel(self)

    def _finish(self) -> None:
        self._active = False


@dataclass(slots=True)
class _AsyncOperation:
    request: RpcRequest
    socket: Any
    timer: Any
    decoder: FrameDecoder
    outbound: bytes
    on_response: ResponseCallback
    on_error: ErrorCallback
    handle: QtPendingRequest
    write_offset: int = 0


class QtGuardianAsyncClient:
    """Event-driven Guardian client safe for a Qt desktop event loop.

    ``request`` must be called from a thread with a running Qt event loop.  It
    never blocks and invokes exactly one callback on that same thread:
    ``on_response`` for a valid JSON-RPC response (including typed RPC errors),
    or ``on_error`` for a connection, timeout, framing, or correlation failure.
    Each in-flight request owns one ``QLocalSocket`` and a single-shot
    ``QTimer``; this keeps cancellation and concurrent request IDs isolated.
    """

    def __init__(self, server_name: str, *, timeout_ms: int = 5_000) -> None:
        self.server_name = _validated_server_name(server_name)
        if not 100 <= timeout_ms <= 60_000:
            raise ValueError("timeout_ms must be between 100 and 60000")
        self.timeout_ms = timeout_ms
        self._operations: dict[str, _AsyncOperation] = {}

    @property
    def pending_request_ids(self) -> tuple[str, ...]:
        return tuple(self._operations)

    def request(
        self,
        request: RpcRequest,
        *,
        on_response: ResponseCallback,
        on_error: ErrorCallback,
    ) -> QtPendingRequest:
        if not isinstance(request, RpcRequest):
            raise TypeError("request must be an RpcRequest")
        if not callable(on_response):
            raise TypeError("on_response must be callable")
        if not callable(on_error):
            raise TypeError("on_error must be callable")
        if request.request_id in self._operations:
            raise QtRequestConflict("Guardian request id is already in flight")

        # Serialize before allocating Qt resources.  Oversized or non-JSON-safe
        # requests remain immediate local validation failures.
        outbound = encode_frame(serialize_request(request))
        bindings = load_qt_bindings()
        if bindings.QCoreApplication.instance() is None:
            raise QtTransportError(
                "QCoreApplication must be created before Guardian IPC starts"
            )

        socket = bindings.QLocalSocket()
        timer = bindings.QTimer(socket)
        timer.setSingleShot(True)
        handle = QtPendingRequest(self, request.request_id)
        operation = _AsyncOperation(
            request=request,
            socket=socket,
            timer=timer,
            decoder=FrameDecoder(),
            outbound=outbound,
            on_response=on_response,
            on_error=on_error,
            handle=handle,
        )
        self._operations[request.request_id] = operation

        socket.connected.connect(lambda op=operation: self._on_connected(op))
        socket.bytesWritten.connect(
            lambda _count, op=operation: self._write_remaining(op)
        )
        socket.readyRead.connect(lambda op=operation: self._read_response(op))
        socket.disconnected.connect(
            lambda op=operation: self._on_disconnected(op)
        )
        socket.errorOccurred.connect(
            lambda _error, op=operation: self._on_socket_error(op)
        )
        timer.timeout.connect(lambda op=operation: self._on_timeout(op))

        timer.start(self.timeout_ms)
        socket.connectToServer(self.server_name)
        return handle

    def close(self) -> None:
        for operation in tuple(self._operations.values()):
            self._finish_error(
                operation,
                QtRequestCancelled("Guardian client closed"),
            )

    def _cancel(self, handle: QtPendingRequest) -> bool:
        operation = self._operations.get(handle.request_id)
        if operation is None or operation.handle is not handle:
            handle._finish()
            return False
        self._finish_error(
            operation,
            QtRequestCancelled("Guardian request was cancelled"),
        )
        return True

    def _on_connected(self, operation: _AsyncOperation) -> None:
        self._write_remaining(operation)

    def _write_remaining(self, operation: _AsyncOperation) -> None:
        if not self._is_active(operation):
            return
        if operation.write_offset >= len(operation.outbound):
            return
        remaining = operation.outbound[operation.write_offset :]
        written = int(operation.socket.write(remaining))
        if written < 0:
            self._finish_error(
                operation,
                QtConnectionError("Guardian request could not be written"),
            )
            return
        operation.write_offset += written
        operation.socket.flush()

    def _read_response(self, operation: _AsyncOperation) -> None:
        if not self._is_active(operation):
            return
        chunk = bytes(operation.socket.readAll())
        try:
            frames = operation.decoder.feed(chunk)
        except FrameTooLarge:
            self._finish_error(
                operation,
                QtResponseTooLarge(
                    f"Guardian response exceeds {MAX_FRAME_BYTES} byte limit"
                ),
            )
            return
        except InvalidFrame:
            self._finish_error(
                operation,
                QtResponseProtocolError("Guardian response frame is invalid"),
            )
            return

        if not frames:
            return
        if len(frames) != 1:
            self._finish_error(
                operation,
                QtResponseProtocolError(
                    "Guardian returned multiple responses for one request"
                ),
            )
            return
        try:
            response = parse_response_json(frames[0])
        except ProtocolViolation:
            self._finish_error(
                operation,
                QtResponseProtocolError("Guardian response is invalid"),
            )
            return
        if response.request_id != operation.request.request_id:
            self._finish_error(
                operation,
                QtResponseIdMismatch(
                    "Guardian response request id does not match"
                ),
            )
            return
        self._finish_response(operation, response)

    def _on_disconnected(self, operation: _AsyncOperation) -> None:
        if not self._is_active(operation):
            return
        if operation.socket.bytesAvailable() > 0:
            self._read_response(operation)
        if self._is_active(operation):
            self._finish_error(
                operation,
                QtConnectionError("Guardian closed the local connection"),
            )

    def _on_socket_error(self, operation: _AsyncOperation) -> None:
        if not self._is_active(operation):
            return
        self._finish_error(
            operation,
            QtConnectionError("Guardian local endpoint is unavailable"),
        )

    def _on_timeout(self, operation: _AsyncOperation) -> None:
        if not self._is_active(operation):
            return
        self._finish_error(
            operation,
            QtRequestTimeout("Guardian request timed out"),
        )

    def _finish_response(
        self,
        operation: _AsyncOperation,
        response: RpcResponse,
    ) -> None:
        if not self._take(operation):
            return
        operation.on_response(response)

    def _finish_error(
        self,
        operation: _AsyncOperation,
        error: QtTransportError,
    ) -> None:
        if not self._take(operation):
            return
        operation.on_error(error)

    def _take(self, operation: _AsyncOperation) -> bool:
        if not self._is_active(operation):
            return False
        self._operations.pop(operation.request.request_id, None)
        operation.timer.stop()
        operation.decoder.reset()
        operation.handle._finish()
        operation.socket.abort()
        operation.socket.deleteLater()
        return True

    def _is_active(self, operation: _AsyncOperation) -> bool:
        return self._operations.get(operation.request.request_id) is operation


def _server_is_live(bindings: QtBindings, server_name: str) -> bool:
    probe = bindings.QLocalSocket()
    try:
        probe.connectToServer(server_name)
        return bool(probe.waitForConnected(150))
    finally:
        probe.abort()
        probe.deleteLater()


def _user_access_option(local_server: Any) -> Any | None:
    socket_option = getattr(local_server, "SocketOption", None)
    if socket_option is not None:
        return getattr(socket_option, "UserAccessOption", None)
    return getattr(local_server, "UserAccessOption", None)


def _remaining_ms(deadline: float) -> int:
    return max(1, int((deadline - time.monotonic()) * 1_000))


def _validated_server_name(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("server_name must be a string")
    if not _SERVER_NAME.fullmatch(value):
        raise ValueError("server_name is invalid")
    return value


__all__ = [
    "GuardianInstanceAlreadyRunning",
    "QtBindings",
    "QtConnectionError",
    "QtGuardianAsyncClient",
    "QtGuardianClient",
    "QtGuardianServer",
    "QtPendingRequest",
    "QtRequestCancelled",
    "QtRequestConflict",
    "QtRequestTimeout",
    "QtResponseIdMismatch",
    "QtResponseProtocolError",
    "QtResponseTooLarge",
    "QtTransportError",
    "QtTransportUnavailable",
    "load_qt_bindings",
]
