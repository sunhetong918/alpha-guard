from copy import deepcopy
from datetime import UTC, datetime

import pytest

from research.network import ContentAccessError
from research.store import ResearchStore
from research.video_research import VideoResearch


COMMAND = ["local-asr", "{input}", "{output_dir}"]


def item(identity="7686727666610883903"):
    return {"platform": "douyin", "external_id": identity,
            "source_url": "https://www.douyin.com/video/" + identity,
            "title": "真实讲话", "author": "作者", "published_at": "2026-09-20T00:00:00Z",
            "content_kind": "metadata", "completeness": "metadata", "quality_issues": []}


def full(value):
    return {**value, "completeness": "full", "content_kind": "transcript",
            "content_text": "原始讲话内容。", "segments": [
                {"start_seconds": 0, "end_seconds": 10, "text": "原始讲话内容。", "speaker": ""}],
            "quality_issues": ["machine_transcription_unverified", "local_media_transcription"]}


def test_complete_recording_persists_once_and_repeated_scan_does_not_replay_or_transcribe(tmp_path, monkeypatch):
    calls, paths, stages = [], [], []
    def capture(page, path, **kwargs):
        calls.append("capture")
        paths.append(path)
        path.write_bytes(b"complete audio")
        return {"capture_complete": True, "expected_duration_seconds": 10, "start_seconds": 0}
    def transcribe(capture, value, **kwargs):
        calls.append("transcribe")
        assert capture.path.read_bytes() == b"complete audio"
        return {"status": "success", "item": full(value)}
    monkeypatch.setattr("research.video_research.capture_audio", capture)
    monkeypatch.setattr("research.video_research.transcribe_captured_media", transcribe)
    database = tmp_path / "state.db"
    worker = VideoResearch(database, command=COMMAND, stage=stages.append)
    value = item()
    worker(None, value)
    assert value["completeness"] == "full"
    assert all(not path.exists() for path in paths)
    with ResearchStore(database) as store:
        saved = store.list_evidence()[0]
    # New worker and reopened DB, including original source time and receipt.
    repeated = VideoResearch(database, command=COMMAND)
    fresh = item()
    repeated(None, fresh)
    assert calls == ["capture", "transcribe"]
    assert repeated.cache_hits == 1 and repeated.attempts == 0
    assert fresh["retrieved_at"] == saved.retrieved_at.isoformat().replace("+00:00", "Z")
    with ResearchStore(database) as store:
        store.upsert_evidence(fresh, now=datetime(2030, 1, 1, tzinfo=UTC))
        assert store.list_evidence() == [saved]
    assert stages == ["capturing", "transcribing", "collecting"]


def test_failed_captures_spend_round_budget_and_never_call_asr(tmp_path, monkeypatch):
    calls = []
    def capture(*args, **kwargs):
        calls.append("capture")
        return {"capture_complete": False, "detail": "media_duration_budget_exhausted"}
    monkeypatch.setattr("research.video_research.capture_audio", capture)
    monkeypatch.setattr("research.video_research.transcribe_captured_media", lambda *a, **k: pytest.fail("ASR"))
    worker = VideoResearch(tmp_path / "state.db", command=COMMAND, max_items=1)
    one, two = item(), item("7686727666610883904")
    worker(None, one)
    worker(None, two)
    assert calls == ["capture"]
    assert "media_duration_budget_exhausted" in one["quality_issues"]
    assert "media_budget_exhausted" in two["quality_issues"]
    with ResearchStore(worker.path) as store:
        assert store.list_evidence() == []


def test_cancellation_deletes_temporary_audio_and_never_persists_partial(tmp_path, monkeypatch):
    paths = []
    def capture(page, path, **kwargs):
        paths.append(path)
        path.write_bytes(b"partial")
        raise ContentAccessError("cancelled")
    monkeypatch.setattr("research.video_research.capture_audio", capture)
    worker = VideoResearch(tmp_path / "state.db", command=COMMAND)
    with pytest.raises(ContentAccessError, match="cancelled"):
        worker(None, item())
    assert all(not path.exists() for path in paths)
    with ResearchStore(worker.path) as store:
        assert store.list_evidence() == []


@pytest.mark.parametrize("command", [None, "not argv", ["no placeholders"], [12]])
def test_unconfigured_transcriber_does_not_play_video(tmp_path, monkeypatch, command):
    monkeypatch.setattr("research.video_research.capture_audio", lambda *a, **k: pytest.fail("capture"))
    worker = VideoResearch(tmp_path / "state.db", command=command)
    value = item()
    original = deepcopy(value)
    worker(None, value)
    assert value["completeness"] == original["completeness"]
    assert "invalid_transcriber_configuration" in value["quality_issues"]
    assert worker.attempts == 0
