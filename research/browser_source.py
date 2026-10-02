"""Opt-in reads from a user's Chrome, restricted to a newly created platform tab.

This module never enumerates existing tabs, reads browser cookies, or exposes
JavaScript over IPC. Extractors are trusted application code reading the DOM.
"""
from __future__ import annotations

import http.client
import base64
import hashlib
import inspect
import itertools
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from research.network import ContentAccessError, check_cancelled, validate_public_url

_FAMILIES = (
    ("youtube.com", "youtu.be"), ("bilibili.com",), ("douyin.com",),
    ("xiaohongshu.com", "xhslink.com"),
)
_MAX_MESSAGE_BYTES = 1_000_000
_BROWSER_LOCK = threading.Lock()
_BROWSER_CONNECTION: Any = None
_BROWSER_CONNECTION_MODE: str | None = None
_DEDICATED_PROCESS: subprocess.Popen | None = None
_REQUEST_IDS = itertools.count(1)
_LOGIN_PAGES = {
    "youtube": "https://www.youtube.com/",
    "bilibili": "https://www.bilibili.com/",
    "douyin": "https://www.douyin.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/explore",
}
_CDP_LOGGER = logging.getLogger("alpha_guard.browser_transport")
_CDP_LOGGER.addHandler(logging.NullHandler())
_CDP_LOGGER.propagate = False
_CDP_LOGGER.setLevel(logging.WARNING)
_SNAPSHOT_JS = """(() => ({
  url: location.href, title: document.title.slice(0,500),
  text: document.body.innerText.slice(0,50000),
  links: Array.from(document.querySelectorAll('a[href]'))
    .filter(a => a.getClientRects().length && /^https?:$/.test(a.protocol))
    .slice(0,100).map(a => ({url:a.href.slice(0,4096),text:a.innerText.slice(0,500)}))
}))()"""


def _platform_family(url: str) -> tuple[str, ...]:
    url, _ = validate_public_url(url, resolve=False)
    host = urlsplit(url).hostname or ""
    for family in _FAMILIES:
        if any(host == domain or host.endswith("." + domain) for domain in family):
            return family
    raise ContentAccessError("browser_navigation_rejected")


def _active_port_files() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library/Application Support"
        return [base / folder / "DevToolsActivePort" for folder in (
            "Google/Chrome", "Google/Chrome Canary", "Chromium",
        )]
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData/Local")))
        return [base / folder / "DevToolsActivePort" for folder in (
            "Google/Chrome/User Data", "Chromium/User Data",
        )]
    return [home / ".config" / folder / "DevToolsActivePort"
            for folder in ("google-chrome", "chromium")]


def _configured_mode() -> str:
    from config import get_settings
    return get_settings().research_browser_mode


def dedicated_profile_path() -> Path:
    from platformdirs import user_data_path
    return user_data_path("alpha-guard", appauthor=False) / "browser-profile"


def _read_active_port(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            lines = stream.read(512).decode("ascii").strip().splitlines()
        if (len(lines) == 2 and lines[0].isdigit()
                and 1 <= int(lines[0]) <= 65535
                and re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]{1,150}", lines[1])):
            return f"ws://127.0.0.1:{int(lines[0])}{lines[1]}"
    except (OSError, UnicodeError):
        pass
    return None


def _chrome_executable() -> str | None:
    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Google/Chrome/Application/chrome.exe",
    ]
    executable = next((str(path) for path in candidates if path.is_file()), None)
    return executable or shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


def open_platform_login(platform: str, *, enabled: bool = False,
                        mode: str = "dedicated") -> dict[str, Any]:
    """Open a fixed account homepage without CDP, scanning, or login claims.

    Chrome's normal profile singleton opens the URL in its existing window, or
    starts that dedicated profile. Do not wait for Chrome or a network response
    on the authenticated IPC handler's thread. The user owns this login tab.
    """
    global _DEDICATED_PROCESS
    if type(enabled) is not bool or not enabled:
        raise ContentAccessError("browser_disabled")
    if mode != "dedicated":
        raise ContentAccessError("browser_login_requires_dedicated")
    if not isinstance(platform, str) or platform not in _LOGIN_PAGES:
        raise ContentAccessError("unsupported_login_platform")
    executable = _chrome_executable()
    if executable is None:
        raise ContentAccessError("browser_not_installed")
    profile = dedicated_profile_path()
    try:
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        process = subprocess.Popen([
            executable, f"--user-data-dir={profile}", "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1", "--no-first-run",
            "--no-default-browser-check", _LOGIN_PAGES[platform],
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False, start_new_session=True)
    except OSError:
        raise ContentAccessError("browser_unavailable") from None
    if _DEDICATED_PROCESS is None or _DEDICATED_PROCESS.poll() is not None:
        _DEDICATED_PROCESS = process
    # A second Chrome invocation normally exits after handing its URL to the
    # existing profile. Reap that invocation without delaying the IPC response;
    # a newly opened visible browser stays available for the user's login.
    threading.Thread(target=process.wait, name="research-login-browser", daemon=True).start()
    return {"status": "opening", "platform": platform, "login_verified": False}


def _dedicated_socket_url(timeout: float) -> str:
    global _DEDICATED_PROCESS
    profile = dedicated_profile_path()
    active_port = profile / "DevToolsActivePort"
    existing = _read_active_port(active_port)
    if existing:
        try:
            with socket.create_connection(("127.0.0.1", urlsplit(existing).port), timeout=0.5):
                return existing
        except OSError:
            pass
    executable = _chrome_executable()
    if executable is None:
        raise ContentAccessError("browser_not_installed")
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _DEDICATED_PROCESS is None or _DEDICATED_PROCESS.poll() is not None:
        _DEDICATED_PROCESS = subprocess.Popen([
            executable, f"--user-data-dir={profile}", "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1", "--no-first-run",
            "--no-default-browser-check", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + min(timeout, 12)
    while time.monotonic() < deadline:
        value = _read_active_port(active_port)
        if value and value != existing:
            return value
        if value:
            try:
                with socket.create_connection(("127.0.0.1", urlsplit(value).port), timeout=0.5):
                    return value
            except OSError:
                pass
        time.sleep(0.1)
    raise ContentAccessError("browser_unavailable")


def _browser_socket_url(*, timeout: float = 25) -> str:
    if _configured_mode() == "dedicated":
        return _dedicated_socket_url(timeout)
    for path in _active_port_files():
        value = _read_active_port(path)
        if value:
            return value
    # Explicit --remote-debugging-port installations expose standard discovery.
    connection = http.client.HTTPConnection("127.0.0.1", 9222, timeout=2)
    try:
        connection.request("GET", "/json/version")
        response = connection.getresponse()
        if response.status != 200:
            raise ContentAccessError("browser_unavailable")
        body = response.read(8193)
        if len(body) > 8192:
            raise ContentAccessError("browser_unavailable")
        value = json.loads(body).get("webSocketDebuggerUrl", "")
        parsed = urlsplit(value)
        if (parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.port != 9222 or parsed.username or parsed.password
                or parsed.query or parsed.fragment
                or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]{1,150}", parsed.path)):
            raise ContentAccessError("browser_unavailable")
        return f"ws://127.0.0.1:9222{parsed.path}"
    except (OSError, ValueError, http.client.HTTPException):
        raise ContentAccessError("browser_unavailable") from None
    finally:
        connection.close()


def _connect_browser(timeout: float) -> Any:
    try:
        from websockets.sync.client import connect
        started = time.monotonic()
        address = _browser_socket_url(timeout=timeout)
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise ContentAccessError("browser_timeout")
        options: dict[str, Any] = {
            "open_timeout": min(remaining, 60), "close_timeout": 1,
            "max_size": _MAX_MESSAGE_BYTES, "compression": None,
            "logger": _CDP_LOGGER,
        }
        # websockets 14 has no proxy support; later versions default to env proxy.
        if "proxy" in inspect.signature(connect).parameters:
            options["proxy"] = None
        if "ping_interval" in inspect.signature(connect).parameters:
            options["ping_interval"] = None
        return connect(address, **options)
    except ContentAccessError:
        raise
    except Exception:
        raise ContentAccessError("browser_unavailable") from None


def disconnect_browser() -> bool:
    """Release this process's Chrome session after a worker stops; never its tabs."""
    global _BROWSER_CONNECTION, _BROWSER_CONNECTION_MODE
    if not _BROWSER_LOCK.acquire(blocking=False):
        return False
    try:
        if _BROWSER_CONNECTION is not None:
            try:
                _BROWSER_CONNECTION.close()
            except Exception:
                pass
            _BROWSER_CONNECTION = None
            _BROWSER_CONNECTION_MODE = None
        return True
    finally:
        _BROWSER_LOCK.release()


class BrowserPage:
    """A bounded context owning exactly one tab; enabled=False never connects."""

    def __init__(self, url: str, *, enabled: bool = False,
                 cancelled: Callable[[], bool] | None = None, timeout: float = 25):
        if type(enabled) is not bool or not enabled:
            raise ContentAccessError("browser_disabled")
        if not 1 <= timeout <= 60:
            raise ContentAccessError("browser_invalid_timeout")
        self.url, _ = validate_public_url(url, resolve=False)
        self._family = _platform_family(self.url)
        self._cancelled = cancelled
        self._timeout = timeout
        self._deadline = time.monotonic() + timeout
        self._socket: Any = None
        self._target: str | None = None
        self._session: str | None = None
        self._leased = False
        parsed = urlsplit(self.url)
        self._video_id = (
            parse_qs(parsed.query).get("v", [""])[0]
            if parsed.path == "/watch"
            else parsed.path.split("/")[-1] if parsed.path.startswith("/shorts/") else ""
        )
        self._captions: dict[str, dict[str, Any]] = {}
        self._caption_finished: set[str] = set()
        match = re.search(r"/video/(BV[A-Za-z0-9]+)", parsed.path)
        self._bili_id = match.group(1) if self._family == _FAMILIES[1] and match else ""
        self._bili_players: dict[str, dict[str, str]] = {}
        self._bili_subtitles: dict[str, str] = {}
        self._bili_finished: set[str] = set()
        douyin = re.fullmatch(r"/video/(\d{10,30})/?", parsed.path)
        self._douyin_id = douyin.group(1) if self._family == _FAMILIES[2] and douyin else ""
        self._douyin_responses: set[str] = set()
        self._douyin_finished: set[str] = set()
        self._response_cache: dict[str, str] = {}
        self._response_cache_bytes = 0
        self._media_deadline_extended = False

    def _record_event(self, value: dict[str, Any]) -> None:
        if value.get("sessionId") != self._session:
            return
        params = value.get("params") or {}
        request_id = params.get("requestId")
        if not isinstance(request_id, str):
            return
        if self._bili_id:
            self._record_bilibili_event(value, request_id, params)
        if self._douyin_id:
            self._record_douyin_event(value, request_id, params)
        if not self._video_id:
            return
        if value.get("method") == "Network.loadingFinished" and request_id in self._captions:
            self._caption_finished.add(request_id)
        if value.get("method") != "Network.responseReceived" or len(self._captions) >= 3:
            return
        response = params.get("response") or {}
        try:
            parsed = urlsplit(response.get("url", ""))
            query = parse_qs(parsed.query)
            if (parsed.scheme != "https" or parsed.hostname not in {"www.youtube.com", "youtube.com"}
                    or parsed.path != "/api/timedtext" or response.get("status") != 200
                    or query.get("v", [None])[0] != self._video_id):
                return
        except (ValueError, TypeError):
            return
        self._captions[request_id] = {
            "video_id": self._video_id,
            "language": (query.get("tlang", [""])[0] or query.get("lang", [""])[0])[:50],
            "machine": query.get("kind", [""])[0] == "asr" or bool(query.get("tlang", [""])[0]),
            "mime_type": str(response.get("mimeType", ""))[:100],
        }

    @staticmethod
    def _subtitle_url_hash(url: str) -> str | None:
        try:
            parsed = urlsplit("https:" + url if url.startswith("//") else url)
            if (parsed.scheme != "https" or parsed.username or parsed.password
                    or parsed.port not in {None, 443}
                    or not parsed.path.startswith(("/bfs/subtitle/", "/bfs/ai_subtitle/"))
                    or not any(parsed.hostname == host or (parsed.hostname or "").endswith("." + host)
                               for host in ("hdslb.com", "bilibili.com"))):
                return None
            # The normal player refreshes auth_key before loading its declared
            # subtitle file. Identity is still bound to the exact host/path and
            # every content parameter; authorization is never copied or replayed.
            query = urlencode([(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                               if key != "auth_key"])
            normalized = urlunsplit(("https", parsed.netloc, parsed.path, query, ""))
            return hashlib.sha256(normalized.encode()).hexdigest()
        except (ValueError, TypeError):
            return None

    def _record_bilibili_event(self, value: dict[str, Any], request_id: str,
                               params: dict[str, Any]) -> None:
        if value.get("method") == "Network.loadingFinished":
            if request_id in self._bili_players or request_id in self._bili_subtitles:
                self._bili_finished.add(request_id)
            return
        if value.get("method") != "Network.responseReceived":
            return
        response = params.get("response") or {}
        if response.get("status") != 200:
            return
        try:
            parsed = urlsplit(response.get("url", ""))
            query = parse_qs(parsed.query)
            if (parsed.scheme == "https" and parsed.hostname == "api.bilibili.com"
                    and parsed.path in {"/x/player/v2", "/x/player/wbi/v2"}
                    and len(self._bili_players) < 6):
                aid, cid = query.get("aid", [""])[0], query.get("cid", [""])[0]
                bvid = query.get("bvid", [""])[0]
                if aid.isdigit() and cid.isdigit() and (not bvid or bvid == self._bili_id):
                    self._bili_players[request_id] = {"aid": aid[:30], "cid": cid[:30]}
            elif len(self._bili_subtitles) < 12:
                fingerprint = self._subtitle_url_hash(response.get("url", ""))
                if fingerprint:
                    self._bili_subtitles[request_id] = fingerprint
        except (ValueError, TypeError):
            return

    def _remaining(self) -> float:
        check_cancelled(self._cancelled)
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise ContentAccessError("browser_timeout")
        return remaining

    def _record_douyin_event(self, value: dict[str, Any], request_id: str,
                             params: dict[str, Any]) -> None:
        if value.get("method") == "Network.loadingFinished":
            if request_id in self._douyin_responses:
                self._douyin_finished.add(request_id)
            return
        if value.get("method") != "Network.responseReceived" or len(self._douyin_responses) >= 4:
            return
        response = params.get("response") or {}
        try:
            parsed = urlsplit(response.get("url", ""))
            if (response.get("status") == 200 and parsed.scheme == "https"
                    and parsed.hostname == "www.douyin.com" and not parsed.username
                    and parsed.port in {None, 443}
                    and parsed.path == "/aweme/v1/web/aweme/detail/"
                    and parse_qs(parsed.query).get("aweme_id", [""])[0] == self._douyin_id):
                self._douyin_responses.add(request_id)
        except (ValueError, TypeError):
            return

    def _command(self, method: str, params: dict[str, Any] | None = None,
                 *, session: bool = True, cleanup: bool = False) -> dict[str, Any]:
        deadline = time.monotonic() + 1 if cleanup else self._deadline
        if not cleanup:
            self._remaining()
        identifier = next(_REQUEST_IDS)
        message: dict[str, Any] = {"id": identifier, "method": method, "params": params or {}}
        if session and self._session:
            message["sessionId"] = self._session
        try:
            self._socket.send(json.dumps(message))
            while True:
                if not cleanup:
                    check_cancelled(self._cancelled)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ContentAccessError("browser_timeout")
                try:
                    raw = self._socket.recv(timeout=min(0.25, remaining))
                except TimeoutError:
                    continue
                if len(raw) > _MAX_MESSAGE_BYTES:
                    raise ContentAccessError("browser_response_too_large")
                value = json.loads(raw)
                if value.get("id") != identifier:
                    self._record_event(value)
                    continue
                if "error" in value or not isinstance(value.get("result"), dict):
                    raise ContentAccessError("browser_protocol_error")
                return value["result"]
        except ContentAccessError:
            raise
        except Exception:
            raise ContentAccessError("browser_unavailable") from None

    def _runtime_value(self, expression: str) -> Any:
        result = self._command("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
            "timeout": max(1, int(self._remaining() * 1000)),
        })
        if "exceptionDetails" in result:
            raise ContentAccessError("browser_page_error")
        return result.get("result", {}).get("value")

    def _assert_origin(self) -> None:
        current = self._runtime_value("location.href")
        if not isinstance(current, str) or _platform_family(current) != self._family:
            raise ContentAccessError("browser_navigation_rejected")

    def __enter__(self) -> BrowserPage:
        global _BROWSER_CONNECTION, _BROWSER_CONNECTION_MODE
        try:
            while not _BROWSER_LOCK.acquire(timeout=min(0.1, self._remaining())):
                self._remaining()
            self._leased = True
            mode = _configured_mode()
            if _BROWSER_CONNECTION is not None and _BROWSER_CONNECTION_MODE != mode:
                _BROWSER_CONNECTION.close()
                _BROWSER_CONNECTION = None
            if _BROWSER_CONNECTION is None:
                _BROWSER_CONNECTION = _connect_browser(self._remaining())
                _BROWSER_CONNECTION_MODE = mode
            self._socket = _BROWSER_CONNECTION
            result = self._command("Target.createTarget", {"url": "about:blank", "background": True}, session=False)
            self._target = result.get("targetId")
            if not isinstance(self._target, str) or not self._target:
                raise ContentAccessError("browser_protocol_error")
            attached = self._command("Target.attachToTarget", {"targetId": self._target, "flatten": True}, session=False)
            self._session = attached.get("sessionId")
            if not isinstance(self._session, str) or not self._session:
                raise ContentAccessError("browser_protocol_error")
            if mode == "dedicated":
                self._command("Page.bringToFront")
            # Mute this owned tab's media from the first DOM insertion. Never
            # change the user's other tabs, system volume, or browser policy.
            self._command("Page.addScriptToEvaluateOnNewDocument", {"source": """
                (()=>{const mute=()=>document.querySelectorAll('video,audio').forEach(v=>{
                  v.muted=true;v.defaultMuted=true;
                });new MutationObserver(mute).observe(document,{childList:true,subtree:true});
                document.addEventListener('play',mute,true);mute();})();
            """})
            if (self._family == _FAMILIES[0] and self._video_id) or self._bili_id or self._douyin_id:
                self._command("Network.enable", {"maxTotalBufferSize": 3_000_000,
                                                 "maxResourceBufferSize": 1_000_000})
            navigation = self._command("Page.navigate", {"url": self.url})
            if navigation.get("errorText"):
                raise ContentAccessError("browser_page_error")
            while True:
                state = self._runtime_value("({url:location.href,ready:document.readyState})")
                if isinstance(state, dict) and state.get("url") != "about:blank":
                    if _platform_family(state.get("url", "")) != self._family:
                        raise ContentAccessError("browser_navigation_rejected")
                    if state.get("ready") in {"interactive", "complete"}:
                        break
                self._remaining()
                time.sleep(0.1)
            return self
        except ContentAccessError:
            self.close()
            raise
        except Exception:
            self.close()
            raise ContentAccessError("browser_unavailable") from None
        except BaseException:
            self.close()
            raise

    def evaluate(self, expression: str) -> Any:
        """Run an internal fixed DOM extractor, never user-provided JavaScript."""
        if not isinstance(expression, str) or len(expression) > 50_000:
            raise ContentAccessError("browser_invalid_extractor")
        self._assert_origin()
        value = self._runtime_value(expression)
        self._assert_origin()
        return value

    def snapshot(self) -> dict[str, Any]:
        value = self.evaluate(_SNAPSHOT_JS)
        if not isinstance(value, dict):
            raise ContentAccessError("browser_page_error")
        return value

    def bring_to_front(self) -> bool:
        """Allow normal visibility only in the application's dedicated window."""
        if _BROWSER_CONNECTION_MODE != "dedicated":
            return False
        self._assert_origin()
        self._command("Page.bringToFront")
        return True

    def caption_responses(self) -> list[dict[str, Any]]:
        """Read only this video's captions already fetched by the visible player.

        URLs, authorization query parameters, headers and cookies never leave
        the transport. No subtitle request is replayed or modified.
        """
        self._assert_origin()
        responses = []
        for request_id in tuple(self._caption_finished):
            body = self._response_body(request_id)
            if body:
                responses.append({**self._captions[request_id], "body": body})
        self._assert_origin()
        return responses

    def douyin_video_metadata(self) -> dict[str, Any] | None:
        """Only public identity fields from the owned page's native response.

        No platform request is generated or replayed. Media URLs, signatures,
        headers and account details never leave this response adapter.
        """
        self._assert_origin()
        if not self._douyin_id:
            return None
        for request_id in tuple(self._douyin_finished):
            body = self._response_body(request_id)
            if not body:
                continue
            try:
                payload = json.loads(body)
                detail = payload.get("aweme_detail") or {}
                author = detail.get("author") or {}
                video = detail.get("video") or {}
                if (payload.get("status_code") != 0
                        or str(detail.get("aweme_id")) != self._douyin_id
                        or not isinstance(author.get("sec_uid"), str)):
                    continue
                result = {"video_id": self._douyin_id,
                    "author_id": author["sec_uid"][:300],
                    "author": str(author.get("nickname") or "")[:300],
                    "description": str(detail.get("desc") or "")[:10000],
                    "is_ads": detail.get("is_ads") if type(detail.get("is_ads")) is bool else None,
                    "published_timestamp": detail.get("create_time") if type(detail.get("create_time")) is int else None,
                    "duration_milliseconds": video.get("duration") if type(video.get("duration")) is int else None}
                self._assert_origin()
                return result
            except (TypeError, ValueError, AttributeError):
                continue
        return None

    def _response_body(self, request_id: str) -> str:
        if request_id in self._response_cache:
            return self._response_cache[request_id]
        try:
            result = self._command("Network.getResponseBody", {"requestId": request_id})
        except ContentAccessError as exc:
            if str(exc) != "browser_protocol_error":
                raise
            # Chrome can evict a completed resource as video segments arrive.
            # Other tracks still may be readable; never abort their extraction.
            self._response_cache[request_id] = ""
            return ""
        body = result.get("body", "")
        if not isinstance(body, str) or len(body) > _MAX_MESSAGE_BYTES:
            return ""
        if result.get("base64Encoded"):
            try:
                body = base64.b64decode(body, validate=True).decode("utf-8")
            except (ValueError, UnicodeError):
                return ""
        size = len(body.encode("utf-8"))
        if self._response_cache_bytes + size > 3_000_000:
            return ""
        self._response_cache[request_id] = body
        self._response_cache_bytes += size
        return body

    def bilibili_caption_responses(self, *, bvid: str, aid: str | int,
                                   cid: str | int) -> list[dict[str, Any]]:
        """Read captions only when this video's player explicitly binds the track.

        Track URLs remain hashed within this tab. Only bodies that the ordinary
        player already loaded are read; no API or signed URL is replayed.
        """
        self._assert_origin()
        if not self._bili_id or bvid != self._bili_id or not str(aid).isdigit() or not str(cid).isdigit():
            raise ContentAccessError("browser_caption_identity_mismatch")
        tracks: dict[str, dict[str, Any]] = {}
        login_required = False
        for request_id, identity in tuple(self._bili_players.items()):
            if (identity != {"aid": str(aid), "cid": str(cid)}
                    or request_id not in self._bili_finished):
                continue
            try:
                payload = json.loads(self._response_body(request_id))
            except (ValueError, UnicodeError):
                continue
            if not isinstance(payload, dict) or payload.get("code") != 0:
                continue
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                continue
            login_required |= data.get("need_login_subtitle") is True
            subtitle = data.get("subtitle") or {}
            if not isinstance(subtitle, dict) or not isinstance(subtitle.get("subtitles"), list):
                continue
            for track in subtitle["subtitles"][:20]:
                if not isinstance(track, dict) or not isinstance(track.get("subtitle_url"), str):
                    continue
                fingerprint = self._subtitle_url_hash(track["subtitle_url"])
                if not fingerprint:
                    continue
                language = str(track.get("lan", ""))[:50]
                tracks[fingerprint] = {"video_id": bvid, "segment_id": int(cid),
                    "language": language, "machine": language.startswith("ai-")
                    or track.get("ai_type") in (1, 2) or "自动" in str(track.get("lan_doc", ""))}
        if login_required and not tracks:
            raise ContentAccessError("browser_login_required")
        responses = []
        for request_id, fingerprint in tuple(self._bili_subtitles.items()):
            if request_id in self._bili_finished and fingerprint in tracks:
                body = self._response_body(request_id)
                if body:
                    responses.append({**tracks[fingerprint], "body": body})
                    if len(responses) == 3:
                        break
        self._assert_origin()
        return responses

    def click(self, selector: str) -> bool:
        return self._pointer_action(selector, click=True)

    def hover(self, selector: str) -> bool:
        """Move over one visible owned-page control without activating it."""
        return self._pointer_action(selector, click=False)

    def _pointer_action(self, selector: str, *, click: bool) -> bool:
        if not isinstance(selector, str) or len(selector) > 1000:
            raise ContentAccessError("browser_invalid_selector")
        expression = """(() => { const e=Array.from(document.querySelectorAll(SELECTOR))
          .find(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
          if (!e) return null; const initial=e.getBoundingClientRect();
          if(initial.top<0 || initial.left<0 || initial.bottom>innerHeight || initial.right>innerWidth)
            e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
          const r=e.getBoundingClientRect(), x=r.left+r.width/2, y=r.top+r.height/2;
          const hit=document.elementFromPoint(x,y);
          if (r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight
            || !hit || !(e===hit || e.contains(hit))) return null;
          return {x,y}; })()""".replace("SELECTOR", json.dumps(selector))
        point = self.evaluate(expression)
        if not isinstance(point, dict) or not all(type(point.get(key)) in {int, float} for key in ("x", "y")):
            return False
        for kind in (("mouseMoved", "mousePressed", "mouseReleased") if click else ("mouseMoved",)):
            self._command("Input.dispatchMouseEvent", {"type": kind, "x": point["x"], "y": point["y"],
                          "button": "left", "clickCount": 1})
        self._assert_origin()
        return True

    def wait_for(self, expression: str, *, timeout: float = 8) -> Any:
        deadline = min(self._deadline, time.monotonic() + max(0, timeout))
        while True:
            value = self.evaluate(expression)
            if value:
                return value
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))

    def extend_for_media_seconds(self, seconds: float) -> None:
        """One bounded extension for a validated, naturally playing audio track."""
        self._remaining()
        if (type(seconds) not in {int, float} or not 1 <= seconds <= 630
                or self._media_deadline_extended or self._target is None or self._session is None):
            raise ContentAccessError("browser_invalid_media_budget")
        self._media_deadline_extended = True
        self._deadline = max(self._deadline, time.monotonic() + seconds)

    def close(self) -> None:
        global _BROWSER_CONNECTION
        if self._socket is not None:
            try:
                if self._target is not None:
                    self._command("Target.closeTarget", {"targetId": self._target}, session=False, cleanup=True)
            except Exception:
                try:
                    self._socket.close()
                except Exception:
                    pass
                if _BROWSER_CONNECTION is self._socket:
                    _BROWSER_CONNECTION = None
            finally:
                self._socket = None
                self._target = None
                self._session = None
        if self._leased:
            self._leased = False
            _BROWSER_LOCK.release()

    def __exit__(self, *_exc: object) -> None:
        self.close()
