from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from typer.testing import CliRunner

import main

runner = CliRunner()


def test_research_commands_offline_roundtrip_and_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'STATE_PATH', tmp_path / 'state.db')
    status = runner.invoke(main.app, ['research', 'status'])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)['runtime']['state'] == 'STOPPED'
    config = tmp_path / 'research.json'
    config.write_text(json.dumps({'markets': ['CN', 'HK', 'US'], 'horizons': ['short', 'swing', 'long'],
                                  'news_enabled': False, 'ai_enabled': False}))
    configured = runner.invoke(main.app, ['research', 'configure', str(config)])
    assert configured.exit_code == 0, configured.output
    scanned = runner.invoke(main.app, ['research', 'scan'])
    assert scanned.exit_code == 0, scanned.output
    state = json.loads(scanned.output)
    assert state['state'] == 'STOPPED'
    assert state['last_run']['status'] == 'disabled'


def test_research_cli_replay_has_no_fake_metrics_and_rejects_naive_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'STATE_PATH', tmp_path / 'state.db')
    sample = tmp_path / 'prices.json'
    sample.write_text('[]')
    result = runner.invoke(main.app, ['research', 'replay', str(sample), '--as-of', '2026-09-30T00:00:00+00:00'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['summary_by_market_horizon'] == []
    invalid = runner.invoke(main.app, ['research', 'replay', str(sample), '--as-of', '2026-09-30T00:00:00'])
    assert invalid.exit_code == 2


def test_research_cli_bad_settings_are_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'STATE_PATH', tmp_path / 'state.db')
    config = tmp_path / 'bad.json'
    config.write_text(json.dumps({'api_key': 'private-test-secret'}))
    result = runner.invoke(main.app, ['research', 'configure', str(config)])
    assert result.exit_code == 2
    assert 'private-test-secret' not in result.output
    assert not (tmp_path / 'state.db').exists()


def test_digest_cli_process_waits_for_persisted_result(tmp_path):
    database = tmp_path / "digest.db"
    script = '''
import sys, time
from pathlib import Path
from types import SimpleNamespace
import research.cli as cli
import research.digest as digest
cli._service = lambda: SimpleNamespace(path=Path(sys.argv[1]))
def collect(*_args):
    time.sleep(0.1)
    return [], []
digest._default_collector = collect
cli.app(args=["digest-run"])
'''
    result = subprocess.run([sys.executable, "-c", script, str(database)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record["accepted"] and record["status"] == "completed"
    from research.store import ResearchStore

    with ResearchStore(database) as store:
        assert store.get_digest(record["id"])["status"] == "completed"


def test_digest_cli_busy_returns_failure_without_collection(tmp_path, monkeypatch):
    from research.locking import ResearchLease

    database = tmp_path / "digest.db"
    monkeypatch.setattr(main, "STATE_PATH", database)
    monkeypatch.setattr("research.digest._default_collector",
                        lambda *_: (_ for _ in ()).throw(AssertionError("must not collect")))
    with ResearchLease(database):
        result = runner.invoke(main.app, ["research", "digest-run"])
    assert result.exit_code == 1
    assert json.loads(result.output)["detail"] == "research_busy"


def _progress_cli_service(tmp_path, monkeypatch, *, count=1, ai=False, analyzer=None):
    import socket

    import pytest

    from config import Settings
    from research.models import evidence_from_item
    from research.progress import ProgressStore
    from research.service import ResearchService
    from research.store import ResearchStore

    environment = Settings.model_construct()
    monkeypatch.setattr("config.get_settings", lambda: environment)
    monkeypatch.setattr("research.analysis.get_settings", lambda: environment)
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("unexpected network"))
    service = ResearchService(tmp_path / "progress.db", analyzer=analyzer)
    service.configure({"news_enabled": False, "ai_enabled": ai, "max_ai_calls": 10,
                       "horizons": ["swing"], "instruments": [{"market": "US", "ticker": "AAPL"}]})
    with ResearchStore(service.path) as store:
        for index in range(count):
            store.upsert_evidence(evidence_from_item({
                "source_url": f"https://example.test/{index}", "title": f"Material {index}",
                "content_text": "The company disclosed its recurring revenue performance.",
                "content_kind": "article", "completeness": "full"}))
    options = service._options()
    with ProgressStore(service.path) as store:
        run_id = store.create_run(options.model_dump(mode="json"), service._progress_signature(options), "once")
        store.freeze(run_id, store.list_current_evidence(), {}, [])
        store.finish_run(run_id, "partial")
    monkeypatch.setattr("research.cli._service", lambda: service)
    return service, run_id


def test_progress_cli_reads_bounded_pages_without_starting_work(tmp_path, monkeypatch):
    import pytest

    service, run_id = _progress_cli_service(tmp_path, monkeypatch, count=21)
    closed = []
    close = service.close
    monkeypatch.setattr(service, "close", lambda: (closed.append(True), close()))
    for method in ("resume", "launch", "_analyze_batch"):
        monkeypatch.setattr(service, method, lambda *a, **kw: pytest.fail("read started work"))
    listed = runner.invoke(main.app, ["research", "runs"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)["items"][0]["run_id"] == run_id
    first = runner.invoke(main.app, ["research", "run-show", run_id])
    assert first.exit_code == 0, first.output
    first_page = json.loads(first.output)
    assert len(first_page["items"]) == 20 and first_page["next_offset"] == 20
    assert first_page["run"]["pending"] == 21
    second = runner.invoke(main.app, ["research", "run-show", run_id, "--offset", "20"])
    assert second.exit_code == 0, second.output
    assert len(json.loads(second.output)["items"]) == 1
    assert len(closed) == 3
    assert runner.invoke(main.app, ["research", "run-show", "invalid"]).exit_code == 2
    assert runner.invoke(main.app, ["research", "runs", "--offset", "-1"]).exit_code == 2
    assert runner.invoke(main.app, ["research", "run-show", run_id, "--offset", "1000001"]).exit_code == 2


def test_progress_cli_requires_uncertain_ack_then_waits_for_saved_report(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from research.models import ResearchInstrument, ResearchReport
    from research.progress import PersistentBudget, ProgressStore
    from research.store import ResearchStore

    calls = []
    def analyze(material, *args, **kwargs):
        calls.append(material.id)
        assert kwargs["persist_reports"] is False
        assert kwargs["budget"].consume()
        now = datetime.now(UTC)
        return [ResearchReport(id="cli-" + material.id, evidence_id=material.id,
            evidence_hash=material.content_hash, instrument=ResearchInstrument(market="US", ticker="AAPL"),
            horizon="swing", action="watch", status="ready", extraction_status="analyzed", summary="Fixture",
            created_at=now, valid_until=now + timedelta(days=1), analyzer_version="fixture")]
    service, run_id = _progress_cli_service(tmp_path, monkeypatch, ai=True, analyzer=analyze)
    with ProgressStore(service.path) as store:
        store.begin_resume(run_id, ack_uncertain=False)
        item = store.items(run_id)[0]
        store.begin_item(run_id, item["id"])
        budget = PersistentBudget(store, run_id)
        budget.item_id = item["id"]
        assert budget.consume()
        store.recover()
    rejected = runner.invoke(main.app, ["research", "resume", run_id])
    assert rejected.exit_code == 2
    assert json.loads(rejected.stdout)["result"]["status"] == "ack_required"
    assert "--ack-uncertain" in rejected.stderr and "额外调用" in rejected.stderr
    assert calls == []
    result = runner.invoke(main.app, ["research", "resume", run_id, "--ack-uncertain"])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["run"]["status"] == "completed"
    assert receipt["run"]["model_calls"] == 2 and receipt["run"]["completed"] == 1
    assert len(calls) == 1 and not service._thread.is_alive()
    with ResearchStore(service.path) as store:
        assert len(store.historical_reports()) == 1


def test_progress_cli_failure_is_nonzero_and_provider_details_are_not_printed(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("fixture-private-provider-detail")
    service, run_id = _progress_cli_service(tmp_path, monkeypatch, analyzer=fail)
    result = runner.invoke(main.app, ["research", "resume", run_id])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["run"]["failed"] == 1
    assert "fixture-private-provider-detail" not in result.output
    assert not service._thread.is_alive()


def test_progress_cli_incompatible_run_does_not_invoke_analyzer(tmp_path, monkeypatch):
    import pytest

    service, run_id = _progress_cli_service(tmp_path, monkeypatch,
        analyzer=lambda *a, **kw: pytest.fail("incompatible resume started"))
    service.configure({"horizons": ["long"]})
    result = runner.invoke(main.app, ["research", "resume", run_id])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["result"]["reason"] == "configuration_changed"
    assert service._thread is None


def test_progress_cli_interrupt_stops_waits_and_closes(monkeypatch):
    from types import SimpleNamespace

    events = []
    def wait():
        events.append("wait")
        if events.count("wait") == 1:
            raise KeyboardInterrupt()
    service = SimpleNamespace(
        resume=lambda run_id, ack_uncertain=False: events.append(("resume", ack_uncertain)) or {},
        wait=wait, stop=lambda: events.append("stop"), close=lambda: events.append("close"),
        run_detail=lambda run_id: {"run": {"status": "cancelled", "pending": 1}, "items": []},
        snapshot=lambda: {"runtime": {"last_run": {"status": "cancelled"}}},
    )
    monkeypatch.setattr("research.cli._service", lambda: service)
    result = runner.invoke(main.app, ["research", "resume", "a" * 32])
    assert result.exit_code == 130
    assert events == [("resume", False), "wait", "stop", "wait", "close"]
    assert json.loads(result.stdout)["run"]["status"] == "cancelled"


def test_progress_cli_process_exits_only_after_report_and_progress_are_saved(tmp_path):
    database = tmp_path / "progress-process.db"
    script = '''
import sys, time
from datetime import UTC, datetime, timedelta
from pathlib import Path
import config
import research.cli as cli
from research.models import ResearchInstrument, ResearchReport
from research.progress import ProgressStore
from research.service import ResearchService
from research.store import ResearchStore
config.get_settings = lambda: config.Settings.model_construct()
def analyze(material, *args, **kwargs):
    time.sleep(0.15)
    now = datetime.now(UTC)
    return [ResearchReport(id="subprocess-report", evidence_id=material.id, evidence_hash=material.content_hash,
        instrument=ResearchInstrument(market="US", ticker="AAPL"), horizon="swing", action="watch",
        status="ready", extraction_status="analyzed", summary="Fixture", created_at=now,
        valid_until=now + timedelta(days=1), analyzer_version="fixture")]
service = ResearchService(Path(sys.argv[1]), analyzer=analyze)
service.configure({"news_enabled":False,"ai_enabled":False,"horizons":["swing"],
                   "instruments":[{"market":"US","ticker":"AAPL"}]})
with ResearchStore(service.path) as store:
    store.upsert_evidence({"source_url":"https://example.test/source","title":"Fixture",
        "content_text":"The company disclosed recurring revenue in its latest statement.",
        "content_kind":"article","completeness":"full"})
options = service._options()
with ProgressStore(service.path) as store:
    run_id = store.create_run(options.model_dump(mode="json"), service._progress_signature(options), "once")
    store.freeze(run_id, store.list_current_evidence(), {}, [])
    store.finish_run(run_id, "partial")
cli._service = lambda: service
cli.app(args=["resume",run_id])
'''
    process = subprocess.run([sys.executable, "-c", script, str(database)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["run"]["completed"] == 1
    from research.progress import ProgressStore

    with ProgressStore(database) as store:
        assert store.get_report("subprocess-report") is not None
        assert store.load_run(result["run"]["run_id"])["status"] == "completed"


def test_progress_cli_completed_items_do_not_hide_partial_analysis(monkeypatch):
    from types import SimpleNamespace

    closed = []
    service = SimpleNamespace(
        resume=lambda *args, **kwargs: {}, wait=lambda: None, close=lambda: closed.append(True),
        run_detail=lambda run_id: {"run": {"status": "completed", "completed": 1}, "items": []},
        snapshot=lambda: {"runtime": {"last_run": {"status": "partial"}}},
    )
    monkeypatch.setattr("research.cli._service", lambda: service)
    result = runner.invoke(main.app, ["research", "resume", "a" * 32])
    assert result.exit_code == 1 and closed == [True]
    assert json.loads(result.stdout)["last_run"]["status"] == "partial"
