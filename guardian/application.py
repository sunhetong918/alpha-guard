"""Authenticated Guardian request dispatcher.

This module defines the API boundary only.  It deliberately does not import or
call the existing CLI orchestration.  Until a concrete dispatcher is wired,
every allowlisted method returns a typed ``method_not_implemented`` response
instead of fabricated health, status, or configuration data.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from pydantic import JsonValue, TypeAdapter, ValidationError

from .protocol import (
    GUARDIAN_METHODS,
    MAX_FRAME_BYTES,
    ProtocolViolation,
    RpcErrorCode,
    RpcErrorKind,
    RpcRequest,
    RpcResponse,
    best_effort_request_id,
    error_response,
    parse_request_json,
    serialize_response,
    success_response,
)

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_METHOD_TO_HANDLER = {
    "health.get": "health_get",
    "cockpit.get": "cockpit_get",
    "runs.list": "runs_list",
    "incidents.list": "incidents_list",
    "providers.list": "providers_list",
    "config.get": "config_get",
    "config.validate": "config_validate",
    "config.apply": "config_apply",
    "scan.trigger": "scan_trigger",
    "delivery.test": "delivery_test",
    "guardian.stop": "guardian_stop",
    "guardian.restart": "guardian_restart",
}


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    method: str


class GuardianDispatchError(RuntimeError):
    """A safe, typed error intentionally returned across the IPC boundary."""

    def __init__(
        self,
        *,
        code: RpcErrorCode,
        kind: RpcErrorKind,
        message: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind
        self.safe_message = message
        self.retryable = retryable

    @classmethod
    def not_implemented(cls, method: str) -> GuardianDispatchError:
        del method
        return cls(
            code=RpcErrorCode.METHOD_NOT_IMPLEMENTED,
            kind="method_not_implemented",
            message="Guardian method is not wired",
            retryable=False,
        )


class GuardianBackend:
    """Override individual methods to connect domain services to Guardian.

    Method implementations must return JSON-compatible values and must never
    return secrets.  Expected operational failures should raise
    :class:`GuardianDispatchError`; unexpected exceptions are converted into a
    low-cardinality internal error by :class:`GuardianApplication`.
    """

    def dispatch(
        self,
        method: str,
        params: dict[str, JsonValue],
        context: RequestContext,
    ) -> JsonValue:
        handler_name = _METHOD_TO_HANDLER.get(method)
        if handler_name is None:
            raise GuardianDispatchError(
                code=RpcErrorCode.METHOD_NOT_FOUND,
                kind="method_not_found",
                message="Guardian method is not allowlisted",
            )
        handler = getattr(self, handler_name)
        return handler(params, context)

    def health_get(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("health.get")

    def cockpit_get(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("cockpit.get")

    def runs_list(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("runs.list")

    def incidents_list(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("incidents.list")

    def providers_list(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("providers.list")

    def config_get(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("config.get")

    def config_validate(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("config.validate")

    def config_apply(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("config.apply")

    def scan_trigger(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("scan.trigger")

    def delivery_test(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("delivery.test")

    def guardian_stop(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("guardian.stop")

    def guardian_restart(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del params, context
        raise GuardianDispatchError.not_implemented("guardian.restart")


class GuardianApplication:
    """Validate, authenticate, dispatch, and sanitize one Guardian request."""

    def __init__(
        self,
        expected_auth_token: str,
        *,
        backend: GuardianBackend | None = None,
        dispatcher: GuardianBackend | None = None,
    ) -> None:
        if backend is not None and dispatcher is not None:
            raise ValueError("provide backend or dispatcher, not both")
        self._expected_auth_token = _validated_token_bytes(expected_auth_token)
        self._backend = backend or dispatcher or GuardianBackend()

    def handle(self, request: RpcRequest) -> RpcResponse:
        request_id = request.request_id
        provided = request.auth_token.get_secret_value().encode("utf-8")
        if not hmac.compare_digest(provided, self._expected_auth_token):
            return error_response(
                request_id,
                code=RpcErrorCode.UNAUTHORIZED,
                kind="unauthorized",
                message="Guardian authentication failed",
            )

        if request.method not in GUARDIAN_METHODS:
            return error_response(
                request_id,
                code=RpcErrorCode.METHOD_NOT_FOUND,
                kind="method_not_found",
                message="Guardian method is not allowlisted",
            )

        context = RequestContext(request_id=request_id, method=request.method)
        try:
            result = self._backend.dispatch(
                request.method,
                dict(request.params),
                context,
            )
            validated = _JSON_VALUE_ADAPTER.validate_python(result, strict=True)
            return success_response(request_id, validated)
        except GuardianDispatchError as exc:
            return error_response(
                request_id,
                code=exc.code,
                kind=exc.kind,
                message=exc.safe_message,
                retryable=exc.retryable,
                method=request.method,
            )
        except ValidationError:
            return error_response(
                request_id,
                code=RpcErrorCode.INVALID_PARAMS,
                kind="invalid_params",
                message="Guardian method parameters are invalid",
                method=request.method,
            )
        except Exception:  # noqa: BLE001 - IPC trust boundary
            return error_response(
                request_id,
                code=RpcErrorCode.INTERNAL_ERROR,
                kind="internal_error",
                message="Guardian request failed",
                retryable=False,
                method=request.method,
            )

    def handle_json(self, payload: bytes) -> bytes:
        try:
            request = parse_request_json(payload)
        except ProtocolViolation as exc:
            request_id = best_effort_request_id(payload)
            if exc.kind == "frame_too_large":
                response = error_response(
                    request_id,
                    code=RpcErrorCode.FRAME_TOO_LARGE,
                    kind="frame_too_large",
                    message="Guardian frame exceeds the size limit",
                    max_frame_bytes=MAX_FRAME_BYTES,
                )
            elif exc.kind == "parse_error":
                response = error_response(
                    request_id,
                    code=RpcErrorCode.PARSE_ERROR,
                    kind="parse_error",
                    message="Guardian request is not valid JSON",
                )
            else:
                response = error_response(
                    request_id,
                    code=RpcErrorCode.INVALID_REQUEST,
                    kind="invalid_request",
                    message="Guardian JSON-RPC request is invalid",
                )
            return serialize_response(response)
        return serialize_response(self.handle(request))


def _validated_token_bytes(value: str) -> bytes:
    if not isinstance(value, str):
        raise TypeError("expected_auth_token must be a string")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("expected_auth_token must be URL-safe ASCII") from exc
    if not 32 <= len(encoded) <= 256:
        raise ValueError("expected_auth_token must be between 32 and 256 bytes")
    allowed = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if any(character not in allowed for character in encoded):
        raise ValueError("expected_auth_token must be URL-safe ASCII")
    return encoded


# Compatibility name for the first IPC vertical slice.  New integrations
# should use ``GuardianBackend``; both names refer to the same overridable
# interface and never imply that a backend is already wired.
GuardianDispatcher = GuardianBackend


__all__ = [
    "GuardianApplication",
    "GuardianBackend",
    "GuardianDispatchError",
    "GuardianDispatcher",
    "RequestContext",
]
