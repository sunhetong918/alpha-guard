from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from unittest.mock import patch
import wave

import pytest

from research.media_transcription import CapturedMedia, MediaBudget, probe_media_duration, transcribe_captured_media
from research.network import ContentAccessError
from research.store import ResearchStore

COMMAND = ["trusted-whisper", "{input}", "--output-dir", "{output_dir}"]
URL = "https://www.douyin.com/video/7690773408996445480"


def fixture(tmp_path: Path, seconds: int = 3):
    path = tmp_path / "capture.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000 * seconds)
    item = {"source_url": URL, "external_id": "7690773408996445480", "platform": "douyin",
            "title": "来源标题", "author": "原始作者", "published_at": "2026-09-20T00:00:00+00:00",
            "summary": "这是视频简介，不是转写正文。", "content_kind": "metadata", "completeness": "metadata",
            "quality_issues": ["transcript_unavailable", "recording_time_unknown"], "segments": []}
    capture = CapturedMedia(path, URL, item["external_id"], True, float(seconds))
    return capture, item


def asr(path, **kwargs):
    return {"status": "success", "item": {
        "content_text": "Source speech with enough content to be quoted as an unverified research observation.",
        "segments": [{"start_seconds": 0.0, "end_seconds": 3.0,
                      "text": "Source speech with enough content to be quoted as an unverified research observation.",
                      "speaker": ""}], "quality_issues": []}}


def test_real_audio_decode_and_source_metadata_survive_asr(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is not installed")
    capture, item = fixture(tmp_path)
    budget = MediaBudget(max_calls=1)
    result = transcribe_captured_media(capture, item, command=COMMAND, budget=budget, transcriber=asr)
    assert result["status"] == "success"
    assert result["duration_seconds"] == 3.0
    saved = result["item"]
    for key in ("external_id", "title", "author", "published_at", "platform", "source_url"):
        assert saved[key] == item[key]
    assert saved["completeness"] == "full"
    assert "machine_transcription_unverified" in saved["quality_issues"]
    assert "recording_time_unknown" in saved["quality_issues"]
    assert "transcript_unavailable" not in saved["quality_issues"]
    assert budget.used_calls == 1 and budget.used_duration_seconds == 3
    assert str(capture.path) not in json.dumps(result)
    with ResearchStore(tmp_path / "state.db") as store:
        evidence = store.upsert_evidence(saved)
        assert evidence.event_at == datetime(2026, 9, 20, tzinfo=UTC)
        assert evidence.research_ready


@pytest.mark.parametrize("changes,detail", [
    ({"capture_complete": False}, "media_capture_incomplete"),
    ({"start_seconds": 1.0}, "media_capture_incomplete"),
    ({"start_seconds": 0.1}, "media_capture_incomplete"),
    ({"start_seconds": False}, "media_capture_incomplete"),
    ({"expected_duration_seconds": float("nan")}, "invalid_media_duration"),
    ({"expected_duration_seconds": True}, "invalid_media_duration"),
    ({"external_id": "7690773408996445481"}, "media_source_mismatch"),
    ({"source_url": "https://www.douyin.com/video/7690773408996445481"}, "media_source_mismatch"),
])
def test_incomplete_or_wrong_source_never_reaches_decoder(tmp_path, changes, detail):
    capture, item = fixture(tmp_path)
    result = transcribe_captured_media(replace(capture, **changes), item, command=COMMAND,
        probe=lambda *a, **k: pytest.fail("invalid media must not be decoded"), transcriber=asr)
    assert result == {"status": "failed", "detail": detail}


def test_truncated_audio_is_rejected_by_real_samples_not_wav_header(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is not installed")
    capture, item = fixture(tmp_path, seconds=6)
    with Path(capture.path).open("r+b") as output:
        output.truncate(44 + 16000 * 2 * 3)  # Keep the six-second WAV header intact.
    result = transcribe_captured_media(capture, item, command=COMMAND,
        transcriber=lambda *a, **k: pytest.fail("truncated audio must not reach ASR"))
    assert result["status"] == "failed"
    assert result["detail"] in {"media_duration_mismatch", "media_decode_failed"}


def test_tiny_recorder_start_jitter_is_explicit_and_bounded(tmp_path):
    capture, item = fixture(tmp_path)
    result = transcribe_captured_media(replace(capture, start_seconds=0.013), item,
        command=COMMAND, probe=lambda *a, **k: 3.0, transcriber=asr)
    assert result["status"] == "success"
    assert result["capture_start_seconds"] == 0.013
    assert "recording_start_offset_negligible" in result["item"]["quality_issues"]


@pytest.mark.parametrize("budget", [MediaBudget(max_calls=0), MediaBudget(max_duration_seconds=1), MediaBudget(max_bytes=1)])
def test_exhausted_shared_budget_prevents_decode_and_asr(tmp_path, budget):
    capture, item = fixture(tmp_path)
    result = transcribe_captured_media(capture, item, command=COMMAND, budget=budget,
        probe=lambda *a, **k: pytest.fail("budget must be checked before decode"), transcriber=asr)
    assert result["detail"] == "media_budget_exhausted"
    assert budget.used_calls == 0


def test_failed_attempt_consumes_budget_and_errors_are_redacted(tmp_path):
    capture, item = fixture(tmp_path)
    budget = MediaBudget(max_calls=1)
    def failed(*args, **kwargs):
        raise RuntimeError("https://private.example?token=DO-NOT-PRINT")
    result = transcribe_captured_media(capture, item, command=COMMAND, probe=lambda *a, **k: 3,
                                      budget=budget, transcriber=failed)
    assert result == {"status": "failed", "detail": "media_transcription_failed"}
    assert budget.used_calls == 1


def test_cache_reuses_only_asr_and_source_cluster_keeps_original_event(tmp_path):
    capture, item = fixture(tmp_path)
    cache, calls = {}, []
    def counted(*args, **kwargs):
        calls.append(True)
        return asr(*args, **kwargs)
    first = transcribe_captured_media(capture, item, command=COMMAND, probe=lambda *a, **k: 3,
                                     cache=cache, transcriber=counted)
    new_url = "https://www.douyin.com/video/7690773408996445481"
    second_source = {**item, "source_url": new_url, "external_id": "7690773408996445481",
                     "published_at": "2026-09-21T00:00:00+00:00"}
    second = transcribe_captured_media(replace(capture, source_url=new_url, external_id=second_source["external_id"]),
        second_source, command=COMMAND, cache=cache, budget=MediaBudget(max_calls=0),
        probe=lambda *a, **k: pytest.fail("cached identical bytes need no decode"), transcriber=counted)
    assert len(calls) == 1 and second["cache_hit"]
    assert second["item"]["published_at"] == second_source["published_at"]
    assert second["item"]["source_url"] == new_url
    with ResearchStore(tmp_path / "state.db") as store:
        a = store.upsert_evidence(first["item"])
        b = store.upsert_evidence(second["item"])
        assert a.source_cluster == b.source_cluster
        assert a.event_at == b.event_at


def test_cache_is_model_sensitive_and_cannot_be_mutated_through_output(tmp_path):
    capture, item = fixture(tmp_path)
    cache, calls = {}, []
    def counted(*args, **kwargs):
        calls.append(True)
        return asr(*args, **kwargs)
    result = transcribe_captured_media(capture, item, command=COMMAND, probe=lambda *a, **k: 3,
                                      cache=cache, transcriber=counted)
    result["item"]["segments"][0]["text"] = "caller edit"
    again = transcribe_captured_media(capture, item, command=COMMAND, cache=cache, transcriber=counted)
    assert again["cache_hit"] and "caller edit" not in again["item"]["segments"][0]["text"]
    changed = transcribe_captured_media(capture, item, command=[*COMMAND, "--model", "different"],
                                       probe=lambda *a, **k: 3, cache=cache, transcriber=counted)
    assert not changed["cache_hit"] and len(calls) == 2


def test_page_navigation_tokens_never_leave_source_binding(tmp_path):
    capture, item = fixture(tmp_path)
    identity = "1234567890abcdef12345678"
    url = f"https://www.xiaohongshu.com/explore/{identity}?xsec_token=PRIVATE&xsec_source=pc_user"
    item.update(platform="xiaohongshu", external_id=identity, source_url=url)
    result = transcribe_captured_media(replace(capture, external_id=identity, source_url=url), item,
        command=COMMAND, probe=lambda *a, **k: 3, transcriber=asr)
    assert result["status"] == "success"
    assert result["item"]["source_url"] == f"https://www.xiaohongshu.com/explore/{identity}"
    assert "PRIVATE" not in json.dumps(result)


def test_stop_after_asr_does_not_return_item_or_cache_result(tmp_path):
    capture, item = fixture(tmp_path)
    stopped = False
    cache = {}
    def stop(*args, **kwargs):
        nonlocal stopped
        stopped = True
        return asr(*args, **kwargs)
    result = transcribe_captured_media(capture, item, command=COMMAND, probe=lambda *a, **k: 3,
                                      cache=cache, cancelled=lambda: stopped, transcriber=stop)
    assert result == {"status": "cancelled", "detail": "cancelled"}
    assert cache == {}


def test_out_of_range_asr_timestamps_are_not_complete_evidence(tmp_path):
    capture, item = fixture(tmp_path)
    def out_of_range(*args, **kwargs):
        value = asr(*args, **kwargs)
        value["item"]["segments"][0]["end_seconds"] = 10.0
        return value
    result = transcribe_captured_media(capture, item, command=COMMAND, probe=lambda *a, **k: 3,
                                      transcriber=out_of_range)
    assert result == {"status": "failed", "detail": "transcription_duration_mismatch"}


def test_media_mutation_during_asr_is_rejected(tmp_path):
    capture, item = fixture(tmp_path)
    def changed(*args, **kwargs):
        Path(capture.path).write_bytes(b"different audio")
        return asr(*args, **kwargs)
    result = transcribe_captured_media(capture, item, command=COMMAND, probe=lambda *a, **k: 3,
                                      transcriber=changed)
    assert result == {"status": "failed", "detail": "media_changed_during_transcription"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_probe_cancellation_terminates_actual_owned_process(tmp_path):
    capture, _ = fixture(tmp_path)
    decoder = tmp_path / "decoder"
    decoder.write_text("#!/bin/sh\nexec sleep 30\n")
    decoder.chmod(0o700)
    processes = []
    original = subprocess.Popen
    def recording(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    started = time.monotonic()
    with patch("research.media_transcription.subprocess.Popen", side_effect=recording):
        with pytest.raises(ContentAccessError, match="cancelled"):
            probe_media_duration(Path(capture.path), ffmpeg=str(decoder), max_duration_seconds=3,
                timeout_seconds=5, cancelled=lambda: time.monotonic() - started > 0.15)
    assert processes[0].poll() is not None
