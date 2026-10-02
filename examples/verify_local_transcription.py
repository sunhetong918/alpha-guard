"""Opt-in, real local speech-to-evidence acceptance using synthetic macOS speech.

Run from the repository with its Python via ``-m examples.verify_local_transcription``.
The isolated runtime must already contain the local model and its dependencies.
No application settings, live research database, or network source is used.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
from unittest.mock import patch
import uuid
import wave

from config import Settings
from research.ipc import execute
from research.service import ResearchService
from research.store import ResearchStore

SAMPLES = {
    "english": ("Samantha", 150, "This is a local speech recognition test for Alpha Guard. "
                "We review company revenue, cash flow, and competitive advantages. "
                "Short term prices may react to news. Long term research needs financial evidence. "
                "This recording contains no investment advice."),
    "chinese": ("Tingting", 170, "这是一个本地语音识别测试。我们关注公司的收入、现金流和竞争优势。"
                "短期股价可能受到新闻影响，长期判断需要核对财务数据。"
                "这段录音不包含真实的投资建议。"),
}


def verify(runtime: Path, ffmpeg: Path, *, synthesize: bool = False, model: str = "base") -> dict[str, Any]:
    if model not in {"tiny", "base"}:
        raise ValueError("unsupported acceptance model")
    runtime = runtime.expanduser().resolve()
    folder = runtime / "verification"
    folder.mkdir(parents=True, exist_ok=True)
    command = [str(runtime / "venv/bin/python"), str(runtime / "local_whisper.py"),
               "{input}", "--model-file", str(runtime / "models" / f"{model}.pt"),
               "--ffmpeg", str(ffmpeg), "--output-dir", "{output_dir}", "--threads", "4"]
    # model_construct populates defaults without reading .env or process secrets.
    settings = Settings.model_construct(transcription_command=json.dumps(command), anthropic_api_key=None)
    database = folder / f"service-{uuid.uuid4().hex[:8]}.sqlite"
    service = ResearchService(database)
    results: list[dict[str, Any]] = []
    with patch("config.get_settings", return_value=settings):
        service.configure({"ai_enabled": False, "news_enabled": False})
        assert service.snapshot()["runtime"]["state"] == "STOPPED"
        for name, (voice, rate, expected) in SAMPLES.items():
            media = folder / f"{name}.wav"
            if synthesize:
                source = folder / f"{name}-expected.txt"
                source.write_text(expected, encoding="utf-8")
                aiff = folder / f"{name}.aiff"
                subprocess.run(["/usr/bin/say", "-v", voice, "-r", str(rate), "-f", str(source),
                                "-o", str(aiff)], check=True, timeout=60)
                subprocess.run([str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-i", str(aiff),
                                "-ar", "16000", "-ac", "1", str(media)], check=True, timeout=30)
            started = time.monotonic()
            execute(service, {"command": "transcribe", "path": str(media),
                              "title": f"本机合成语音技术验收｜{name}｜不是真实访谈"})
            service.wait(180)
            snapshot = service.snapshot()
            if snapshot["runtime"]["state"] != "STOPPED":
                service.close(5)
            assert snapshot["runtime"]["state"] == "STOPPED", snapshot["runtime"]
            assert snapshot["runtime"]["last_run"]["status"] == "success", snapshot["runtime"]
            assert snapshot["runtime"]["next_run_at"] is None
            with ResearchStore(database) as store:
                evidence = next(item for item in store.list_evidence() if name in item.title)
                assert evidence.segments and evidence.content_kind == "transcript"
                assert evidence.completeness == "full" and not evidence.research_ready
                assert "machine_transcription_unverified" in evidence.quality_issues
                assert "source_url_missing" in evidence.quality_issues
                assert evidence.event_at is None
            with wave.open(str(media), "rb") as stream:
                duration = stream.getnframes() / stream.getframerate()
            results.append({"sample": name, "synthetic": True, "audio_seconds": round(duration, 3),
                            "elapsed_seconds": round(time.monotonic() - started, 3), "expected": expected,
                            "recognized": evidence.content_text,
                            "segments": [item.model_dump(mode="json") for item in evidence.segments],
                            "evidence_id": evidence.id, "quality_issues": evidence.quality_issues,
                            "research_ready": evidence.research_ready})
            print(json.dumps({"sample": name, "status": "success", "segments": len(evidence.segments)},
                             ensure_ascii=False), flush=True)

        entered = threading.Event()
        processes: list[Any] = []
        original_popen = subprocess.Popen

        def recording_popen(*args: Any, **kwargs: Any):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            entered.set()
            return process

        with patch("research.ingest.subprocess.Popen", side_effect=recording_popen):
            execute(service, {"command": "transcribe", "path": str(folder / "english.wav"),
                              "title": "取消验收，不应保存"})
            assert entered.wait(5), "transcriber process did not launch"
            time.sleep(0.2)
            assert processes[0].poll() is None, "transcriber exited before cancellation"
            stopped_at = time.monotonic()
            execute(service, {"command": "stop"})
            service.wait(5)
        snapshot = service.snapshot()
        assert snapshot["runtime"]["state"] == "STOPPED"
        assert snapshot["runtime"]["last_run"]["status"] == "cancelled"
        assert snapshot["runtime"]["next_run_at"] is None
        assert processes[0].poll() is not None, "transcription subprocess leaked"
        with ResearchStore(database) as store:
            assert len(store.list_evidence()) == len(SAMPLES)
        restarted = ResearchService(database)
        assert restarted.snapshot()["runtime"]["state"] == "STOPPED"
        result = {"verified_at": datetime.now(UTC).isoformat(), "kind": "synthetic_technical_acceptance",
                  "model": model, "model_sha256": hashlib.sha256((runtime / "models" / f"{model}.pt").read_bytes()).hexdigest(),
                  "command": command, "database": str(database), "samples": results,
                  "cancellation": {"status": "cancelled", "state": "STOPPED", "process_exited": True,
                                   "elapsed_seconds": round(time.monotonic() - stopped_at, 3)},
                  "restart_state": "STOPPED", "network_sources_started": False}
        (folder / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / f"acceptance-{model}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        service.close()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, default=Path("/opt/homebrew/bin/ffmpeg"))
    parser.add_argument("--synthesize", action="store_true")
    parser.add_argument("--model", choices=("tiny", "base"), default="base")
    options = parser.parse_args()
    result = verify(options.runtime_dir, options.ffmpeg, synthesize=options.synthesize, model=options.model)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
