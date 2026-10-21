from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta

import pytest

from config import Settings
from research.ipc import execute
from research.locking import ResearchLease
from research.models import ResearchInstrument, ResearchReport
from research.progress import PersistentBudget, ProgressStore, ResumeError
from research.service import ResearchService
from research.store import ResearchStore


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    environment = Settings.model_construct()
    monkeypatch.setattr("config.get_settings", lambda: environment)
    monkeypatch.setattr("research.analysis.get_settings", lambda: environment)
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("unexpected network"))


def setup_service(tmp_path, *, count=3, ai=False, analyzer=None, **kwargs):
    service = ResearchService(tmp_path / "research.db", analyzer=analyzer, **kwargs)
    service.configure({"news_enabled": False, "ai_enabled": ai, "max_ai_calls": 10,
                       "horizons": ["swing"], "instruments": [{"market": "US", "ticker": "AAPL"}]})
    with ResearchStore(service.path) as store:
        for index in range(count):
            store.upsert_evidence({"source_url": f"https://example.org/{index}", "platform": "news",
                "title": f"Material {index}", "content_text": f"The company disclosed detailed revenue performance {index}.",
                "content_kind": "article", "completeness": "full"})
    return service


def report_for(material, *, suffix="", extraction="analyzed"):
    now = datetime.now(UTC)
    return ResearchReport(id=material.id + suffix, evidence_id=material.id, evidence_hash=material.content_hash,
        instrument=ResearchInstrument(market="US", ticker="AAPL"), horizon="swing", action="watch",
        status="ready", extraction_status=extraction, summary="Source-backed observation",
        created_at=now, valid_until=now + timedelta(days=1), analyzer_version="test")


def freeze(service, *, mode="once", contexts=None):
    options = service._options()
    with ProgressStore(service.path) as store:
        identity = store.create_run(options.model_dump(mode="json"), service._progress_signature(options), mode)
        store.freeze(identity, store.list_current_evidence(), contexts or {}, [])
        return identity


def test_cancel_then_restart_only_resumes_remaining_items_without_recollecting(tmp_path):
    entered, release = threading.Event(), threading.Event()
    seen = []
    def analyze(material, *args, **kwargs):
        assert kwargs["persist_reports"] is False
        seen.append(material.id)
        if len(seen) == 2:
            entered.set()
            assert release.wait(4)
        return [report_for(material)]
    service = setup_service(tmp_path, analyzer=analyze)
    run_id = service.launch()["run_id"]
    try:
        assert entered.wait(4)
        during = service.run_detail(run_id)
        assert during["run"]["completed"] == 1
        assert during["run"]["running"] == 1
        service.stop()
    finally:
        release.set()
        service.wait(4)
    assert len(seen) == 2
    resumed = ResearchService(service.path, analyzer=analyze,
        market_loader=lambda *a, **kw: pytest.fail("resume reloaded quote"),
        collect_news=lambda *a, **kw: pytest.fail("resume recollected"),
        discoverer=lambda *a, **kw: pytest.fail("resume rediscovered"))
    assert resumed.run_detail(run_id)["run"]["resumable"] is True
    assert resumed.resume(run_id)["mode"] == "resume"
    resumed.wait(4)
    assert seen.count(seen[0]) == 1
    assert len(seen) == 4
    assert resumed.run_detail(run_id)["run"]["completed"] == 3
    with ResearchStore(service.path) as store:
        assert len(store.historical_reports()) == 3


def test_restart_marks_inflight_call_uncertain_and_retains_spent_budget(tmp_path):
    service = setup_service(tmp_path, count=2, ai=True)
    run_id = freeze(service)
    with ProgressStore(service.path) as store:
        item = store.items(run_id)[0]
        store.begin_item(run_id, item["id"])
        budget = PersistentBudget(store, run_id)
        budget.item_id = item["id"]
        assert budget.consume()
        # A separate connection can observe the charge before dispatch/return.
        with sqlite3.connect(service.path) as other:
            assert other.execute("SELECT model_calls FROM research_scan_runs WHERE id=?", (run_id,)).fetchone()[0] == 1
    calls = []
    def analyze(material, *args, **kwargs):
        calls.append(kwargs["budget"].used_calls)
        assert kwargs["budget"].consume()
        return [report_for(material)]
    restarted = ResearchService(service.path, analyzer=analyze)
    detail = restarted.run_detail(run_id)
    assert detail["run"]["status"] == "interrupted"
    assert detail["run"]["uncertain"] == 1
    assert detail["run"]["model_calls"] == 1
    with pytest.raises(ResumeError, match="uncertain_model_call"):
        restarted.resume(run_id)
    assert calls == []
    restarted.resume(run_id, ack_uncertain=True)
    restarted.wait(4)
    assert calls == [1, 2]
    assert restarted.run_detail(run_id)["run"]["model_calls"] == 3


def test_unavailable_model_does_not_complete_material_and_can_resume_after_setup(tmp_path, monkeypatch):
    monkeypatch.setattr("research.model_provider.model_capabilities", lambda *_: {"model_configured": False})
    service = setup_service(tmp_path, count=1, ai=True, market_loader=lambda *a, **kw: {})
    run_id = service.launch()["run_id"]
    service.wait(4)
    first = service.run_detail(run_id)
    assert first["run"]["completed"] == 0 and first["run"]["failed"] == 1
    assert first["run"]["model_calls"] == 0 and first["run"]["resumable"] is True
    assert first["items"][0]["detail"] == "model_unavailable"
    def available(material, *args, **kwargs):
        assert kwargs["budget"].consume()
        return [report_for(material)]
    service._analyzer = available
    service.resume(run_id)
    service.wait(4)
    result = service.run_detail(run_id)
    assert result["run"]["completed"] == 1 and result["run"]["failed"] == 0
    assert result["run"]["model_calls"] == 1 and result["run"]["resumable"] is False


def test_second_window_never_interrupts_live_owner_and_cannot_resume(tmp_path):
    service = setup_service(tmp_path)
    run_id = freeze(service)
    with ResearchLease(service.path):
        other = ResearchService(service.path)
        assert other.run_detail(run_id)["run"]["status"] == "running"
        with pytest.raises(ResumeError, match="run_active"):
            other.resume(run_id)
        with pytest.raises(ValueError, match="another session"):
            other.launch()


def test_collection_interruption_requires_new_scan_without_resuming_network(tmp_path):
    service = setup_service(tmp_path)
    options = service._options()
    with ProgressStore(service.path) as store:
        run_id = store.create_run(options.model_dump(mode="json"), service._progress_signature(options), "once")
        store.stage(run_id, "reading")
    restarted = ResearchService(service.path)
    assert restarted.run_detail(run_id)["run"]["incompatible_reason"] == "collection_not_resumable"
    with pytest.raises(ResumeError, match="collection_not_resumable"):
        restarted.resume(run_id)


@pytest.mark.parametrize("change,expected", [
    ("budget", "configuration_changed"), ("instrument", "configuration_changed"),
    ("model", "configuration_changed"), ("date", "batch_date_expired"),
    ("material", "material_changed"), ("cluster", "material_changed"), ("quote", "market_snapshot_expired"),
    ("monitor", "mode_not_resumable"),
])
def test_resume_rejects_changed_inputs_before_model_work(tmp_path, monkeypatch, change, expected):
    service = setup_service(tmp_path, count=1)
    contexts = {"US:AAPL": {"price": 10, "source_as_of": (datetime.now(UTC)-timedelta(days=4)).isoformat()}} if change == "quote" else {}
    run_id = freeze(service, mode="monitor" if change == "monitor" else "once", contexts=contexts)
    with ProgressStore(service.path) as store:
        store.finish_run(run_id, "cancelled")
        if change == "date":
            with store._connection:
                store._connection.execute("UPDATE research_scan_runs SET created_at=? WHERE id=?",
                    ((datetime.now(UTC)-timedelta(days=1)).isoformat(), run_id))
        if change == "material":
            old = store.list_current_evidence()[0]
            store.upsert_evidence({**old.model_dump(mode="json"), "content_text": "Corrected company source material with a revised business statement."})
        if change == "cluster":
            old = store.list_current_evidence()[0]
            store.upsert_evidence({"source_url": "https://example.org/earlier-copy", "platform": "news",
                "title": "Earlier report", "content_text": old.content_text, "content_kind": "article",
                "completeness": "full", "published_at": (datetime.now(UTC)-timedelta(days=2)).isoformat()})
    if change == "budget":
        service.configure({"max_ai_calls": 11})
    if change == "instrument":
        service.configure({"instruments": [{"market": "US", "ticker": "MSFT"}]})
    if change == "model":
        monkeypatch.setattr(service, "_progress_signature", lambda options: "different")
    with pytest.raises(ResumeError, match=expected):
        service.resume(run_id)
    assert service.run_detail(run_id)["run"]["incompatible_reason"] == expected


def test_missing_quote_remains_missing_and_does_not_prevent_resume(tmp_path):
    contexts = []
    def analyze(material, *args, **kwargs):
        contexts.append(kwargs["market_contexts"])
        return [report_for(material)]
    service = setup_service(tmp_path, count=1, analyzer=analyze)
    run_id = freeze(service, contexts={"US:AAPL": {"quality_issues": ["market_unavailable"]}})
    with ProgressStore(service.path) as store:
        store.finish_run(run_id, "cancelled")
    service.resume(run_id)
    service.wait(4)
    assert contexts[0]["US:AAPL"]["price"] is None
    assert contexts[0]["US:AAPL"]["quality_issues"] == ["market_unavailable"]


def test_item_failure_does_not_skip_following_items_and_budget_never_resets(tmp_path):
    seen = []
    def analyze(material, *args, **kwargs):
        seen.append(material.id)
        assert kwargs["budget"].consume()
        if len(seen) == 1:
            raise TimeoutError("unsafe provider error")
        return [report_for(material)]
    service = setup_service(tmp_path, count=3, ai=True, analyzer=analyze, market_loader=lambda *a, **kw: {})
    service.configure({**service._options().model_dump(mode="json"), "max_ai_calls": 3})
    run_id = service.launch()["run_id"]
    service.wait(4)
    result = service.run_detail(run_id)
    assert len(seen) == 3
    assert result["run"]["completed"] == 2
    assert result["run"]["uncertain"] == 1
    assert result["run"]["model_calls"] == 3
    with pytest.raises(ResumeError, match="model_budget_exhausted"):
        service.resume(run_id, ack_uncertain=True)
    assert "unsafe provider" not in json.dumps(result)


def test_report_and_item_commit_rollback_together(tmp_path):
    service = setup_service(tmp_path, count=1)
    run_id = freeze(service)
    with ProgressStore(service.path) as store:
        item = store.items(run_id)[0]
        material = store.get_evidence(item["evidence_id"])
        store.begin_item(run_id, item["id"])
        store._connection.executescript("""CREATE TRIGGER fail_progress BEFORE UPDATE ON research_scan_items
            WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'simulate disk failure'); END;""")
        with pytest.raises(sqlite3.IntegrityError, match="simulate disk failure"):
            store.finish_item(run_id, item["id"], [report_for(material)], "completed")
        assert store.historical_reports() == []
        assert store.items(run_id)[0]["status"] == "running"
        store._connection.execute("DROP TRIGGER fail_progress")
        store.finish_item(run_id, item["id"], [report_for(material)], "completed")
        assert len(store.historical_reports()) == 1
        assert store.items(run_id)[0]["status"] == "completed"


def test_progress_schema_backup_precedes_additive_tables_and_is_idempotent(tmp_path):
    path = tmp_path / "existing.db"
    with ResearchStore(path) as store:
        store.save_settings({"news_enabled": False})
    with ProgressStore(path) as store:
        backup = store.backup_path
        assert backup is not None and backup.is_file()
        with sqlite3.connect(backup) as original:
            assert original.execute("SELECT 1 FROM sqlite_master WHERE name='research_scan_runs'").fetchone() is None
            assert original.execute("SELECT 1 FROM research_settings").fetchone()
    with ProgressStore(path) as store:
        assert store.backup_path is None


def test_run_detail_pages_bounded_metadata_without_material_body(tmp_path):
    service = setup_service(tmp_path, count=24)
    run_id = freeze(service)
    first = service.run_detail(run_id)
    second = service.run_detail(run_id, offset=first["next_offset"])
    assert len(first["items"]) == 20 and len(second["items"]) == 4
    assert second["next_offset"] is None
    assert all(len(item["title"]) <= 300 for item in first["items"])
    assert "content_text" not in json.dumps(first)


@pytest.mark.parametrize("params", [
    {"command": "run_list", "offset": True}, {"command": "run_list", "extra": 1},
    {"command": "run_detail", "run_id": "invalid"}, {"command": "resume", "run_id": "a"*32, "ack_uncertain": 1},
    {"command": "resume", "run_id": "a"*32, "reset_budget": True},
])
def test_progress_ipc_is_strict(tmp_path, params):
    service = setup_service(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        execute(service, params)


def test_progress_ipc_returns_explicit_uncertainty_and_incompatible_receipts(tmp_path):
    service = setup_service(tmp_path, ai=True)
    run_id = freeze(service)
    with ProgressStore(service.path) as store:
        item = store.items(run_id)[0]
        store.begin_item(run_id, item["id"])
        budget = PersistentBudget(store, run_id)
        budget.item_id = item["id"]
        budget.consume()
    restarted = ResearchService(service.path)
    response = execute(restarted, {"command": "resume", "run_id": run_id})
    assert response["accepted"] is False
    assert response["result"]["status"] == "ack_required"
    restarted.configure({"max_ai_calls": 11})
    response = execute(restarted, {"command": "resume", "run_id": run_id})
    assert response["result"]["reason"] == "configuration_changed"


def test_abrupt_worker_process_exit_is_visible_without_automatic_retry(tmp_path):
    service = setup_service(tmp_path, count=1, ai=True)
    script = """
import os, sys
import config
config.get_settings = lambda: config.Settings.model_construct()
from research.service import ResearchService
def analyze(material, *args, **kwargs):
    assert kwargs['budget'].consume()
    os._exit(23)
service = ResearchService(sys.argv[1], analyzer=analyze, market_loader=lambda *a, **kw: {})
service.launch()
service.wait(5)
raise SystemExit(99)
"""
    child = subprocess.run([sys.executable, "-c", script, str(service.path)], timeout=10,
                           capture_output=True, text=True, check=False)
    assert child.returncode == 23, child.stderr
    restarted = ResearchService(service.path, analyzer=lambda *a, **kw: pytest.fail("automatic resume"))
    run = restarted.run_list()["items"][0]
    assert run["status"] == "interrupted"
    assert run["uncertain"] == 1
    assert run["model_calls"] == 1
    assert restarted._state == "STOPPED"


def test_actual_analysis_charges_durably_before_extract_and_commits_report(tmp_path, monkeypatch):
    from research.analysis import analyze_evidence
    from research.models import ExtractionResult

    charged = []
    def extract(material, securities, horizons, runtime, client, before_call, cancelled, **kwargs):
        before_call()
        with sqlite3.connect(service.path) as observer:
            charged.append(observer.execute("SELECT model_calls FROM research_scan_runs").fetchone()[0])
            assert observer.execute("SELECT COUNT(*) FROM research_reports").fetchone()[0] == 0
        return ExtractionResult()
    monkeypatch.setattr("research.analysis._extract", extract)
    def analyze(*args, **kwargs):
        return analyze_evidence(*args, **kwargs, client=object())
    service = setup_service(tmp_path, count=1, ai=True, analyzer=analyze, market_loader=lambda *a, **kw: {})
    run_id = service.launch()["run_id"]
    service.wait(4)
    assert charged == [1]
    run = service.run_detail(run_id)
    assert run["run"]["completed"] == 1
    assert run["items"][0]["report_ids"]
    with ResearchStore(service.path) as store:
        assert len(store.historical_reports()) == 1


def test_stop_before_worker_enters_scan_does_not_leave_running_batch(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    service = setup_service(tmp_path, analyzer=lambda *a, **kw: pytest.fail("cancelled before scan"))
    original = service._worker
    def delayed_worker(*args):
        entered.set()
        assert release.wait(4)
        original(*args)
    monkeypatch.setattr(service, "_worker", delayed_worker)
    run_id = service.launch()["run_id"]
    try:
        assert entered.wait(4)
        service.stop()
    finally:
        release.set()
        service.wait(4)
    assert service.run_detail(run_id)["run"]["status"] == "cancelled"
    assert service.run_detail(run_id)["run"]["incompatible_reason"] == "collection_not_resumable"


def test_cancel_after_dispatch_requires_explicit_uncertain_ack(tmp_path):
    def analyze(material, *args, **kwargs):
        assert kwargs["budget"].consume()
        service.stop()
        return [report_for(material)]
    service = setup_service(tmp_path, count=2, ai=True, analyzer=analyze, market_loader=lambda *a, **kw: {})
    run_id = service.launch()["run_id"]
    service.wait(4)
    detail = service.run_detail(run_id)
    assert detail["run"]["uncertain"] == 1
    assert detail["run"]["pending"] == 1
    assert detail["run"]["model_calls"] == 1
    with pytest.raises(ResumeError, match="uncertain_model_call"):
        service.resume(run_id)
    with ResearchStore(service.path) as store:
        assert store.historical_reports() == []
