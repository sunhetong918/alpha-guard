from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from research import digest as module
from research.digest import DigestScheduler, generate_digest, match_and_rank
from research.locking import ResearchLease
from research.models import DigestSettings
from research.store import ResearchStore


def _sections():
    return [{"key": "US:AAPL", "kind": "instrument", "entries": [
        {"evidence_id": "apple", "title": "苹果否认营收下滑 10%，未宣布收购"},
        {"evidence_id": "tesla", "title": "特斯拉营收下滑 20%"},
    ]}]


def _model_output(citations):
    return json.dumps({"sections": [{"key": "US:AAPL", "citations": citations}]})


def test_ai_excerpts_preserve_complete_context_and_citations():
    references = [{"evidence_id": "apple", "quote": _sections()[0]["entries"][0]["title"]}]
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return _model_output(references)

    outcome = module._ai_section_summaries(_sections(), model_text=model)
    assert outcome["status"] == "generated"
    assert outcome["sections"]["US:AAPL"] == references[0]["quote"]
    assert outcome["citations"]["US:AAPL"] == references
    assert len(prompts) == 1
    assert "apple" in prompts[0]


@pytest.mark.parametrize("references", [
    [{"evidence_id": "apple", "quote": "苹果营收下滑 10%"}],  # dropped negation
    [{"evidence_id": "apple", "quote": "苹果否认营收下滑 20%，未宣布收购"}],  # another source's number
    [{"evidence_id": "apple", "quote": "特斯拉营收下滑 20%"}],  # wrong source
    [{"evidence_id": "missing", "quote": "特斯拉营收下滑 20%"}],
    [{"evidence_id": "apple", "quote": "特斯拉宣布收购英伟达"}],
    [{"evidence_id": "tesla", "quote": "特斯拉营收下滑 20%"}] * 2,
    [],
])
def test_ai_excerpts_reject_unsupported_statements(references):
    outcome = module._ai_section_summaries(_sections(), model_text=lambda _: _model_output(references))
    assert outcome["status"] == "rejected"
    assert outcome["sections"] == {}


def test_ai_output_size_and_freeform_prose_are_rejected():
    for output in ("x" * (module.MAX_AI_SUMMARY_CHARS + 1),
                   json.dumps({"sections": [{"key": "US:AAPL", "summary": "苹果宣布收购"}]})):
        assert module._ai_section_summaries(_sections(), model_text=lambda _: output)["status"] == "rejected"


@pytest.mark.parametrize("cancel_at", ["before", "collection", "model"])
def test_cancellation_blocks_later_model_and_delivery(tmp_path, cancel_at):
    stop = threading.Event()
    calls = []
    if cancel_at == "before":
        stop.set()

    def collect(*_args):
        calls.append("collect")
        if cancel_at == "collection":
            stop.set()
        return [{"source_url": "https://example.com/a", "title": "苹果新闻", "platform": "news"}], []

    def model(_prompt):
        calls.append("model")
        stop.set()
        return "{}"

    result = generate_digest(tmp_path / "state.db", settings_override=DigestSettings(
        ai_summary=True, notify=True, topics=[{"label": "苹果", "keywords": ["苹果"]}]),
        collector=collect, model_text=model,
        deliver=lambda *_: pytest.fail("cancelled batch must not deliver"), cancelled=stop.is_set)
    assert calls == {"before": [], "collection": ["collect"], "model": ["collect", "model"]}[cancel_at]
    assert result["status"] == "cancelled"
    assert result["delivery"]["status"] == "skipped"
    with ResearchStore(tmp_path / "state.db") as store:
        assert store.get_digest(result["id"])["status"] == "cancelled"


def test_provider_receives_cancellation_and_does_not_deliver(tmp_path, monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr("config.get_settings", lambda: object())
    monkeypatch.setattr("research.model_provider.model_capabilities", lambda *_: {"model_configured": True})

    def extract(_prompt, _settings, **kwargs):
        assert kwargs["cancelled"] is stop.is_set or callable(kwargs["cancelled"])
        stop.set()
        assert kwargs["cancelled"]()
        return "{}"

    monkeypatch.setattr("research.model_provider.extract_text", extract)
    result = generate_digest(tmp_path / "state.db", settings_override=DigestSettings(
        ai_summary=True, notify=True, topics=[{"label": "苹果", "keywords": ["苹果"]}]),
        collector=lambda *_: ([{"source_url": "https://example.com/a", "title": "苹果新闻"}], []),
        deliver=lambda *_: pytest.fail("cancelled provider must not deliver"), cancelled=stop.is_set)
    assert result["status"] == "cancelled"


def test_digest_is_persisted_before_delivery(tmp_path):
    def deliver(database, record):
        with ResearchStore(database) as store:
            assert store.get_digest(record["id"])["delivery"]["status"] == "pending"
        return {"enabled": True, "status": "sent", "detail": ""}

    result = generate_digest(tmp_path / "state.db", settings_override=DigestSettings(notify=True),
                             collector=lambda *_: ([], []), deliver=deliver)
    assert result["status"] == "completed"
    assert result["delivery"]["status"] == "sent"


@pytest.mark.parametrize("stop_after_first", [False, True])
def test_delivery_checks_cancel_between_channels(tmp_path, monkeypatch, stop_after_first):
    from notifier.mobile import ChannelDelivery, MobileDeliveryReport

    stop = threading.Event()
    channels = []

    class Environment:
        notifications_enabled = True
        whatsapp_enabled = True

        def model_copy(self, *, update):
            return SimpleNamespace(**update)

    monkeypatch.setattr("config.get_settings", Environment)
    monkeypatch.setattr("notifier.mobile.configured_mobile_channels", lambda settings: tuple(
        channel for channel, enabled in (("telegram", settings.notifications_enabled),
                                        ("whatsapp", settings.whatsapp_enabled)) if enabled))

    async def deliver(_store, **kwargs):
        settings = kwargs["settings"]
        channel = "telegram" if settings.notifications_enabled else "whatsapp"
        channels.append(channel)
        assert len(kwargs["payload"]) <= module.MAX_DELIVERY_CHARS
        if stop_after_first:
            stop.set()
        return MobileDeliveryReport("test", (channel,), (ChannelDelivery(channel, True, True),))

    monkeypatch.setattr("notifier.mobile.deliver_mobile", deliver)
    result = module._deliver(tmp_path / "state.db", {"id": "test", "sections": []},
                             date_label="2026-10-15", cancelled=stop.is_set)
    assert channels == (["telegram"] if stop_after_first else ["telegram", "whatsapp"])
    assert result["status"] == ("cancelled" if stop_after_first else "accepted")


def test_settings_changed_in_another_session_cancels_batch(tmp_path):
    database = tmp_path / "state.db"
    with ResearchStore(database) as store:
        store.save_digest_settings({"enabled": True, "time_local": "08:00", "notify": True})

    def collect(*_args):
        with ResearchStore(database) as other:
            other.save_digest_settings({"enabled": False, "time_local": "08:00", "notify": False})
        return [], []

    result = generate_digest(database, trigger="schedule", collector=collect,
                             deliver=lambda *_: pytest.fail("revoked schedule must not deliver"))
    assert result["status"] == "cancelled"


def test_disabled_saved_schedule_does_not_collect(tmp_path):
    result = generate_digest(tmp_path / "state.db", trigger="schedule",
                             collector=lambda *_: pytest.fail("disabled schedule must not collect"))
    assert result["status"] == "cancelled"


def test_same_score_sorts_newest_then_evidence_id_with_timezone_normalization():
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    items = [{"id": "old", "title": "AAPL", "published_at": "2026-10-15T09:00:00+00:00"},
             {"id": "b", "title": "AAPL", "published_at": "2026-10-15T18:00:00+08:00"},
             {"id": "a", "title": "AAPL", "published_at": "2026-10-15T10:00:00+00:00"}]
    result = match_and_rank(items, [{"market": "US", "ticker": "AAPL"}], [], now=now)
    assert [item["evidence_id"] for item in result["top_items"]] == ["a", "b", "old"]
    assert result["sections"][0]["entries"] == result["top_items"]


@pytest.mark.parametrize("hours_since_previous, expected_hours", [(1, 24), (72, 72)])
def test_digest_window_is_wider_of_day_and_last_success(tmp_path, hours_since_previous, expected_hours):
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    with ResearchStore(tmp_path / "state.db") as store:
        store.save_digest_record({"id": "previous", "created_at": (now - timedelta(hours=hours_since_previous)).isoformat(),
                                  "status": "completed"})
        store.save_digest_record({"id": "failure", "created_at": (now - timedelta(minutes=5)).isoformat(),
                                  "status": "failed"})
    result = generate_digest(tmp_path / "state.db", collector=lambda *_: ([], []), now=now)
    assert result["window_start"] == (now - timedelta(hours=expected_hours)).isoformat()


def test_scheduler_keeps_stopping_worker_and_restarts_only_after_it_exits(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    with ResearchStore(database) as store:
        store.save_digest_settings({"enabled": True, "time_local": "08:00"})
    entered, release = threading.Event(), threading.Event()
    targets = []

    def next_target(*_args):
        targets.append(True)
        return (datetime.now(UTC) + timedelta(seconds=0.02 if len(targets) == 1 else 3600)).isoformat()

    monkeypatch.setattr(module, "next_run_at", next_target)
    scheduler = DigestScheduler(database, runner=lambda _: (entered.set(), release.wait(3)))
    assert scheduler.start_if_enabled()
    assert entered.wait(2)
    old_thread, old_event = scheduler._thread, scheduler._stop
    real_stop = scheduler.stop
    monkeypatch.setattr(scheduler, "stop", lambda timeout=5: real_stop(timeout=0.001))
    try:
        scheduler.stop()
        assert scheduler.state == "STOPPING"
        assert scheduler._thread is old_thread and old_thread.is_alive()
        assert not scheduler.start_if_enabled()
        assert scheduler.restart()
        assert scheduler._thread is old_thread and old_event.is_set()
        assert len(targets) == 1
        release.set()
        old_thread.join(2)
        deadline = time.monotonic() + 2
        while len(targets) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(targets) == 2
        assert scheduler._thread is not old_thread
        assert scheduler._stop is not old_event and old_event.is_set()
        assert scheduler.state == "RUNNING"
    finally:
        release.set()
        real_stop(timeout=2)
    assert scheduler.state == "STOPPED"


@pytest.mark.parametrize("manual", [True, False])
def test_scheduler_cancels_default_runner_and_manual_batch(tmp_path, monkeypatch, manual):
    database = tmp_path / "state.db"
    entered, release = threading.Event(), threading.Event()
    callbacks = []

    def collect(_path, _digest, _research, cancelled):
        callbacks.append(cancelled)
        entered.set()
        release.wait(2)
        assert cancelled()
        return [], []

    monkeypatch.setattr(module, "_default_collector", collect)
    scheduler = DigestScheduler(database)
    if manual:
        assert module.submit_manual_digest(database)["accepted"]
    else:
        with ResearchStore(database) as store:
            store.save_digest_settings({"enabled": True, "time_local": "08:00"})
        monkeypatch.setattr(module, "next_run_at", lambda *_:
                            (datetime.now(UTC) + timedelta(seconds=0.02)).isoformat())
        assert scheduler.start_if_enabled()
    assert entered.wait(2)
    scheduler.stop(timeout=0)
    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with ResearchStore(database) as store:
            records = store.list_digests()
        if records:
            break
        time.sleep(0.01)
    assert callbacks[0]()
    assert records[0]["status"] == "cancelled"
    scheduler.stop(timeout=2)


def test_retry_budget_is_at_most_fifteen_minutes_with_four_retries(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    clock = [0.0]
    attempts = []
    real_lease = ResearchLease

    class Event:
        def is_set(self):
            return False

        def wait(self, seconds):
            clock[0] += seconds
            return False

    def lease(path):
        attempts.append(clock[0])
        return real_lease(path)

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(module, "ResearchLease", lease)
    with real_lease(database):
        DigestScheduler(database)._attempt(Event())
    assert attempts == [0, 225, 450, 675, 900]
    with ResearchStore(database) as store:
        assert store.list_digests()[0]["detail"] == "research_busy"


def test_scheduler_records_failure_without_leaking_source_diagnostics(tmp_path):
    def fail(_trigger):
        raise RuntimeError("secret-source-diagnostic")

    scheduler = DigestScheduler(tmp_path / "state.db", runner=fail)
    scheduler._attempt()
    with ResearchStore(tmp_path / "state.db") as store:
        records = store.list_digests()
    assert records[0]["status"] == "failed"
    assert records[0]["detail"] == "digest_failed"
    assert "secret-source-diagnostic" not in json.dumps(records)
