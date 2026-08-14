"""Guardian client boundary for the desktop application.

Production IPC reuses :mod:`guardian.protocol` and the canonical event-driven
:class:`guardian.transport_qt.QtGuardianAsyncClient`.  It uses Qt socket
signals and bounded timers, so the GUI event loop never waits on local IPC.
The presentation layer receives only redacted DTOs; it never opens SQLite,
discovers runtime tokens, or imports notification SDKs.
"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, QTimer, Signal, Slot

from guardian.protocol import (
    JSONRPC_VERSION,
    RpcErrorResponse,
    RpcRequest,
    RpcSuccessResponse,
)
from guardian.transport_qt import (
    QtConnectionError,
    QtGuardianAsyncClient,
    QtRequestCancelled,
    QtRequestTimeout,
    QtResponseIdMismatch,
    QtResponseProtocolError,
    QtResponseTooLarge,
    QtTransportError,
    QtTransportUnavailable,
)

from .models import (
    ActionReceipt,
    ChannelKind,
    DashboardSnapshot,
    PayloadError,
    Preferences,
    public_config_update,
)


class GuardianClient(QObject):
    """Signal-based adapter consumed by the Qt presentation layer."""

    snapshot_ready = Signal(object)
    request_failed = Signal(str, str)
    busy_changed = Signal(bool)
    action_completed = Signal(object)
    configuration_saved = Signal(object)
    connection_state_changed = Signal(str)

    def refresh(self) -> None:
        raise NotImplementedError

    def request_scan(self) -> None:
        raise NotImplementedError

    def test_channel(self, channel: ChannelKind) -> None:
        raise NotImplementedError

    def update_preferences(
        self, preferences: Preferences, *, revision: int
    ) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Release transport resources without mutating Guardian state."""


class FixtureGuardianClient(GuardianClient):
    """Offline client with method-shaped payloads matching Guardian DTOs."""

    def __init__(self, fixture_path: Path | None = None) -> None:
        super().__init__()
        self._fixture_path = fixture_path or (
            Path(__file__).parent / "fixtures" / "guardian.json"
        )
        self._payload: dict[str, Any] | None = None
        self._action_counter = 0

    def _load(self) -> dict[str, Any]:
        if self._payload is None:
            try:
                value = json.loads(self._fixture_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise PayloadError("fixture_unavailable") from None
            if not isinstance(value, dict):
                raise PayloadError("fixture_invalid:object")
            self._payload = value
        return self._payload

    def _snapshot(self) -> DashboardSnapshot:
        payload = self._load()
        return DashboardSnapshot.from_payloads(
            health=payload.get("health"),
            cockpit=payload.get("cockpit"),
            config=payload.get("config"),
            incidents=payload.get("incidents"),
            providers=payload.get("providers"),
        )

    def refresh(self) -> None:
        self.busy_changed.emit(True)

        def complete() -> None:
            try:
                snapshot = self._snapshot()
            except PayloadError as exc:
                self.request_failed.emit("refresh", str(exc))
            else:
                self.connection_state_changed.emit("fixture")
                self.snapshot_ready.emit(snapshot)
            finally:
                self.busy_changed.emit(False)

        QTimer.singleShot(20, complete)

    def _action(self, action: str, message: str) -> None:
        self.busy_changed.emit(True)

        def complete() -> None:
            self._action_counter += 1
            self.action_completed.emit(
                ActionReceipt(
                    action=action,
                    accepted=True,
                    message=message,
                    request_id=f"FIX-{self._action_counter:04d}",
                )
            )
            self.busy_changed.emit(False)

        QTimer.singleShot(180, complete)

    def request_scan(self) -> None:
        self._action("scan", "已交给 Guardian；fixture 不执行外部调用")

    def test_channel(self, channel: ChannelKind) -> None:
        self._action(
            "test-channel",
            f"{channel.value} 测试回执已生成（fixture，不真实发送）",
        )

    def update_preferences(
        self, preferences: Preferences, *, revision: int
    ) -> None:
        self.busy_changed.emit(True)

        def complete() -> None:
            payload = self._load()
            config = payload.setdefault("config", {})
            if not isinstance(config, dict):
                self.request_failed.emit("update-config", "fixture_invalid:config")
                self.busy_changed.emit(False)
                return
            update = public_config_update(preferences, revision)
            config["preferences"] = deepcopy(update["preferences"])
            config["revision"] = revision + 1
            self.configuration_saved.emit(preferences)
            self.snapshot_ready.emit(self._snapshot())
            self.busy_changed.emit(False)

        QTimer.singleShot(120, complete)


class LocalSocketGuardianClient(GuardianClient):
    """Asynchronous UI adapter over Guardian's canonical JSON-RPC transport.

    The trusted launcher injects ``runtime_token``.  It stays inside this
    adapter and is never returned in a DTO, frame log, status message, or UI
    field.  All method names come from Guardian's allowlisted protocol.
    """

    _REFRESH_METHODS: tuple[tuple[str, dict[str, Any]], ...] = (
        ("health.get", {}),
        ("cockpit.get", {}),
        ("config.get", {}),
        ("runs.list", {"limit": 50}),
        ("incidents.list", {"limit": 50}),
        ("providers.list", {}),
    )

    def __init__(
        self,
        socket_name: str,
        runtime_token: str,
        *,
        request_timeout_ms: int = 6_000,
    ) -> None:
        super().__init__()
        if not socket_name.strip():
            raise ValueError("socket_name must not be empty")
        if not runtime_token.strip():
            raise ValueError("runtime_token must not be empty")
        if not 100 <= request_timeout_ms <= 60_000:
            raise ValueError("request_timeout_ms must be between 100 and 60000")
        self._transport = QtGuardianAsyncClient(
            socket_name, timeout_ms=request_timeout_ms
        )
        self._runtime_token = runtime_token
        self._operations: dict[str, dict[str, Any]] = {}
        self._closed = False

    def _operation_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def _start_operation(
        self,
        purpose: str,
        calls: tuple[tuple[str, dict[str, Any]], ...],
        *,
        context: object | None = None,
    ) -> str:
        operation_id = self._operation_id()
        self._operations[operation_id] = {
            "purpose": purpose,
            "pending": {method for method, _params in calls},
            "results": {},
            "handles": {},
            "context": context,
        }
        self.busy_changed.emit(True)
        self.connection_state_changed.emit("connecting")
        for method, params in calls:
            try:
                request = RpcRequest.model_validate(
                    {
                        "jsonrpc": JSONRPC_VERSION,
                        "id": f"desktop:{operation_id}:{method}",
                        "method": method,
                        "params": params,
                        "auth_token": self._runtime_token,
                    }
                )
                handle = self._transport.request(
                    request,
                    on_response=partial(
                        self._on_response, operation_id, method
                    ),
                    on_error=partial(
                        self._on_transport_error, operation_id, method
                    ),
                )
            except QtTransportUnavailable:
                self._on_failure(operation_id, method, "transport_unavailable")
                break
            except QtTransportError:
                self._on_failure(operation_id, method, "guardian_unavailable")
                break
            except Exception:  # noqa: BLE001 - typed IPC construction boundary
                self._on_failure(operation_id, method, "invalid_request")
                break
            operation = self._operations.get(operation_id)
            if operation is None:
                break
            operation["handles"][method] = handle
        return operation_id

    def refresh(self) -> None:
        if self._closed or any(
            operation.get("purpose") == "refresh"
            for operation in self._operations.values()
        ):
            return
        self._start_operation("refresh", self._REFRESH_METHODS)

    def request_scan(self) -> None:
        if self._closed:
            return
        self._start_operation("scan", (("scan.trigger", {}),))

    def test_channel(self, channel: ChannelKind) -> None:
        if self._closed:
            return
        if channel is ChannelKind.EXTERNAL_WATCHER:
            self.request_failed.emit("test-channel", "unsupported_channel")
            return
        self._start_operation(
            "test-channel",
            (("delivery.test", {"channel": channel.value}),),
        )

    def update_preferences(
        self, preferences: Preferences, *, revision: int
    ) -> None:
        if self._closed:
            return
        payload = public_config_update(preferences, revision)
        self._start_operation(
            "config-validate",
            (("config.validate", payload),),
            context={"preferences": preferences, "payload": payload},
        )

    def _on_response(
        self, operation_id: str, method: str, response: object
    ) -> None:
        if isinstance(response, RpcErrorResponse):
            self._on_failure(
                operation_id, method, response.error.data.kind
            )
            return
        if isinstance(response, RpcSuccessResponse):
            self._on_success(operation_id, method, response.result)
            return
        self._on_failure(operation_id, method, "invalid_response")

    def _on_transport_error(
        self, operation_id: str, method: str, error: object
    ) -> None:
        if self._closed:
            return
        if isinstance(error, QtConnectionError):
            code = "guardian_unavailable"
        elif isinstance(error, QtRequestTimeout):
            code = "request_timeout"
        elif isinstance(error, QtResponseIdMismatch):
            code = "response_id_mismatch"
        elif isinstance(error, QtResponseTooLarge):
            code = "response_too_large"
        elif isinstance(error, QtResponseProtocolError):
            code = "invalid_response"
        elif isinstance(error, QtRequestCancelled):
            code = "request_cancelled"
        else:
            code = "transport_error"
        self._on_failure(operation_id, method, code)

    @Slot(str, str, object)
    def _on_success(
        self, operation_id: str, method: str, result: object
    ) -> None:
        operation = self._operations.get(operation_id)
        if operation is None:
            return
        operation["results"][method] = result
        operation["pending"].discard(method)
        if operation["pending"]:
            return
        purpose = operation["purpose"]
        self.connection_state_changed.emit("connected")
        if purpose == "refresh":
            self._complete_refresh(operation)
            self._operations.pop(operation_id, None)
        elif purpose == "config-validate":
            context = operation.get("context")
            self._operations.pop(operation_id, None)
            if not isinstance(context, dict) or not isinstance(
                context.get("payload"), dict
            ):
                self.request_failed.emit("config", "invalid_response")
            else:
                self._start_operation(
                    "config-apply",
                    (("config.apply", context["payload"]),),
                    context=context.get("preferences"),
                )
        elif purpose == "config-apply":
            self.configuration_saved.emit(operation.get("context"))
            self._operations.pop(operation_id, None)
            QTimer.singleShot(0, self.refresh)
        else:
            self._complete_action(purpose, operation, method)
            self._operations.pop(operation_id, None)
        self._emit_idle_if_needed()

    def _complete_refresh(self, operation: dict[str, Any]) -> None:
        results = operation["results"]
        cockpit = results.get("cockpit.get")
        config = results.get("config.get")
        cockpit_data = cockpit if isinstance(cockpit, dict) else {}
        config_data = config if isinstance(config, dict) else {}

        providers = results.get("providers.list", {"capabilities": []})
        incidents = results.get("incidents.list")
        if incidents is None:
            incidents = {"items": []}
        if isinstance(incidents, list):
            incidents = {"items": incidents}
        if isinstance(providers, list):
            providers = {"capabilities": providers}
        try:
            snapshot = DashboardSnapshot.from_payloads(
                health=results.get("health.get"),
                cockpit=cockpit_data,
                config=config_data,
                incidents=incidents,
                providers=providers,
            )
        except PayloadError as exc:
            self.request_failed.emit("refresh", str(exc))
        else:
            self.snapshot_ready.emit(snapshot)

    def _complete_action(
        self, purpose: str, operation: dict[str, Any], method: str
    ) -> None:
        result = operation["results"].get(method)
        if isinstance(result, dict):
            payload = dict(result)
            payload.setdefault("action", purpose)
            payload.setdefault("accepted", True)
            payload.setdefault("message", "Guardian 已接受请求")
            payload.setdefault("request_id", operation.get("request_id", "LOCAL"))
        else:
            payload = {
                "action": purpose,
                "accepted": True,
                "message": "Guardian 已接受请求",
                "request_id": "LOCAL",
            }
        try:
            receipt = ActionReceipt.from_payload(payload)
        except PayloadError as exc:
            self.request_failed.emit(purpose, str(exc))
        else:
            self.action_completed.emit(receipt)

    @Slot(str, str, str)
    def _on_failure(
        self, operation_id: str, method: str, safe_code: str
    ) -> None:
        operation = self._operations.pop(operation_id, None)
        if operation is None:
            return
        for handle in operation.get("handles", {}).values():
            if handle.active:
                handle.cancel()
        purpose = str(operation.get("purpose", method))
        self.connection_state_changed.emit("error")
        self.request_failed.emit(purpose, safe_code)
        self._emit_idle_if_needed()

    def _emit_idle_if_needed(self) -> None:
        if not self._operations:
            self.busy_changed.emit(False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._operations.clear()
        self._transport.close()
        self._runtime_token = ""
        self.busy_changed.emit(False)


class ManagedGuardianClient(GuardianClient):
    """Start a missing Guardian once, then reconnect within a fixed budget."""

    _RECONNECT_DELAYS_MS = (120, 220, 380, 600, 900, 1_300, 1_800)
    _STARTABLE_ERRORS = frozenset({"guardian_unavailable"})

    def __init__(
        self,
        transport: GuardianClient,
        *,
        guardian_program: str = "alpha-guard-guardian",
    ) -> None:
        super().__init__()
        if not guardian_program.strip():
            raise ValueError("guardian_program must not be empty")
        self._transport = transport
        self._guardian_program = guardian_program
        self._spawn_attempted = False
        self._reconnect_index = 0
        self._closed = False
        transport.snapshot_ready.connect(self._on_snapshot)
        transport.request_failed.connect(self._on_failure)
        transport.busy_changed.connect(self.busy_changed)
        transport.action_completed.connect(self.action_completed)
        transport.configuration_saved.connect(self.configuration_saved)
        transport.connection_state_changed.connect(self.connection_state_changed)

    def refresh(self) -> None:
        if not self._closed:
            self._transport.refresh()

    def request_scan(self) -> None:
        if not self._closed:
            self._transport.request_scan()

    def test_channel(self, channel: ChannelKind) -> None:
        if not self._closed:
            self._transport.test_channel(channel)

    def update_preferences(
        self, preferences: Preferences, *, revision: int
    ) -> None:
        if not self._closed:
            self._transport.update_preferences(preferences, revision=revision)

    @Slot(object)
    def _on_snapshot(self, snapshot: object) -> None:
        self._reconnect_index = 0
        self.snapshot_ready.emit(snapshot)

    @Slot(str, str)
    def _on_failure(self, purpose: str, code: str) -> None:
        if (
            purpose == "refresh"
            and code in self._STARTABLE_ERRORS
            and self._reconnect_index < len(self._RECONNECT_DELAYS_MS)
            and not self._closed
        ):
            if not self._spawn_attempted:
                self._spawn_attempted = True
                started = QProcess.startDetached(self._guardian_program, [])
                if isinstance(started, tuple):
                    launched = bool(started[0])
                else:
                    launched = bool(started)
                if not launched:
                    self.request_failed.emit("startup", "guardian_start_failed")
                    return
                self.connection_state_changed.emit("starting")
            delay = self._RECONNECT_DELAYS_MS[self._reconnect_index]
            self._reconnect_index += 1
            QTimer.singleShot(delay, self.refresh)
            return
        self.request_failed.emit(purpose, code)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._transport.close()
