"""Bounded public content reads with DNS pinning and redirect validation."""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
import threading
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit


class ContentAccessError(ValueError):
    """A low-cardinality, safe-to-display content access failure."""


@dataclass(frozen=True)
class FetchedContent:
    url: str
    text: str
    content_type: str = "text/plain"
    status_code: int = 200


def check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ContentAccessError("cancelled")


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_global and not address.is_multicast and not address.is_reserved


def _resolve(hostname: str, port: int, timeout: float) -> list[str]:
    # libc DNS has no per-call timeout API. A daemon resolver cannot block the
    # user worker indefinitely; timed-out DNS results are never connected to.
    result: list[Any] = []
    def run():
        try:
            result.extend(socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM))
        except (OSError, TypeError, ValueError):
            pass
    worker = threading.Thread(target=run, name="research-dns", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise ContentAccessError("request_timeout")
    return sorted({str(record[4][0]) for record in result})


def validate_public_url(url: str, *, resolve: bool = True,
                        dns_timeout: float = 5) -> tuple[str, list[str]]:
    """Validate URLs at entry and each redirect; never return credential URLs."""
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise ContentAccessError("invalid_url")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        hostname = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not hostname
                or parsed.username is not None or parsed.password is not None
                or "\\" in url or "%" in hostname):
            raise ContentAccessError("invalid_url")
        expected_port = 443 if parsed.scheme == "https" else 80
        if port not in {None, expected_port}:
            raise ContentAccessError("port_not_allowed")
        hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
            raise ContentAccessError("non_public_address")
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is not None and not _public_address(str(literal)):
            raise ContentAccessError("non_public_address")
        addresses = []
        if resolve:
            addresses = _resolve(hostname, expected_port, dns_timeout)
            if not addresses or any(not _public_address(ip) for ip in addresses):
                raise ContentAccessError("non_public_address")
        authority = f"[{hostname}]" if ":" in hostname else hostname
        canonical = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
        return canonical, addresses
    except ContentAccessError:
        raise
    except (ValueError, UnicodeError, OSError):
        raise ContentAccessError("invalid_or_unresolvable_url") from None


DEFAULT_USER_AGENT = "AlphaGuard/0.3 research-reader"
# SEC Fair Access policy: www.sec.gov original documents are served only to a
# User-Agent of the shape "Sample Company Name AdminContact@example.com" —
# name-only, URL-only and browser UAs are rejected with 403 (verified live
# 2026-10-21). Build that shape for every sec.gov hop, redirects included.
SEC_USER_AGENT_PREFIX = "AlphaGuard Research Reader"


def _user_agent_for(hostname: str) -> str:
    host = hostname.rstrip(".").lower()
    if host == "sec.gov" or host.endswith(".sec.gov"):
        from config import get_settings

        contact = get_settings().research_sec_contact.strip()
        if contact:
            return f"{SEC_USER_AGENT_PREFIX} {contact}"
    return DEFAULT_USER_AGENT


def safe_fetch(
    url: str, *, timeout: float = 10, max_bytes: int = 2_000_000,
    cancelled: Callable[[], bool] | None = None,
) -> FetchedContent:
    """Read text without proxy/cookies, pinning the connection to vetted IPs.

    Redirects cannot switch to a private host. HTTPS still validates the original
    hostname and uses it for SNI. No credentials or exception URLs are surfaced.
    """
    if not 0 < timeout <= 60 or not 1 <= max_bytes <= 10_000_000:
        raise ValueError("invalid_fetch_limits")
    deadline = time.monotonic() + timeout
    current = url
    for _ in range(4):
        check_cancelled(cancelled)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ContentAccessError("request_timeout")
        current, addresses = validate_public_url(current, dns_timeout=remaining)
        parsed = urlsplit(current)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ContentAccessError("request_timeout")
        host = parsed.hostname or ""
        port = 443 if parsed.scheme == "https" else 80
        connection = (http.client.HTTPSConnection(host, timeout=remaining,
                                                 context=ssl.create_default_context())
                      if parsed.scheme == "https" else
                      http.client.HTTPConnection(host, timeout=remaining))
        # HTTPConnection normally performs its own DNS lookup. Override only the
        # socket factory so DNS cannot be rebound between validation and connect.
        def pinned_socket(_address, timeout=remaining, source_address=None):
            return socket.create_connection((addresses[0], port), timeout, source_address)
        setattr(connection, "_create_connection", pinned_socket)
        timed_out = threading.Event()
        active_response: list[Any] = []
        def abort_overdue_request():
            timed_out.set()
            current_socket = connection.sock
            if current_socket is None and active_response:
                fp = getattr(active_response[0], "fp", None)
                current_socket = getattr(getattr(fp, "raw", None), "_sock", None)
            if current_socket is not None:
                try:
                    current_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        watchdog = threading.Timer(remaining, abort_overdue_request)
        watchdog.daemon = True
        watchdog.start()
        try:
            check_cancelled(cancelled)
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request("GET", path, headers={
                "User-Agent": _user_agent_for(host),
                "Accept": "text/*,application/json,application/xml,application/rss+xml,application/atom+xml",
                "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            active_response.append(response)
            if timed_out.is_set():
                raise ContentAccessError("request_timeout")
            if response.status in {301, 302, 303, 307, 308}:
                target = response.getheader("Location")
                if not target:
                    raise ContentAccessError("invalid_redirect")
                current = urljoin(current, target)
                continue
            if response.status in {401, 403}:
                raise ContentAccessError("permission_required")
            if response.status == 429:
                raise ContentAccessError("rate_limited")
            if response.status < 200 or response.status >= 300:
                raise ContentAccessError("http_error")
            if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise ContentAccessError("unsupported_encoding")
            length = response.getheader("Content-Length")
            if length and int(length) > max_bytes:
                raise ContentAccessError("content_too_large")
            chunks: list[bytes] = []
            size = 0
            while True:
                check_cancelled(cancelled)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ContentAccessError("request_timeout")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ContentAccessError("content_too_large")
                chunks.append(chunk)
            if timed_out.is_set():
                raise ContentAccessError("request_timeout")
            content_type = response.getheader("Content-Type", "text/plain")
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                text = b"".join(chunks).decode(charset, errors="replace")
            except LookupError:
                text = b"".join(chunks).decode("utf-8", errors="replace")
            return FetchedContent(current, text, content_type, response.status)
        except ContentAccessError:
            raise
        except (TimeoutError, socket.timeout):
            raise ContentAccessError("request_timeout") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise ContentAccessError("request_timeout" if timed_out.is_set() else "request_failed") from None
        finally:
            watchdog.cancel()
            connection.close()
    raise ContentAccessError("too_many_redirects")
