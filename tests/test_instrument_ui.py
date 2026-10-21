from __future__ import annotations

from copy import deepcopy

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication

from desktop.ui.client import GuardianClient
from desktop.ui.instrument_page import InstrumentOverviewPage
from desktop.ui.models import ActionReceipt
from desktop.ui.window import MainWindow


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication(["instrument-overview-tests"])


class Client(GuardianClient):
    def __init__(self):
        super().__init__()
        self.commands = []
        self.details = []

    def refresh_research(self):
        pass

    def research_command(self, command, fields=None):
        self.commands.append((command, deepcopy(fields or {})))

    def research_detail(self, kind, identity, **_kwargs):
        self.details.append((kind, identity))


def overview(key="US:AAPL"):
    return {"instrument": {"key": key, "name": "Apple"}, "as_of": "2026-10-15T12:00:00Z",
            "membership": {"status": "ready", "research_enabled": True},
            "rules": {"status": "ready", "monitoring_enabled": False, "rule_count": 0},
            "scan": {"status": "not_collected", "observed_at": None, "detail": "per_instrument_scan_not_recorded"},
            "reports": {"status": "ready", "total": 1, "items": [{"id": "r1", "horizon": "long", "status": "expired", "summary": "原报告摘要"}]},
            "reviews": {"status": "ready", "pending_count": 1, "next_review_at": "2026-10-20T00:00:00Z"},
            "events": {"status": "empty", "total": 0, "items": [], "coverage": "不代表没有未来事件",
                       "sources": [{"source": "yahoo_chart", "status": "failed", "last_success_at": None}]}}


def emit(client, value):
    client.action_completed.emit(ActionReceipt("research-instrument_overview", True, "读取完成", "overview", value))


def test_overview_switch_serializes_reads_and_ignores_late_other_security(qt_app):
    client = Client()
    page = InstrumentOverviewPage(client)
    assert client.commands == []
    page.show_instrument("US:AAPL")
    page.show_instrument("US:MSFT")
    assert len(client.commands) == 1
    emit(client, overview())
    assert client.commands[-1] == ("instrument_overview", {"instrument": "US:MSFT"})
    assert "原报告摘要" not in page.content.toPlainText()
    emit(client, overview("US:MSFT"))
    assert "US:MSFT" in page.identity_label.text()
    emit(client, overview())
    assert "US:MSFT" in page.identity_label.text()
    assert all(c[0] == "instrument_overview" for c in client.commands)
    page.close()


def test_overview_preserves_previous_snapshot_on_error_and_labels_gaps(qt_app):
    client = Client()
    page = InstrumentOverviewPage(client)
    page.show_instrument("US:AAPL")
    emit(client, overview())
    text = page.content.toPlainText()
    assert "已过期" in text and "待核验 1" in text
    assert "刷新失败，保留旧记录" in text and "尚未采集" in text
    page.refresh()
    client.request_failed.emit("research-instrument_overview", "invalid_response")
    assert page.content.toPlainText() == text and "保留上次" in page.message.text()
    page.show_instrument("US:MSFT")
    client.request_failed.emit("research-instrument_overview", "invalid_response")
    assert "原报告摘要" not in page.content.toPlainText()
    assert "不能据此判断" in page.message.text()
    page.close()


def test_overview_allows_only_saved_report_links_and_preserves_identity(qt_app):
    client = Client()
    page = InstrumentOverviewPage(client)
    reports, reviews, events = [], [], []
    page.report_requested.connect(reports.append)
    page.review_requested.connect(reviews.append)
    page.events_requested.connect(events.append)
    page.show_instrument("US:AAPL")
    value = overview()
    value["reports"]["items"][0]["summary"] = '<a href="https://evil.example">外部内容</a>'
    emit(client, value)
    page.content.anchorClicked.emit(QUrl("https://evil.example"))
    page.content.anchorClicked.emit(QUrl("alpha-guard-report:missing"))
    assert reports == []
    page.content.anchorClicked.emit(QUrl("alpha-guard-report:r1"))
    page.review_button.click()
    page.events_button.click()
    assert reports == ["r1"] and reviews == events == ["US:AAPL"]
    page.close()


def test_window_roundtrip_keeps_watchlist_page_filter_selection_and_draft(qt_app):
    client = Client()
    window = MainWindow(client, auto_refresh=False)
    window.show_page("assets")
    page = window.watchlist_page
    data = {"items": [{"key": "US:AAPL", "market": "US", "ticker": "AAPL", "name": "Apple",
                       "research_enabled": True, "monitoring_enabled": False, "rule_count": 0}],
            "revision": "one", "offset": 20, "next_offset": None, "total": 21}
    client.action_completed.emit(ActionReceipt("research-watchlist_get", True, "ok", "one", data))
    page.table.selectRow(0)
    page.name_input.setText("尚未保存的名称")
    page.overview_button.click()
    emit(client, overview())
    assert window.assets_tabs.currentWidget() is window.instrument_scroll
    window.instrument_page.back_button.click()
    assert client.commands[-1] == ("watchlist_get", {"offset": 20})
    client.action_completed.emit(ActionReceipt("research-watchlist_get", True, "ok", "two", data))
    assert page._selected_key == "US:AAPL" and page._offset == 20
    assert page.name_input.text() == "尚未保存的名称" and page._form_dirty
    window._close_allowed = True
    window.close()


def test_report_overview_uses_report_security_instead_of_previous_context(qt_app):
    client = Client()
    window = MainWindow(client, auto_refresh=False)
    window._set_instrument_return("US:AAPL")
    window._show_original_report("msft-report")
    client.research_detail_ready.emit({"kind": "report", "id": "msft-report", "item": {
        "id": "msft-report", "instrument": {"market": "US", "ticker": "MSFT"},
        "status": "ready", "action": "watch", "horizon": "long", "summary": "Original"}})
    assert window._instrument_return_key == "US:MSFT"
    window.research_page.report_instrument_button.click()
    assert window.instrument_page.instrument == "US:MSFT"
    assert client.commands[-1] == ("instrument_overview", {"instrument": "US:MSFT"})
    window._close_allowed = True
    window.close()
