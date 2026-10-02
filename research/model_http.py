"""One bounded, authenticated POST to a user-configured model endpoint.

Requests never follow redirects, pin the connection to vetted addresses and
map every failure to a low-cardinality code. Private networks are refused
unless the profile explicitly allows them for local inference.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from .network import ContentAccessError, check_cancelled, _public_address, _resolve

MAX_REQUEST_BYTES = 200_000
MAX_RESPONSE_BYTES = 2_000_000


def _private_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_loopback or address.is_private or address.is_link_local


def resolve_endpoint_addresses(url: str, *, allow_private: bool,
                               dns_timeout: float = 5.0) -> tuple[list[str], int, str, str]:
    """Validate the endpoint and return pinned addresses plus request target."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ContentAccessError("invalid_model_endpoint") from None
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (parsed.scheme not in {"http", "https"} or not hostname or parsed.username is not None
            or parsed.password is not None):
        raise ContentAccessError("invalid_model_endpoint")
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise ContentAccessError("invalid_model_endpoint")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise ContentAccessError("invalid_model_endpoint") from None
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        addresses = _resolve(hostname, port, dns_timeout)
    if not addresses:
        raise ContentAccessError("non_public_address")
    if not allow_private and any(not _public_address(value) for value in addresses):
        raise ContentAccessError("non_public_address")
    if allow_private and any(not (_public_address(value) or _private_address(value)) for value in addresses):
        raise ContentAccessError("invalid_model_endpoint")
    target = parsed.path or "/"
    return addresses, port, hostname, target


def model_post_json(*, endpoint: str, path: str, payload: dict[str, Any],
                    api_key: str | None, protocol: str, timeout_seconds: float,
                    allow_private: bool, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Send one JSON POST and return the parsed JSON response."""
    if not 5 <= timeout_seconds <= 600:
        raise ContentAccessError("invalid_model_timeout")
    url = endpoint.rstrip("/") + path
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ContentAccessError("request_too_large")
    addresses, port, hostname, target = resolve_endpoint_addresses(url, allow_private=allow_private)
    check_cancelled(cancelled)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "AlphaGuard/0.3 research-model",
        "Content-Length": str(len(body)),
    }
    if api_key:
        if protocol == "anthropic_compatible":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    deadline = time.monotonic() + timeout_seconds
    connection: http.client.HTTPConnection
    if urlsplit(url).scheme == "https":
        connection = http.client.HTTPSConnection(hostname, timeout=timeout_seconds,
                                                 context=ssl.create_default_context())
    else:
        connection = http.client.HTTPConnection(hostname, timeout=timeout_seconds)
    watchdog_triggered = threading.Event()

    def pinned_socket(_address, timeout=timeout_seconds, source_address=None):
        return socket.create_connection((addresses[0], port), timeout, source_address)

    setattr(connection, "_create_connection", pinned_socket)

    def abort_overdue_request() -> None:
        watchdog_triggered.set()
        current_socket = connection.sock
        if current_socket is not None:
            try:
                current_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    watchdog = threading.Timer(timeout_seconds, abort_overdue_request)
    watchdog.daemon = True
    watchdog.start()
    try:
        check_cancelled(cancelled)
        connection.request("POST", target, body=body, headers=headers)
        response = connection.getresponse()
        if watchdog_triggered.is_set():
            raise ContentAccessError("request_timeout")
        if 300 <= response.status < 400:
            raise ContentAccessError("redirect_not_allowed")
        if response.status in {401, 403}:
            raise ContentAccessError("provider_rejected")
        if response.status == 429:
            raise ContentAccessError("rate_limited")
        if response.status < 200 or response.status >= 300:
            raise ContentAccessError("http_error")
        length = response.getheader("Content-Length")
        if length and int(length) > MAX_RESPONSE_BYTES:
            raise ContentAccessError("content_too_large")
        chunks: list[bytes] = []
        size = 0
        while True:
            check_cancelled(cancelled)
            if time.monotonic() >= deadline or watchdog_triggered.is_set():
                raise ContentAccessError("request_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(max(0.1, deadline - time.monotonic()))
            chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ContentAccessError("content_too_large")
            chunks.append(chunk)
        if watchdog_triggered.is_set():
            raise ContentAccessError("request_timeout")
        try:
            result = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ContentAccessError("invalid_model_response") from None
        if not isinstance(result, dict):
            raise ContentAccessError("invalid_model_response")
        return result
    except ContentAccessError:
        raise
    except (TimeoutError, socket.timeout):
        raise ContentAccessError("request_timeout") from None
    except (OSError, http.client.HTTPException):
        raise ContentAccessError("request_timeout" if watchdog_triggered.is_set() else "request_failed") from None
    finally:
        watchdog.cancel()
        connection.close()
