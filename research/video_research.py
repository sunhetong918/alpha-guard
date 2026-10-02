"""Per-run, opt-in bridge from a verified player to durable source evidence."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import tempfile
from typing import Any

from .browser_media import capture_audio
from .browser_source import BrowserPage
from .media_transcription import CapturedMedia, MediaBudget, transcribe_captured_media
from .network import check_cancelled
from .store import ResearchStore


class VideoResearch:
    """Budget attempts before playback; reuse complete source transcripts first.

    Discovery adapters verify the work/author in the owned page. Only complete
    recordings reach ASR. Temporary media are deleted after success or failure.
    Stored original publication/retrieval dates never become this scan's dates.
    """

    def __init__(self, path: Path, *, command: Any, max_items: int = 2,
                 max_seconds: int = 600, cancelled: Callable[[], bool] | None = None,
                 stage: Callable[[str], None] | None = None) -> None:
        self.path = path
        self.command = command
        self.max_items = max_items
        self.max_seconds = max_seconds
        self.cancelled = cancelled
        self.stage = stage or (lambda _: None)
        self.budget = MediaBudget(max_calls=max_items,
                                  max_duration_seconds=max_items * (max_seconds + 2))
        self.attempts = 0
        self.cache_hits = 0
        self._seen: set[str] = set()

    def reuse(self, item: dict[str, Any]) -> bool:
        check_cancelled(self.cancelled)
        with ResearchStore(self.path) as store:
            evidence = store.latest_reusable_transcript(item)
        if evidence is None:
            return False
        item.update(evidence.model_dump(mode="json"))
        self.cache_hits += 1
        return True

    @staticmethod
    def _issue(item: dict[str, Any], detail: str) -> None:
        item["quality_issues"] = list(dict.fromkeys([*item.get("quality_issues", []), detail]))

    def __call__(self, page: BrowserPage, item: dict[str, Any]) -> None:
        check_cancelled(self.cancelled)
        if item.get("completeness") == "full" or self.reuse(item):
            return
        if (not isinstance(self.command, list) or not self.command
                or not all(isinstance(arg, str) for arg in self.command)
                or not any("{input}" in arg for arg in self.command)
                or not any("{output_dir}" in arg for arg in self.command)):
            self._issue(item, "invalid_transcriber_configuration")
            return
        identity = str(item.get("platform")) + ":" + str(item.get("external_id"))
        if self.attempts >= self.max_items or identity in self._seen:
            self._issue(item, "media_budget_exhausted")
            return
        self._seen.add(identity)
        self.attempts += 1
        try:
            with tempfile.TemporaryDirectory(prefix="alpha-guard-video-") as directory:
                path = Path(directory) / "audio.webm"
                self.stage("capturing")
                receipt = capture_audio(page, path, max_duration_seconds=self.max_seconds,
                                        cancelled=self.cancelled)
                check_cancelled(self.cancelled)
                if receipt.get("capture_complete") is not True:
                    self._issue(item, str(receipt.get("detail") or "media_capture_incomplete"))
                    return
                capture = CapturedMedia(path=path, source_url=str(item["source_url"]),
                    external_id=str(item["external_id"]), capture_complete=True,
                    expected_duration_seconds=receipt["expected_duration_seconds"],
                    start_seconds=receipt.get("start_seconds", 0))
                self.stage("transcribing")
                result = transcribe_captured_media(capture, item, command=self.command,
                    cancelled=self.cancelled, budget=self.budget)
                check_cancelled(self.cancelled)
                if result.get("status") == "success" and result.get("item"):
                    item.update(result["item"])
                    # Make the expensive complete transcript durable immediately;
                    # later source failures cannot discard it or force re-recording.
                    with ResearchStore(self.path) as store:
                        store.upsert_evidence(item)
                else:
                    self._issue(item, str(result.get("detail") or "media_transcription_failed"))
        finally:
            self.stage("collecting")
