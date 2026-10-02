"""Offline-only OpenAI Whisper runner for an isolated local Python environment.

Run this file with the transcription environment's Python, not the Guardian's
Python. Models must already exist locally. FFmpeg receives a local-file-only
protocol allowlist, and model inference never fetches or uploads media.
"""
from __future__ import annotations

import argparse
import json
import importlib
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable

SAMPLE_RATE = 16_000
MAX_SECONDS = 10_800
MAX_OUTPUT_BYTES = 2_000_000


def _local_file(value: str | Path, reason: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(reason)
    return path


def _default_loader(model_path: Path, threads: int):
    import torch
    import whisper

    torch.set_num_threads(threads)
    # A path, never a registry alias: a missing file fails before this call, so
    # Whisper cannot fall back to downloading weights during media processing.
    return whisper.load_model(str(model_path), device="cpu")


def transcribe_local(
    input_path: str | Path, *, model_file: str | Path, ffmpeg: str | Path,
    output_dir: str | Path, language: str | None = None, threads: int = 4,
    max_seconds: int = MAX_SECONDS,
    model_loader: Callable[[Path, int], Any] | None = None,
) -> Path:
    """Decode a bounded local file and write standard Whisper JSON atomically."""
    media = _local_file(input_path, "media_file_missing")
    model_path = _local_file(model_file, "local_model_missing")
    decoder = _local_file(ffmpeg, "ffmpeg_missing")
    if not os.access(decoder, os.X_OK):
        raise ValueError("ffmpeg_not_executable")
    if type(threads) is not int or not 1 <= threads <= 16:
        raise ValueError("invalid_thread_limit")
    if type(max_seconds) is not int or not 1 <= max_seconds <= MAX_SECONDS:
        raise ValueError("invalid_audio_duration_limit")
    if language is not None and (not language.isalpha() or len(language) > 20):
        raise ValueError("invalid_language")
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as decoded:
        subprocess.run(
            [str(decoder), "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1",
             "-protocol_whitelist", "file,pipe", "-i", str(media),
             "-t", str(max_seconds + 1), "-vn", "-sn", "-dn", "-f", "s16le",
             "-ac", "1", "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-"],
            stdout=decoded, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            shell=False, check=True, timeout=300,
        )
        size = decoded.tell()
        if size == 0:
            raise ValueError("audio_empty")
        if size > SAMPLE_RATE * 2 * max_seconds:
            raise ValueError("audio_duration_limit")
        decoded.seek(0)
        np = importlib.import_module("numpy")
        audio = np.frombuffer(decoded.read(), np.int16).astype(np.float32) / 32768.0
    model = (model_loader or _default_loader)(model_path, threads)
    result = model.transcribe(audio, language=language, task="transcribe", fp16=False,
                              verbose=False, condition_on_previous_text=False, temperature=0.0)
    if not isinstance(result, dict) or not isinstance(result.get("segments"), list):
        raise ValueError("invalid_transcription_output")
    if not str(result.get("text") or "").strip() or not result["segments"]:
        raise ValueError("no_speech_detected")
    payload = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_OUTPUT_BYTES:
        raise ValueError("transcription_output_too_large")
    path = destination / "transcript.json"
    temporary = destination / "transcript.json.part"
    temporary.write_bytes(payload)
    temporary.replace(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline local-file Whisper transcription")
    parser.add_argument("input")
    parser.add_argument("--model-file", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--language")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--max-seconds", type=int, default=MAX_SECONDS)
    options = parser.parse_args()
    try:
        transcribe_local(options.input, model_file=options.model_file, ffmpeg=options.ffmpeg,
                         output_dir=options.output_dir, language=options.language,
                         threads=options.threads, max_seconds=options.max_seconds)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "local_transcription_failed"
        allowed = {"media_file_missing", "local_model_missing", "ffmpeg_missing", "ffmpeg_not_executable",
                   "invalid_thread_limit", "invalid_audio_duration_limit", "invalid_language",
                   "audio_empty", "audio_duration_limit", "invalid_transcription_output",
                   "no_speech_detected", "transcription_output_too_large"}
        print(json.dumps({"status": "failed", "detail": reason if reason in allowed else "local_transcription_failed"}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
