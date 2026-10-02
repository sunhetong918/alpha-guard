"""Import user-provided evidence and optional local Whisper-compatible output."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Callable, Sequence

from research.network import check_cancelled, validate_public_url

MAX_TEXT_CHARS = 250_000
_TIME = r"(?:(\d{1,3}):)?(\d{2}):(\d{2})[.,](\d{3})"
_CUE = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}[^\n]*$", re.MULTILINE)


def _seconds(parts: tuple[str | None, ...]) -> float:
    hours, minutes, seconds, millis = parts
    if int(minutes or 0) >= 60 or int(seconds or 0) >= 60:
        raise ValueError("invalid_subtitle_timestamp")
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(seconds or 0) + int(millis or 0) / 1000


def parse_subtitles(text: str) -> list[dict[str, Any]]:
    """Parse SRT/WebVTT cues without inventing timing or speaker metadata."""
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("content_too_large")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_CUE.finditer(text))
    if len(matches) > 5000:
        raise ValueError("too_many_subtitle_segments")
    segments: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = _seconds(match.groups()[:4])
        end = _seconds(match.groups()[4:])
        if end < start:
            raise ValueError("invalid_subtitle_timestamp")
        stop = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():stop].strip()
        # The next SRT numeric cue identifier is not spoken text.
        body = re.sub(r"\n\s*\d+\s*$", "", body)
        body = re.split(r"\n\s*\n", body)[0]
        speaker_match = re.search(r"<v\s+([^>]+)>", body)
        speaker = html.unescape(speaker_match.group(1)).strip() if speaker_match else ""
        body = html.unescape(re.sub(r"<[^>]+>", "", body)).strip()
        if body:
            segments.append({"start_seconds": start, "end_seconds": end,
                             "text": body, "speaker": speaker})
    return segments


def ingest_text(
    text: str, kind: str = "article", *, source_url: str = "", title: str = "",
    author: str = "", platform: str = "manual", published_at: str | None = None,
) -> dict[str, Any]:
    """Normalize an explicit import. Plain transcripts keep unknown timestamps."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("content_required")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("content_too_large")
    if kind not in {"article", "transcript", "srt", "vtt", "summary"}:
        raise ValueError("unsupported_content_kind")
    if source_url:
        source_url, _ = validate_public_url(source_url, resolve=False)
    text = text.strip()
    segments = parse_subtitles(text) if kind in {"transcript", "srt", "vtt"} else []
    if kind in {"srt", "vtt"} and not segments:
        raise ValueError("subtitle_timestamps_required")
    content_kind = "transcript" if kind in {"srt", "vtt", "transcript"} else kind
    content_text = "\n".join(segment["text"] for segment in segments) if segments else text
    issues = []
    if not source_url:
        issues.append("source_url_missing")
    if content_kind == "transcript":
        issues.append("recording_time_unknown")
        if not segments:
            issues.append("transcript_timestamps_unknown")
    return {
        "external_id": hashlib.sha256((source_url or content_text).encode()).hexdigest(),
        "source_url": source_url, "title": title[:500] or "用户导入材料",
        "author": author[:200], "published_at": published_at, "summary": "",
        "content_text": content_text, "content_kind": content_kind,
        "completeness": "partial" if kind == "summary" else "full",
        "segments": segments, "platform": platform, "quality_issues": issues,
    }


def _stop_process(process: Any, *, platform: str | None = None) -> None:
    """Clean up the fresh Popen's own session, including an exited leader's children.

    POSIX callers launch with start_new_session=True and invoke this cleanup
    immediately on completion/cancellation. Never pass an arbitrary saved PID.
    """
    if (platform or os.name) == "nt":
        if process.poll() is not None:
            return
        # Windows has no killpg. taskkill targets this Popen's PID and descendants;
        # an absolute system binary avoids resolving a user-controlled executable.
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        executable = system_root.rstrip("\\/") + r"\System32\taskkill.exe"
        try:
            subprocess.run([executable, "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, shell=False, timeout=3, check=False)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=2)
        return
    import signal
    group = process.pid
    if type(group) is not int or group <= 1 or group == os.getpgrp():
        return
    try:
        owned_session = os.getpgid(group) == group and os.getsid(group) == group
    except ProcessLookupError:
        # An already reaped leader may still have children in its original
        # process group. That group's ID remains reserved while they exist.
        owned_session = True
    if not owned_session:
        # Defensive fallback for a caller that did not start a new session:
        # terminate only the Popen, never its inherited process group.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        return
    deadline = time.monotonic() + 2
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=2)
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # Waiting for the direct child does not wait for its descendants. Probe the
    # same group even after that child exits and enforce the shared grace period.
    while time.monotonic() < deadline:
        try:
            os.kill(-group, 0)
        except ProcessLookupError:
            return
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        # Reap the owned direct child even on platforms where group signalling
        # raced its exit; Popen.kill checks its own process state first.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def transcribe_media(
    path: str | Path, *, command: Sequence[str] | None = None,
    timeout_seconds: float = 900, cancelled: Callable[[], bool] | None = None,
    source_url: str = "", title: str = "", author: str = "", platform: str = "manual",
) -> dict[str, Any]:
    """Run a trusted, explicitly configured local Whisper-compatible CLI.

    ALPHA_GUARD_TRANSCRIPTION_COMMAND is a JSON argv array, e.g.
    ["whisper","{input}","--output_format","json","--output_dir","{output_dir}"].
    No shell is involved. A JSON output file must contain text and/or segments.
    The command is trusted operator configuration, never external source text.
    """
    if command is None:
        raw = os.environ.get("ALPHA_GUARD_TRANSCRIPTION_COMMAND", "")
        if not raw:
            return {"status": "needs_configuration", "detail": "local_transcriber_not_configured"}
        try:
            command = json.loads(raw)
        except (TypeError, ValueError):
            return {"status": "needs_configuration", "detail": "invalid_transcriber_configuration"}
    if (not isinstance(command, (list, tuple)) or not command
            or len(command) > 60 or not all(isinstance(arg, str) and 0 < len(arg) <= 4096 for arg in command)
            or not any("{input}" in arg for arg in command)
            or not any("{output_dir}" in arg for arg in command)):
        return {"status": "needs_configuration", "detail": "invalid_transcriber_configuration"}
    if not 1 <= timeout_seconds <= 3600:
        raise ValueError("invalid_transcription_timeout")
    media = Path(path).expanduser().resolve()
    if not media.is_file() or media.suffix.lower() not in {
        ".mp3", ".mp4", ".wav", ".m4a", ".mov", ".mkv", ".webm", ".ogg", ".flac", ".aac"
    }:
        return {"status": "failed", "detail": "invalid_media_file"}
    if media.stat().st_size > 2_000_000_000:
        return {"status": "failed", "detail": "media_too_large"}
    process = None
    try:
        check_cancelled(cancelled)
        # ExitStack stops the process before TemporaryDirectory removes files;
        # Windows cannot unlink an output file still held by a running CLI.
        with tempfile.TemporaryDirectory(prefix="alpha-guard-transcript-") as directory, ExitStack() as cleanup:
            args = [arg.replace("{input}", str(media)).replace("{output_dir}", directory) for arg in command]
            # DEVNULL ensures a verbose/faulty CLI cannot exhaust memory or leak
            # credentials through logged subprocess output.
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, shell=False, start_new_session=(os.name != "nt"))
            cleanup.callback(_stop_process, process)
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                check_cancelled(cancelled)
                if time.monotonic() >= deadline:
                    raise ValueError("transcription_timeout")
                if any(p.stat().st_size > 2_000_000 for p in Path(directory).glob("*.json")):
                    raise ValueError("transcription_output_too_large")
                time.sleep(0.1)
            check_cancelled(cancelled)
            if process.returncode:
                return {"status": "failed", "detail": "transcriber_failed"}
            files = sorted(Path(directory).glob("*.json"))
            if len(files) != 1 or files[0].stat().st_size > 2_000_000:
                return {"status": "failed", "detail": "invalid_transcription_output"}
            output = json.loads(files[0].read_text(encoding="utf-8"))
            if not isinstance(output, dict):
                raise ValueError("invalid_transcription_output")
            if "\ufffd" in str(output.get("text", "")):
                raise ValueError("invalid_transcription_output")
            raw_segments = output.get("segments", [])
            if not isinstance(raw_segments, list) or len(raw_segments) > 5000:
                raise ValueError("invalid_transcription_output")
            segments: list[dict[str, Any]] = []
            for segment in raw_segments:
                start, end = float(segment["start"]), float(segment["end"])
                if not 0 <= start <= end <= 86400:
                    raise ValueError("invalid_transcription_timestamp")
                segment_text = str(segment["text"]).strip()
                if "\ufffd" in segment_text:
                    raise ValueError("invalid_transcription_output")
                if segment_text:
                    segments.append({"start_seconds": start, "end_seconds": end,
                                     "text": segment_text, "speaker": str(segment.get("speaker") or "")[:160]})
            text = "\n".join(s["text"] for s in segments) or str(output.get("text", "")).strip()
            item = ingest_text(text, "transcript", source_url=source_url, title=title or media.stem,
                               author=author, platform=platform)
            item["segments"] = segments
            item["quality_issues"] = ["machine_transcription_unverified", "recording_time_unknown"]
            if not source_url:
                item["quality_issues"].append("source_url_missing")
            if not segments:
                item["quality_issues"].append("transcript_timestamps_unknown")
            return {"status": "success", "detail": "local_transcription_complete", "item": item}
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "transcription_failed"
        allowed = {"cancelled", "transcription_timeout", "transcription_output_too_large",
                   "invalid_transcription_output", "invalid_transcription_timestamp", "content_too_large"}
        return {"status": "failed", "detail": reason if reason in allowed else "transcription_failed"}
