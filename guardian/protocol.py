"""Strict, bounded JSON-RPC protocol used by the local Guardian.

Wire messages use a four-byte unsigned big-endian length followed by UTF-8
JSON.  The length covers only the JSON body.  Both peers enforce the same 1 MiB
limit before allocating or parsing a complete message.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from enum import IntEnum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

JSONRPC_VERSION: Literal["2.0"] = "2.0"
FRAME_HEADER_BYTES = 4
MAX_FRAME_BYTES = 1_048_576

GUARDIAN_METHODS = frozenset(
    {
        "health.get",
        "cockpit.get",
        "runs.list",
        "incidents.list",
        "providers.list",
        "config.get",
        "config.validate",
        "config.apply",
        "scan.trigger",
        "delivery.test",
        "guardian.stop",
        "guardian.restart",
    }
)

RequestId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
MethodName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    ),
]


class _StrictRpcModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_by_alias=True,
        validate_by_name=False,
    )


class RpcRequest(_StrictRpcModel):
    """One authenticated JSON-RPC request.

    JSON-RPC calls the correlation field ``id``.  The Python API names it
    ``request_id`` to make its operational purpose explicit while preserving
    the standard wire shape.
    """

    jsonrpc: Literal["2.0"]
    request_id: RequestId = Field(alias="id")
    method: MethodName
    params: dict[str, JsonValue] = Field(default_factory=dict)
    auth_token: SecretStr = Field(min_length=32, max_length=256, repr=False)


class RpcErrorCode(IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    UNAUTHORIZED = -32001
    METHOD_NOT_IMPLEMENTED = -32002
    SERVICE_UNAVAILABLE = -32003
    FRAME_TOO_LARGE = -32004
    CONFLICT = -32005


RpcErrorKind: TypeAlias = Literal[
    "parse_error",
    "invalid_request",
    "method_not_found",
    "invalid_params",
    "internal_error",
    "unauthorized",
    "method_not_implemented",
    "service_unavailable",
    "frame_too_large",
    "conflict",
]


class RpcErrorData(_StrictRpcModel):
    kind: RpcErrorKind
    retryable: bool = False
    method: MethodName | None = None
    max_frame_bytes: int | None = Field(default=None, ge=1)


class RpcErrorObject(_StrictRpcModel):
    code: RpcErrorCode
    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
    ]
    data: RpcErrorData


class RpcSuccessResponse(_StrictRpcModel):
    jsonrpc: Literal["2.0"]
    request_id: RequestId = Field(alias="id")
    result: JsonValue


class RpcErrorResponse(_StrictRpcModel):
    jsonrpc: Literal["2.0"]
    request_id: RequestId | None = Field(alias="id")
    error: RpcErrorObject


RpcResponse: TypeAlias = RpcSuccessResponse | RpcErrorResponse
_RESPONSE_ADAPTER: TypeAdapter[RpcResponse] = TypeAdapter(RpcResponse)


class ProtocolViolation(ValueError):
    """A peer supplied a malformed or unsafe protocol message."""

    def __init__(self, kind: RpcErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class FrameTooLarge(ProtocolViolation):
    def __init__(self, size: int) -> None:
        super().__init__(
            "frame_too_large",
            f"frame exceeds {MAX_FRAME_BYTES} byte limit",
        )
        self.size = size


class InvalidFrame(ProtocolViolation):
    def __init__(self, message: str = "invalid frame") -> None:
        super().__init__("invalid_request", message)


class FrameDecoder:
    """Incrementally decode bounded length-prefixed frames."""

    def __init__(self, *, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        if not 1 <= max_frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError(
                f"max_frame_bytes must be between 1 and {MAX_FRAME_BYTES}"
            )
        self.max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes | bytearray | memoryview) -> tuple[bytes, ...]:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("frame chunk must be bytes-like")
        self._buffer.extend(chunk)
        frames: list[bytes] = []
        while len(self._buffer) >= FRAME_HEADER_BYTES:
            declared = struct.unpack(">I", self._buffer[:FRAME_HEADER_BYTES])[0]
            if declared == 0:
                self._buffer.clear()
                raise InvalidFrame("zero-length frame is not allowed")
            if declared > self.max_frame_bytes:
                self._buffer.clear()
                raise FrameTooLarge(declared)
            total = FRAME_HEADER_BYTES + declared
            if len(self._buffer) < total:
                break
            frames.append(bytes(self._buffer[FRAME_HEADER_BYTES:total]))
            del self._buffer[:total]
        return tuple(frames)

    def reset(self) -> None:
        self._buffer.clear()


def encode_frame(payload: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("frame payload must be bytes-like")
    body = bytes(payload)
    if not body:
        raise InvalidFrame("zero-length frame is not allowed")
    if len(body) > MAX_FRAME_BYTES:
        raise FrameTooLarge(len(body))
    return struct.pack(">I", len(body)) + body


def parse_request_json(payload: bytes) -> RpcRequest:
    raw = _load_strict_json_object(payload)
    try:
        return RpcRequest.model_validate(raw)
    except ValidationError as exc:
        raise ProtocolViolation("invalid_request", "invalid JSON-RPC request") from exc


def parse_response_json(payload: bytes) -> RpcResponse:
    # First pass rejects duplicate keys and non-standard numeric constants.
    # Validate from JSON again so strict IntEnum fields accept their JSON
    # integer representation without weakening Python-side construction.
    _load_strict_json_object(payload)
    try:
        return _RESPONSE_ADAPTER.validate_json(payload, strict=True)
    except ValidationError as exc:
        raise ProtocolViolation("invalid_request", "invalid JSON-RPC response") from exc


def serialize_request(request: RpcRequest) -> bytes:
    payload: dict[str, JsonValue] = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request.request_id,
        "method": request.method,
        "params": request.params,
        "auth_token": request.auth_token.get_secret_value(),
    }
    return _dump_json(payload)


def serialize_response(response: RpcResponse) -> bytes:
    payload = response.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
    return _dump_json(payload)


def success_response(request_id: str, result: JsonValue) -> RpcSuccessResponse:
    return RpcSuccessResponse(
        jsonrpc=JSONRPC_VERSION,
        id=request_id,
        result=result,
    )


def error_response(
    request_id: str | None,
    *,
    code: RpcErrorCode,
    kind: RpcErrorKind,
    message: str,
    retryable: bool = False,
    method: str | None = None,
    max_frame_bytes: int | None = None,
) -> RpcErrorResponse:
    data_values: dict[str, Any] = {
        "kind": kind,
        "retryable": retryable,
    }
    if method is not None and method in GUARDIAN_METHODS:
        data_values["method"] = method
    if max_frame_bytes is not None:
        data_values["max_frame_bytes"] = max_frame_bytes
    return RpcErrorResponse(
        jsonrpc=JSONRPC_VERSION,
        id=request_id,
        error=RpcErrorObject(
            code=code,
            message=message,
            data=RpcErrorData.model_validate(data_values),
        ),
    )


def best_effort_request_id(payload: bytes) -> str | None:
    """Recover only a syntactically valid request id for an error response."""

    try:
        raw = _load_strict_json_object(payload)
        value = raw.get("id")
        if not isinstance(value, str):
            return None
        # Reuse the strict request-id contract without accepting other fields.
        class _IdModel(_StrictRpcModel):
            request_id: RequestId = Field(alias="id")

        return _IdModel.model_validate({"id": value}).request_id
    except (ProtocolViolation, ValidationError):
        return None


def _load_strict_json_object(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes):
        raise TypeError("JSON payload must be bytes")
    if not payload:
        raise ProtocolViolation("parse_error", "empty JSON payload")
    if len(payload) > MAX_FRAME_BYTES:
        raise FrameTooLarge(len(payload))

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        decoded = payload.decode("utf-8", errors="strict")
        raw = json.loads(
            decoded,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ProtocolViolation("parse_error", "invalid JSON payload") from exc
    if not isinstance(raw, dict):
        raise ProtocolViolation("invalid_request", "JSON-RPC root must be an object")
    return raw


def _dump_json(payload: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolViolation("internal_error", "response is not JSON-safe") from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise FrameTooLarge(len(encoded))
    return encoded


__all__ = [
    "FRAME_HEADER_BYTES",
    "GUARDIAN_METHODS",
    "JSONRPC_VERSION",
    "MAX_FRAME_BYTES",
    "FrameDecoder",
    "FrameTooLarge",
    "InvalidFrame",
    "ProtocolViolation",
    "RpcErrorCode",
    "RpcErrorData",
    "RpcErrorKind",
    "RpcErrorObject",
    "RpcErrorResponse",
    "RpcRequest",
    "RpcResponse",
    "RpcSuccessResponse",
    "best_effort_request_id",
    "encode_frame",
    "error_response",
    "parse_request_json",
    "parse_response_json",
    "serialize_request",
    "serialize_response",
    "success_response",
]
