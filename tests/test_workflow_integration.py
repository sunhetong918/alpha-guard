"""Real authenticated dispatch for desktop workflows; isolated user data."""
from __future__ import annotations

from pathlib import Path

import pytest

from config import Settings
from guardian.application import GuardianApplication
from guardian.backend import AlphaGuardBackend
from guardian.preferences import PreferencesStore
from guardian.protocol import RpcErrorResponse, RpcRequest, RpcSuccessResponse, serialize_response
from research.store import ResearchStore

TOKEN = "w" * 43
COMMANDS = ("watchlist_get", "watchlist_save", "rules_preview", "rules_save", "review_list",
            "review_detail", "review_save", "replay_import", "replay_run", "replay_list",
            "event_list", "event_save", "event_import", "event_history", "event_refresh", "event_status", "event_stop")


@pytest.fixture
def workflows(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    rules = tmp_path / "rules.yaml"
    rules.write_bytes(Path("signals/rules.yaml").read_bytes())
    monkeypatch.setattr("guardian.backend.STATE_PATH", database)
    monkeypatch.setattr("config.RULES_CONFIG_PATH", rules)
    backend = AlphaGuardBackend(settings_loader=lambda: Settings(_env_file=None),
                                preferences_store=PreferencesStore(tmp_path / "preferences.json"))
    yield GuardianApplication(TOKEN, dispatcher=backend), database, rules
    backend.close_research()


def send(app, command, fields=None, *, token=TOKEN):
    return app.handle(RpcRequest.model_validate({"jsonrpc": "2.0", "id": "workflow",
        "method": "research.command", "params": {"command": command, **(fields or {})}, "auth_token": token}))


def result(app, command, fields=None):
    response = send(app, command, fields)
    assert isinstance(response, RpcSuccessResponse), response
    assert response.result["accepted"] is True, response.result
    assert len(serialize_response(response)) < 1_048_576
    return response.result["result"]


@pytest.mark.parametrize("command", COMMANDS)
def test_workflow_auth_precedes_data_access(workflows, command):
    app, database, rules = workflows
    original = rules.read_bytes()
    response = send(app, command, token="x" * 43)
    assert isinstance(response, RpcErrorResponse)
    assert response.error.data.kind == "unauthorized"
    assert not database.exists()
    assert rules.read_bytes() == original


@pytest.mark.parametrize("command", COMMANDS)
def test_workflow_rejects_unknown_fields_without_reflecting_values(workflows, command):
    app, _, _ = workflows
    response = send(app, command, {"unsupported": "private-test-value"})
    assert isinstance(response, RpcErrorResponse)
    assert response.error.data.kind == "invalid_params"
    assert "private-test-value" not in response.model_dump_json()


def test_watchlist_to_rule_preview_and_save_via_authenticated_ipc(workflows):
    from config import load_rules_config

    app, database, rules = workflows
    initial = result(app, "watchlist_get")
    original = load_rules_config(rules)
    following = result(app, "watchlist_save", {"market": "US", "ticker": "GOOG",
        "name": "Alphabet", "research_enabled": True, "expected_revision": initial["revision"]})
    assert any(item["key"] == "US:GOOG" and item["research_enabled"] for item in following["items"])
    configuration = {"market": "US", "currency": "USD", "name": "Alphabet", "enabled": False,
                     "buy_rules": [{"type": "price_below", "value": 100}], "sell_rules": []}
    preview = result(app, "rules_preview", {"market": "US", "ticker": "GOOG",
        "configuration": configuration, "expected_revision": following["revision"]})
    assert "GOOG" not in load_rules_config(rules).watchlist
    saved = result(app, "rules_save", {"market": "US", "ticker": "GOOG",
        "configuration": preview["configuration"], "preview_token": preview["preview_token"],
        "expected_revision": preview["revision"]})
    parsed = load_rules_config(rules)
    assert parsed.watchlist["GOOG"].enabled is False
    assert parsed.watchlist["GOOG"].buy_rules[0].id
    assert parsed.reliability == original.reliability
    assert all(parsed.watchlist[key] == value for key, value in original.watchlist.items())
    assert saved["revision"] != following["revision"]
    stale = send(app, "watchlist_save", {"market": "US", "ticker": "GOOG", "name": "old",
        "research_enabled": False, "expected_revision": initial["revision"]})
    assert isinstance(stale, RpcSuccessResponse)
    assert stale.result["accepted"] is False
    assert stale.result["result"]["status"] == "conflict"
    with ResearchStore(database) as store:
        assert store.list_runs() == []
        assert store.load_settings()["instruments"][0]["ticker"] == "GOOG"


def test_additive_workflow_schema_backs_up_existing_records_once(tmp_path):
    import sqlite3

    database = tmp_path / "legacy.db"
    with ResearchStore(database) as store:
        evidence = store.upsert_evidence({"source_url": "https://example.org/source", "title": "Original",
                                         "content_text": "Original source", "content_kind": "article"})
    with ResearchStore(database) as store:
        store.ensure_feature_schema("CREATE TABLE IF NOT EXISTS test_workflow (id TEXT PRIMARY KEY);",
                                    ("test_workflow",))
        backup = store.backup_path
        assert backup is not None and backup.exists()
        assert store.get_evidence(evidence.id) is not None
        store.ensure_feature_schema("CREATE TABLE IF NOT EXISTS test_workflow (id TEXT PRIMARY KEY);",
                                    ("test_workflow",))
        assert store.backup_path == backup
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT id FROM research_evidence").fetchone()[0] == evidence.id
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='test_workflow'").fetchone()
    assert len(list(tmp_path.glob("*.pre-workflow-*.bak"))) == 1


def test_workflow_result_reaches_real_qt_client(workflows):
    import uuid
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from desktop.ui.client import LocalSocketGuardianClient
    from guardian.transport_qt import QtGuardianServer

    qt = QApplication.instance() or QApplication([])
    app, _, _ = workflows
    name = "ag-workflow-" + uuid.uuid4().hex
    server = QtGuardianServer(name, app.handle_json)
    server.start()
    client = LocalSocketGuardianClient(name, TOKEN, request_timeout_ms=2000)
    receipts, failures = [], []
    loop = QEventLoop()
    client.action_completed.connect(lambda receipt: (receipts.append(receipt), loop.quit()))
    client.request_failed.connect(lambda *error: (failures.append(error), loop.quit()))
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    try:
        client.research_command("watchlist_get")
        timer.start(3000)
        loop.exec()
        assert not failures
        assert receipts and receipts[0].accepted
        assert receipts[0].action == "research-watchlist_get"
        assert receipts[0].result["revision"]
        assert any(item["key"] == "US:AAPL" for item in receipts[0].result["items"])
    finally:
        timer.stop()
        client.close()
        server.stop()
        qt.processEvents()


def test_window_links_securities_reports_and_events_with_scrollable_forms(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from desktop.ui.app import create_application
    from desktop.ui.client import FixtureGuardianClient

    qt = QApplication.instance() or QApplication([])
    client = FixtureGuardianClient()
    _, window = create_application([], client=client, auto_refresh=False)
    selected = []
    monkeypatch.setattr(window.review_page, "set_instrument", lambda key: selected.append(("review", key)))
    monkeypatch.setattr(window.event_page, "set_instrument", lambda key: selected.append(("event", key)))
    monkeypatch.setattr(window.review_page, "set_report", lambda key: selected.append(("report", key)))
    try:
        window.resize(960, 640)
        window.show()
        window.watchlist_page.view_research_requested.emit("US:AAPL")
        qt.processEvents()
        assert window.stack.currentWidget() is window.research_page
        assert window.research_page.tabs.currentWidget() is window.review_scroll
        assert window.review_scroll.verticalScrollBar().maximum() > 0
        assert window.review_page.table.height() >= window.review_page.table.minimumHeight()
        window.watchlist_page.view_events_requested.emit("US:AAPL")
        qt.processEvents()
        assert window.research_page.tabs.currentWidget() is window.event_scroll
        assert window.event_scroll.verticalScrollBar().maximum() > 0
        window.research_page.review_requested.emit("saved-report")
        assert selected == [("review", "US:AAPL"), ("event", "US:AAPL"), ("report", "saved-report")]
        window.show_page("assets")
        assert window.stack.currentWidget() is window.assets_tabs
        assert window.stack.count() == 6
    finally:
        window._close_allowed = True
        window.close()
        client.close()
        qt.processEvents()
