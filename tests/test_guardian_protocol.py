from __future__ import annotations

import struct

import pytest

from guardian.protocol import (
    MAX_FRAME_BYTES,
    FrameDecoder,
    FrameTooLarge,
    InvalidFrame,
    ProtocolViolation,
    RpcErrorCode,
    RpcErrorResponse,
    RpcRequest,
    encode_frame,
    error_response,
    parse_request_json,
    parse_response_json,
    serialize_request,
    serialize_response,
)

TOKEN = "a" * 43


def _request(**overrides: object) -> RpcRequest:
    values = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "health.get",
        "params": {},
        "auth_token": TOKEN,
    }
    values.update(overrides)
    return RpcRequest.model_validate(values)


def test_request_round_trip_uses_standard_id_and_masks_token_repr() -> None:
    request = _request(params={"nested": [True, None, 3]})

    rendered = serialize_request(request)
    parsed = parse_request_json(rendered)

    assert parsed == request
    assert b'"id":"req-1"' in rendered
    assert b'"request_id"' not in rendered
    assert TOKEN not in repr(request)


def test_request_wire_shape_is_strict_and_rejects_extra_or_named_alias() -> None:
    with pytest.raises(ProtocolViolation, match="invalid JSON-RPC request"):
        parse_request_json(
            (
                '{"jsonrpc":"2.0","request_id":"req-1",'
                '"method":"health.get","params":{},"auth_token":"'
                + TOKEN
                + '"}'
            ).encode()
        )

    with pytest.raises(ProtocolViolation, match="invalid JSON-RPC request"):
        parse_request_json(
            (
                '{"jsonrpc":"2.0","id":"req-1","method":"health.get",'
                '"params":{},"auth_token":"'
                + TOKEN
                + '","unexpected":true}'
            ).encode()
        )


def test_strict_json_rejects_duplicate_keys_and_nonstandard_numbers() -> None:
    duplicate = (
        '{"jsonrpc":"2.0","id":"req-1","id":"req-2",'
        '"method":"health.get","params":{},"auth_token":"'
        + TOKEN
        + '"}'
    ).encode()
    with pytest.raises(ProtocolViolation) as duplicate_error:
        parse_request_json(duplicate)
    assert duplicate_error.value.kind == "parse_error"

    nonstandard = (
        '{"jsonrpc":"2.0","id":"req-1","method":"health.get",'
        '"params":{"value":NaN},"auth_token":"'
        + TOKEN
        + '"}'
    ).encode()
    with pytest.raises(ProtocolViolation) as number_error:
        parse_request_json(nonstandard)
    assert number_error.value.kind == "parse_error"


def test_frame_decoder_supports_fragmentation_and_multiple_frames() -> None:
    first = serialize_request(_request(id="req-1"))
    second = serialize_request(_request(id="req-2"))
    wire = encode_frame(first) + encode_frame(second)
    decoder = FrameDecoder()

    assert decoder.feed(wire[:3]) == ()
    assert decoder.feed(wire[3:11]) == ()
    frames = decoder.feed(wire[11:])

    assert frames == (first, second)
    assert decoder.buffered_bytes == 0


def test_frame_limit_is_enforced_from_header_before_body_arrives() -> None:
    decoder = FrameDecoder()
    header = struct.pack(">I", MAX_FRAME_BYTES + 1)

    with pytest.raises(FrameTooLarge) as exc_info:
        decoder.feed(header)

    assert exc_info.value.size == MAX_FRAME_BYTES + 1
    assert decoder.buffered_bytes == 0

    with pytest.raises(FrameTooLarge):
        encode_frame(b"x" * (MAX_FRAME_BYTES + 1))


def test_zero_length_frame_is_rejected() -> None:
    with pytest.raises(InvalidFrame):
        FrameDecoder().feed(b"\x00\x00\x00\x00")
    with pytest.raises(InvalidFrame):
        encode_frame(b"")


def test_typed_error_response_round_trip() -> None:
    response = error_response(
        "req-1",
        code=RpcErrorCode.METHOD_NOT_IMPLEMENTED,
        kind="method_not_implemented",
        message="Guardian method is not wired",
        method="health.get",
    )

    parsed = parse_response_json(serialize_response(response))

    assert isinstance(parsed, RpcErrorResponse)
    assert parsed.request_id == "req-1"
    assert parsed.error.code is RpcErrorCode.METHOD_NOT_IMPLEMENTED
    assert parsed.error.data.kind == "method_not_implemented"
    assert parsed.error.data.method == "health.get"
