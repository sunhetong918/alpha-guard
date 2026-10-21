from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from research.digest import (
    DigestScheduler, generate_digest, match_and_rank, next_run_at,
    submit_manual_digest, _ai_section_summaries,
)
from research.locking import ResearchLease
from research.service import ResearchService
from research.store import ResearchStore


@pytest.fixture(autouse=True)
def offline_digest_sources(monkeypatch):
    monkeypatch.setattr("research.digest._default_collector", lambda *_: ([], []))


def test_digest_settings_validation():
    from research.models import DigestSettings

    assert DigestSettings().enabled is False
    assert DigestSettings(time_local="23:59").time_local == "23:59"
    for bad in ("7:00", "24:00", "08:60", "0800", None):
        with pytest.raises(ValueError):
            DigestSettings(time_local=bad)
    with pytest.raises(ValueError):
        DigestSettings(topics=[{"label": "半导体", "keywords": ["芯片"]},
                               {"label": "半导体", "keywords": ["晶圆"]}])
    with pytest.raises(ValueError):
        DigestSettings(topics=[{"label": "空", "keywords": []}])
    with pytest.raises(ValueError):
        DigestSettings(topics=[{"label": f"主题{index}", "keywords": ["词"]}
                               for index in range(21)])


def test_next_run_at_uses_local_time_and_wraps_a_day():
    from research.models import parse_time

    now = datetime(2026, 10, 2, 23, 30, tzinfo=UTC)
    local = now.astimezone()
    for minute in (40, 20):
        expected = local.replace(hour=23, minute=minute, second=0, microsecond=0)
        if expected <= local:
            expected += timedelta(days=1)
        assert next_run_at(f"23:{minute:02d}", now=now) == expected.astimezone(UTC).isoformat()
        assert parse_time(next_run_at(f"23:{minute:02d}", now=now)) > now


def _items():
    return [
        {"id": "1", "title": "苹果发布新品 AAPL", "summary": "", "content_text": "",
         "source_url": "https://e.example.com/1", "platform": "news",
         "published_at": datetime.now(UTC).isoformat(), "source_cluster": "c1"},
        {"id": "2", "title": "半导体产业链涨价", "summary": "芯片与光刻机供应", "content_text": "",
         "source_url": "https://e.example.com/2", "platform": "news",
         "published_at": datetime.now(UTC).isoformat(), "source_cluster": "c1"},
        {"id": "3", "title": "无关的体育新闻 AAPLX", "summary": "", "content_text": "",
         "source_url": "https://e.example.com/3", "platform": "news",
         "published_at": datetime.now(UTC).isoformat(), "source_cluster": "c3"},
        {"id": "4", "title": "腾讯业绩说明会", "summary": "", "content_text": "",
         "source_url": "https://e.example.com/4", "platform": "news",
         "published_at": (datetime.now(UTC) - timedelta(hours=48)).isoformat(),
         "source_cluster": "c4"},
    ]


def test_match_and_rank_is_deterministic_and_honest():
    instruments = [{"market": "US", "ticker": "AAPL", "name": "苹果"},
                   {"market": "HK", "ticker": "00700", "name": "腾讯"},
                   {"market": "US", "ticker": "TSLA", "name": "特斯拉"}]
    topics = [{"label": "半导体", "keywords": ["芯片", "光刻"]}]
    items = _items()
    first = match_and_rank(items, instruments, topics)
    second = match_and_rank(items, instruments, topics)
    assert first == second
    sections = {section["key"]: section for section in first["sections"]}
    assert {entry["evidence_id"] for entry in sections["US:AAPL"]["entries"]} == {"1"}
    assert {entry["evidence_id"] for entry in sections["topic:半导体"]["entries"]} == {"2"}
    assert {entry["evidence_id"] for entry in sections["HK:00700"]["entries"]} == {"4"}
    # Ticker boundary: AAPLX must not match AAPL; empty groups stay visible.
    assert all(entry["evidence_id"] != "3" for entry in first["top_items"])
    assert sections["US:TSLA"]["entries"] == []
    assert first["matched"] == 3
    scores = [entry["score"] for entry in first["top_items"]]
    assert scores == sorted(scores, reverse=True)


def test_ai_summary_gate_rejects_unsupported_numbers_and_missing_keys():
    sections = [{"key": "US:AAPL", "kind": "instrument", "entries": [
        {"evidence_id": "apple-proof", "title": "苹果营收 100 亿美元创新高"}]}]

    def good(_prompt):
        return json.dumps({"sections": [{"key": "US:AAPL", "citations": [
            {"evidence_id": "apple-proof", "quote": "苹果营收 100 亿美元创新高"}]}]})

    outcome = _ai_section_summaries(sections, model_text=good)
    assert outcome["status"] == "generated" and outcome["sections"]["US:AAPL"].endswith("创新高")

    def invented(_prompt):
        return json.dumps({"sections": [{"key": "US:AAPL", "citations": [
            {"evidence_id": "apple-proof", "quote": "营收达到 200 亿美元"}]}]})

    assert _ai_section_summaries(sections, model_text=invented)["status"] == "rejected"

    def missing(_prompt):
        return json.dumps({"sections": []})

    assert _ai_section_summaries(sections, model_text=missing)["status"] == "rejected"

    def broken(_prompt):
        return "not-json"

    assert _ai_section_summaries(sections, model_text=broken)["status"] == "rejected"
    assert _ai_section_summaries([], model_text=good)["status"] == "disabled"


def _collector(items):
    def collect(_database, _digest, _research, _cancelled):
        return list(items), [{"label": "测试源", "status": "success", "fetched": len(items)}]
    return collect


def test_generate_digest_persists_bounded_record(tmp_path):
    database = tmp_path / "state.db"
    with ResearchStore(database) as store:
        store.save_settings({**store.load_settings(), "instruments": [
            {"market": "US", "ticker": "AAPL", "name": "苹果"}]})
    settings = {"enabled": True, "time_local": "08:00",
                "topics": [{"label": "半导体", "keywords": ["芯片"]}],
                "ai_summary": False, "notify": False, "max_items": 5}
    from research.models import DigestSettings

    record = generate_digest(database, settings_override=DigestSettings.model_validate(settings),
                             collector=_collector(_items()[:2]))
    assert record["status"] == "completed" and record["counts"]["matched"] == 2
    assert record["delivery"] == {"enabled": False, "status": "skipped", "detail": ""}
    with ResearchStore(database) as store:
        stored = store.get_digest(record["id"])
        assert stored["counts"]["evidence_window"] >= 2
        assert {section["key"] for section in stored["sections"]} >= {"US:AAPL", "topic:半导体"}
        # A second run dedupes the window via source heads; identical content
        # does not double the sections.
        again = generate_digest(database, settings_override=DigestSettings.model_validate(settings),
                                collector=_collector(_items()[:2]), now=datetime.now(UTC) + timedelta(seconds=5))
        assert again["counts"]["evidence_window"] == stored["counts"]["evidence_window"]
        assert "watchlist_groups_without_items" not in again["coverage_gaps"]


def test_generate_digest_ai_gate_and_notify_offline(tmp_path):
    database = tmp_path / "state.db"
    settings = {"enabled": False, "time_local": "08:00",
                "topics": [{"label": "半导体", "keywords": ["芯片"]}],
                "ai_summary": True, "notify": True, "max_items": 5}

    def bad_numbers(_prompt):
        return json.dumps({"sections": [{"key": "topic:半导体", "summary": "规模 500 亿"}]})

    delivered = []

    def deliver(_database, record):
        delivered.append(record)
        return {"enabled": True, "status": "sent", "detail": ""}

    record = generate_digest(database, settings_override=__import__(
        "research.models", fromlist=["DigestSettings"]).DigestSettings.model_validate(settings),
        collector=_collector(_items()[:2]), model_text=bad_numbers, deliver=deliver)
    assert record["ai_summary"]["status"] == "rejected"
    assert record["ai_summary"]["detail"] in {"unsupported_numbers", "invalid_model_output",
                                              "incomplete_model_output"}
    assert delivered and delivered[0]["id"] == record["id"]
    with ResearchStore(database) as store:
        assert store.get_digest(record["id"])["delivery"]["status"] == "sent"


def test_scheduler_resumes_only_when_enabled_and_stops_cleanly(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    with ResearchStore(database) as store:
        store.save_digest_settings({"enabled": False, "time_local": "08:00"})
    scheduler = DigestScheduler(database, runner=lambda trigger: pytest.fail("must not run"))
    assert scheduler.start_if_enabled() is False
    assert scheduler.state == "STOPPED"

    runs = []
    scheduler = DigestScheduler(database, runner=lambda trigger: runs.append(trigger))
    with ResearchStore(database) as store:
        store.save_digest_settings({"enabled": True, "time_local": "08:00"})
    # Pin the next occurrence one second out so the loop fires promptly.
    monkeypatch.setattr("research.digest.next_run_at",
                        lambda time_local, now=None: (datetime.now(UTC) + timedelta(seconds=1)).isoformat())
    assert scheduler.start_if_enabled() is True
    deadline = time.monotonic() + 6
    while not runs and time.monotonic() < deadline:
        time.sleep(0.05)
    scheduler.stop()
    assert runs == ["schedule"] and scheduler.state == "STOPPED"


def test_manual_digest_single_flight_and_busy_recording(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    gate = threading.Event()

    def collector(_database, _digest, _research, _cancelled):
        gate.wait(5)
        return [], []

    monkeypatch.setattr("research.digest._default_collector", collector)
    first = submit_manual_digest(database)
    assert first["accepted"] is True
    duplicate = submit_manual_digest(database)
    assert duplicate["accepted"] is False and duplicate["detail"] == "digest_already_running"
    gate.set()
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        with ResearchStore(database) as store:
            if store.list_digests(limit=1):
                break
        time.sleep(0.05)
    with ResearchStore(database) as store:
        record = store.list_digests(limit=1)[0]
    assert record["status"] == "completed" and record["trigger"] == "manual"

    # A held research lease turns the manual run into an honest busy record.
    with ResearchLease(database):
        gate.clear()
        assert submit_manual_digest(database)["accepted"] is True
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            with ResearchStore(database) as store:
                records = store.list_digests(limit=1)
                if records and records[0]["status"] == "failed":
                    break
            time.sleep(0.05)
    assert records[0]["detail"] == "research_busy"


def test_digest_snapshot_and_service_wiring(tmp_path):
    service = ResearchService(tmp_path / "state.db")
    saved = service.save_digest_settings({"enabled": True, "time_local": "09:30",
                                          "topics": [{"label": "医药", "keywords": ["临床"]}]})
    assert saved["enabled"] is True and saved["time_local"] == "09:30"

    class _FakeScheduler:
        restarts = 0
        state = "RUNNING"
        next_run_at = "2026-10-03T01:30:00+00:00"

        def restart(self):
            self.restarts += 1

    service.digest_scheduler = _FakeScheduler()
    service.save_digest_settings({"enabled": True, "time_local": "10:00"})
    assert service.digest_scheduler.restarts == 1

    from research.digest import digest_snapshot

    block = digest_snapshot(service.path)
    assert block["settings"]["time_local"] == "10:00"
    assert block["plan"]["enabled"] is True and block["plan"]["next_run_at"]
    assert block["latest"] is None and block["history"] == []
    snapshot = service.snapshot()
    assert snapshot["digest"]["plan"]["enabled"] is True
    receipt = service.run_digest_once()
    assert receipt["accepted"] is True
    with ResearchStore(service.path) as store:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if store.list_digests(limit=1):
                break
            time.sleep(0.05)
    assert snapshot["capabilities"].get("model_profile_active") is None


def test_ipc_digest_commands(tmp_path):
    from research.ipc import execute

    service = ResearchService(tmp_path / "state.db")
    with pytest.raises(ValueError):
        execute(service, {"command": "digest_save"})
    result = execute(service, {"command": "digest_save", "settings": {
        "enabled": True, "time_local": "08:15", "topics": [{"label": "能源", "keywords": ["油价"]}]}})
    assert result["result"]["enabled"] is True
    with ResearchStore(service.path) as store:
        assert store.load_digest_settings()["time_local"] == "08:15"
    assert execute(service, {"command": "digest_once"})["result"]["accepted"] is True
