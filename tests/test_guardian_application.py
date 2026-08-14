from __future__ import annotations

import json

import pytest

from guardian.application import (
    GuardianApplication,
    GuardianDispatchError,
    GuardianDispatcher,
)
from guardian.protocol import (
    GUARDIAN_METHODS,
    RpcErrorCode,
    RpcErrorResponse,
    RpcRequest,
    RpcSuccessResponse,
    parse_response_json,
)

TOKEN = "a" * 43


def _request(*, method: str = "health.get", token: str = TOKEN) -> RpcRequest:
    return RpcRequest.model_validate(
        {
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": method,
            "params": {},
            "auth_token": token,
        }
    )


class _WorkingDispatcher(GuardianDispatcher):
    def health_get(self, params, context):
        assert params == {}
        return {
            "status": "ready",
            "request_id": context.request_id,
        }


class _FailingDispatcher(GuardianDispatcher):
    def health_get(self, params, context):
        del params, context
        raise RuntimeError("secret backend detail token=do-not-return")


class _UnavailableDispatcher(GuardianDispatcher):
    def health_get(self, params, context):
        del params, context
        raise GuardianDispatchError(
            code=RpcErrorCode.SERVICE_UNAVAILABLE,
            kind="service_unavailable",
            message="Guardian is starting",
            retryable=True,
        )


def test_application_authenticates_and_preserves_request_id() -> None:
    response = GuardianApplication(
        TOKEN,
        dispatcher=_WorkingDispatcher(),
    ).handle(_request())

    assert isinstance(response, RpcSuccessResponse)
    assert response.request_id == "req-1"
    assert response.result == {"status": "ready", "request_id": "req-1"}


def test_authentication_uses_typed_unauthorized_error() -> None:
    response = GuardianApplication(TOKEN).handle(_request(token="b" * 43))

    assert isinstance(response, RpcErrorResponse)
    assert response.error.code is RpcErrorCode.UNAUTHORIZED
    assert response.error.data.kind == "unauthorized"
    assert response.error.data.method is None


def test_unknown_method_is_rejected_after_authentication() -> None:
    response = GuardianApplication(TOKEN).handle(_request(method="unknown.get"))

    assert isinstance(response, RpcErrorResponse)
    assert response.error.code is RpcErrorCode.METHOD_NOT_FOUND
    assert response.error.data.kind == "method_not_found"


@pytest.mark.parametrize("method", sorted(GUARDIAN_METHODS))
def test_every_unwired_allowlisted_method_returns_typed_error(method: str) -> None:
    response = GuardianApplication(TOKEN).handle(_request(method=method))

    assert isinstance(response, RpcErrorResponse)
    assert response.error.code is RpcErrorCode.METHOD_NOT_IMPLEMENTED
    assert response.error.data.kind == "method_not_implemented"
    assert response.error.data.method == method


def test_expected_operational_error_preserves_retryability() -> None:
    response = GuardianApplication(
        TOKEN,
        dispatcher=_UnavailableDispatcher(),
    ).handle(_request())

    assert isinstance(response, RpcErrorResponse)
    assert response.error.code is RpcErrorCode.SERVICE_UNAVAILABLE
    assert response.error.data.retryable is True


def test_unexpected_error_is_sanitized() -> None:
    response = GuardianApplication(
        TOKEN,
        dispatcher=_FailingDispatcher(),
    ).handle(_request())

    assert isinstance(response, RpcErrorResponse)
    rendered = response.model_dump_json()
    assert response.error.code is RpcErrorCode.INTERNAL_ERROR
    assert "do-not-return" not in rendered
    assert "RuntimeError" not in rendered


def test_invalid_request_returns_typed_error_and_best_effort_id() -> None:
    malformed = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "req-parse",
            "method": "health.get",
            "params": {},
        }
    ).encode()

    response = parse_response_json(GuardianApplication(TOKEN).handle_json(malformed))

    assert isinstance(response, RpcErrorResponse)
    assert response.request_id == "req-parse"
    assert response.error.code is RpcErrorCode.INVALID_REQUEST


def test_expected_token_contract_rejects_short_or_non_ascii_values() -> None:
    with pytest.raises(ValueError):
        GuardianApplication("short")
    with pytest.raises(ValueError):
        GuardianApplication("密" * 43)
