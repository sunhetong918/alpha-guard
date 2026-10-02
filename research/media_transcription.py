"""Bound a complete, already-local source recording to offline transcription.

This module never downloads media or starts discovery. A trusted capture adapter
must supply both the source binding and a verified end-of-playback signal.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, Sequence
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, TypeGuard
from urllib.parse import parse_qs, urlsplit

from .ingest import _stop_process, transcribe_media
from .models import TranscriptSegment
from .network import ContentAccessError, check_cancelled, validate_public_url

MAX_MEDIA_BYTES = 2_000_000_000
MAX_MEDIA_SECONDS = 10_800
_SAMPLE_RATE = 16_000
_CACHE_VERSION = "source-asr-v1"


@dataclass(frozen=True)
class CapturedMedia:
    path: str | Path
    source_url: str
    external_id: str
    capture_complete: bool
    expected_duration_seconds: float
    start_seconds: float = 0.0


@dataclass
class MediaBudget:
    """Shared scan allowance for attempted ASR, including failed attempts."""

    max_calls: int = 3
    max_duration_seconds: float = 900
    max_bytes: int = MAX_MEDIA_BYTES
    used_calls: int = field(default=0, init=False)
    used_duration_seconds: float = field(default=0, init=False)
    used_bytes: int = field(default=0, init=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if (type(self.max_calls) is not int or not 0 <= self.max_calls <= 100
                or not _number(self.max_duration_seconds, allow_zero=True)
                or self.max_duration_seconds > 86_400
                or type(self.max_bytes) is not int or not 0 <= self.max_bytes <= MAX_MEDIA_BYTES):
            raise ValueError("invalid_media_budget")

    def reserve(self, duration: float, size: int) -> bool:
        with self._lock:
            if not self._fits(duration, size):
                return False
            self.used_calls += 1
            self.used_duration_seconds += duration
            self.used_bytes += size
            return True

    def permits(self, duration: float, size: int) -> bool:
        with self._lock:
            return self._fits(duration, size)

    def _fits(self, duration: float, size: int) -> bool:
        return (self.used_calls < self.max_calls
                and self.used_duration_seconds + duration <= self.max_duration_seconds
                and self.used_bytes + size <= self.max_bytes)


def _number(value: Any, *, allow_zero: bool = False) -> TypeGuard[int | float]:
    return (type(value) in {int, float} and math.isfinite(value)
            and (value >= 0 if allow_zero else value > 0))


def _tolerance(duration: float) -> float:
    # Codec padding is small; a long video must not permit minutes of missing audio.
    return min(2.0, max(0.25, duration * 0.005), duration * 0.02)


def _source_url(platform: str, url: str, identity: str) -> str:
    url, _ = validate_public_url(url, resolve=False)
    parsed = urlsplit(url)
    host, query = parsed.hostname or "", parse_qs(parsed.query)
    domain = {"youtube": "youtube.com", "bilibili": "bilibili.com",
              "douyin": "douyin.com", "xiaohongshu": "xiaohongshu.com"}.get(platform)
    if not domain or not (host == domain or host.endswith("." + domain)
                          or (platform == "youtube" and host in {"youtu.be", "www.youtu.be"})):
        raise ValueError("media_source_mismatch")
    if platform == "youtube":
        if host in {"youtu.be", "www.youtu.be"}:
            actual = parsed.path.strip("/")
        elif parsed.path == "/watch":
            actual = query.get("v", [""])[0]
        else:
            match = re.fullmatch(r"/(?:shorts|embed)/([A-Za-z0-9_-]{11})/?", parsed.path)
            actual = match.group(1) if match else ""
        if actual != identity or not re.fullmatch(r"[A-Za-z0-9_-]{11}", actual):
            raise ValueError("media_source_mismatch")
        return "https://www.youtube.com/watch?v=" + actual
    if platform == "bilibili":
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10})/?", parsed.path)
        part = query.get("p", ["1"])[0]
        if not match or match.group(1) != identity or not part.isdigit() or not 1 <= int(part) <= 1000:
            raise ValueError("media_source_mismatch")
        return "https://www.bilibili.com/video/" + identity + (f"?p={int(part)}" if int(part) > 1 else "")
    if platform == "douyin":
        match = re.fullmatch(r"/video/(\d{10,30})/?", parsed.path)
        if not match or match.group(1) != identity:
            raise ValueError("media_source_mismatch")
        return "https://www.douyin.com/video/" + identity
    match = re.fullmatch(r"/(?:explore|search_result)/([a-fA-F0-9]{24})/?|"
                        r"/user/profile/[a-fA-F0-9]{24}/([a-fA-F0-9]{24})/?", parsed.path)
    if not match or next(value for value in match.groups() if value) != identity:
        raise ValueError("media_source_mismatch")
    # Browser navigation tokens belong only to the active tab, never to evidence.
    return "https://www.xiaohongshu.com/explore/" + identity


def _decoder(command: Sequence[str]) -> str:
    if "--ffmpeg" in command:
        index = command.index("--ffmpeg")
        if index + 1 < len(command):
            return command[index + 1]
    return shutil.which("ffmpeg") or ""


def probe_media_duration(path: Path, *, ffmpeg: str, max_duration_seconds: float,
                         timeout_seconds: float, cancelled: Callable[[], bool] | None = None) -> float:
    """Measure decoded audio samples, rather than trusting a container's duration."""
    decoder = Path(ffmpeg)
    if not ffmpeg or not decoder.is_file() or not os.access(decoder, os.X_OK):
        raise ValueError("media_decoder_unavailable")
    deadline = time.monotonic() + timeout_seconds
    with tempfile.TemporaryFile() as pcm, ExitStack() as cleanup:
        process = subprocess.Popen([
            str(decoder.resolve()), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
            "-threads", "1", "-protocol_whitelist", "file,pipe", "-i", str(path),
            "-t", str(max_duration_seconds + 1), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-f", "s16le", "-ac", "1", "-ar", str(_SAMPLE_RATE), "-"],
            stdin=subprocess.DEVNULL, stdout=pcm, stderr=subprocess.DEVNULL,
            shell=False, start_new_session=(os.name != "nt"))
        cleanup.callback(_stop_process, process)
        while process.poll() is None:
            check_cancelled(cancelled)
            if time.monotonic() >= deadline:
                raise ValueError("media_probe_timeout")
            if os.fstat(pcm.fileno()).st_size > (max_duration_seconds + 1) * _SAMPLE_RATE * 2:
                raise ValueError("media_duration_mismatch")
            time.sleep(0.05)
        check_cancelled(cancelled)
        if process.returncode:
            raise ValueError("media_decode_failed")
        size = os.fstat(pcm.fileno()).st_size
        if not size:
            raise ValueError("media_audio_missing")
        return size / (_SAMPLE_RATE * 2)


def _fingerprint(path: Path, cancelled: Callable[[], bool] | None, deadline: float) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            check_cancelled(cancelled)
            if time.monotonic() >= deadline:
                raise ValueError("transcription_timeout")
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_MEDIA_BYTES:
                raise ValueError("media_too_large")
            digest.update(chunk)
    return digest.hexdigest()


def _payload(value: Mapping[str, Any], duration: float) -> dict[str, Any]:
    text = value.get("content_text")
    segments = value.get("segments")
    if (not isinstance(text, str) or not text.strip() or "\ufffd" in text or len(text) > 250_000
            or not isinstance(segments, list) or not 1 <= len(segments) <= 5000):
        raise ValueError("invalid_transcription_output")
    normalized = [TranscriptSegment.model_validate(segment).model_dump(mode="json") for segment in segments]
    if any(segment["end_seconds"] is None or segment["end_seconds"] > duration + _tolerance(duration)
           for segment in normalized):
        raise ValueError("transcription_duration_mismatch")
    if text != "\n".join(segment["text"] for segment in normalized):
        raise ValueError("invalid_transcription_output")
    return {"content_text": text, "segments": normalized}


def transcribe_captured_media(
    capture: CapturedMedia, item: Mapping[str, Any], *, command: Sequence[str] | None,
    timeout_seconds: float = 900, cancelled: Callable[[], bool] | None = None,
    budget: MediaBudget | None = None, cache: MutableMapping[str, dict[str, Any]] | None = None,
    probe: Callable[..., float] | None = None, transcriber: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a normal source item after complete capture and audio validation.

    The caller owns the captured file and its cleanup. Cache entries contain only
    ASR text, timings and measured duration, never source dates or signed URLs.
    """
    try:
        check_cancelled(cancelled)
        if capture.capture_complete is not True:
            raise ValueError("media_capture_incomplete")
        expected = capture.expected_duration_seconds
        if not _number(expected) or expected > MAX_MEDIA_SECONDS:
            raise ValueError("invalid_media_duration")
        # Browser playback can advance one frame while MediaRecorder starts.
        # Never allow a seeked excerpt; tolerate at most 50 ms / 0.5% startup.
        if (not _number(capture.start_seconds, allow_zero=True)
                or capture.start_seconds > min(0.05, expected * 0.005)):
            raise ValueError("media_capture_incomplete")
        if not _number(timeout_seconds) or not 1 <= timeout_seconds <= 900:
            raise ValueError("invalid_transcription_timeout")
        if (not isinstance(command, (list, tuple)) or not command or len(command) > 60
                or not all(isinstance(arg, str) and 0 < len(arg) <= 4096 for arg in command)
                or not any("{input}" in arg for arg in command)
                or not any("{output_dir}" in arg for arg in command)):
            return {"status": "needs_configuration", "detail": "invalid_transcriber_configuration"}
        platform, identity = item.get("platform"), item.get("external_id")
        if (not isinstance(platform, str) or not isinstance(identity, str) or not identity
                or capture.external_id != identity or not isinstance(item.get("source_url"), str)):
            raise ValueError("media_source_mismatch")
        url = _source_url(platform, item["source_url"], identity)
        if _source_url(platform, capture.source_url, identity) != url:
            raise ValueError("media_source_mismatch")
        path = Path(capture.path).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in {
            ".mp3", ".mp4", ".wav", ".m4a", ".mov", ".mkv", ".webm", ".ogg", ".flac", ".aac",
        }:
            raise ValueError("invalid_media_file")
        size = path.stat().st_size
        if not 0 < size <= MAX_MEDIA_BYTES:
            raise ValueError("media_too_large" if size else "media_audio_missing")
        deadline = time.monotonic() + timeout_seconds
        media_hash = _fingerprint(path, cancelled, deadline)
        key = hashlib.sha256(json.dumps([_CACHE_VERSION, media_hash, list(command)],
                                        separators=(",", ":")).encode()).hexdigest()
        cached = cache.get(key) if cache is not None else None
        if cached is not None:
            cached_duration = cached.get("duration_seconds")
            if not _number(cached_duration):
                raise ValueError("invalid_transcription_cache")
            duration = float(cached_duration)
            payload = _payload(cached, duration)
        else:
            if budget is not None and not budget.permits(expected, size):
                return {"status": "partial", "detail": "media_budget_exhausted", "cache_hit": False}
            duration = (probe or probe_media_duration)(path, ffmpeg=_decoder(command),
                max_duration_seconds=expected + _tolerance(expected),
                timeout_seconds=max(0.01, min(60, deadline - time.monotonic())), cancelled=cancelled)
            if not _number(duration):
                raise ValueError("invalid_media_duration")
            payload = {}
        if abs(duration - expected) > _tolerance(expected):
            raise ValueError("media_duration_mismatch")
        check_cancelled(cancelled)
        if cached is None:
            if budget is not None and not budget.reserve(duration, size):
                return {"status": "partial", "detail": "media_budget_exhausted", "cache_hit": False}
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise ValueError("transcription_timeout")
            result = (transcriber or transcribe_media)(path, command=list(command),
                timeout_seconds=remaining, cancelled=cancelled, source_url=url,
                title=str(item.get("title") or "视频材料"), author=str(item.get("author") or ""), platform=platform)
            check_cancelled(cancelled)
            if result.get("status") != "success" or not isinstance(result.get("item"), dict):
                detail = result.get("detail")
                raise ValueError(detail if detail in _SAFE_ERRORS else "transcriber_failed")
            payload = _payload(result["item"], duration)
            # Detect replacement/modification between integrity checks and ASR.
            if _fingerprint(path, cancelled, deadline) != media_hash:
                raise ValueError("media_changed_during_transcription")
            if cache is not None:
                cache[key] = {**deepcopy(payload), "duration_seconds": duration}
        check_cancelled(cancelled)
        issues = [issue for issue in item.get("quality_issues", []) if isinstance(issue, str)
                  and issue not in {"transcript_unavailable", "browser_metadata_only", "transcript_timestamps_unknown"}]
        issues.extend(["machine_transcription_unverified", "recording_time_unknown", "local_media_transcription"])
        if capture.start_seconds:
            issues.append("recording_start_offset_negligible")
        output = {name: item.get(name) for name in ("external_id", "title", "author", "published_at", "platform")}
        output.update(source_url=url, summary=str(item.get("summary") or "")[:20_000],
                      content_kind="transcript", completeness="full", **payload,
                      quality_issues=list(dict.fromkeys(issues)))
        return {"status": "success", "detail": "local_transcription_complete", "item": output,
                "cache_hit": cached is not None, "media_sha256": media_hash, "duration_seconds": duration,
                "capture_start_seconds": capture.start_seconds}
    except Exception as exc:
        detail = str(exc) if isinstance(exc, (ValueError, ContentAccessError)) else "media_transcription_failed"
        detail = detail if detail in _SAFE_ERRORS else "media_transcription_failed"
        status = "cancelled" if detail == "cancelled" else "needs_configuration" if detail == "media_decoder_unavailable" else "failed"
        return {"status": status, "detail": detail}


_SAFE_ERRORS = {
    "cancelled", "media_capture_incomplete", "invalid_media_duration", "invalid_transcription_timeout",
    "media_source_mismatch", "invalid_media_file", "media_too_large", "media_audio_missing",
    "media_decoder_unavailable", "media_probe_timeout", "media_decode_failed", "media_duration_mismatch",
    "invalid_transcription_output", "transcription_duration_mismatch", "invalid_transcription_cache",
    "transcription_timeout", "transcription_output_too_large", "invalid_transcription_timestamp",
    "content_too_large", "transcriber_failed", "transcription_failed", "media_changed_during_transcription",
    "media_transcription_failed",
}
