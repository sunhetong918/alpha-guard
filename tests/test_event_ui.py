from __future__ import annotations

import copy

import pytest
from PySide6.QtWidgets import QApplication

from desktop.ui.client import GuardianClient
from desktop.ui.event_page import EventCalendarPage
from desktop.ui.models import ActionReceipt


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication(["events-ui"])
    yield instance


class Client(GuardianClient):
    def __init__(self):
        super().__init__()
        self.commands = []

    def research_command(self, command, fields=None):
        self.commands.append((command, copy.deepcopy(fields or {})))

    def reply(self, command, result, accepted=True):
        self.action_completed.emit(ActionReceipt("research-" + command, accepted, "", "r1", result))


def record(**changes):
    row = {"event_id": "a" * 64, "version": 1, "instrument": {"market": "US", "ticker": "AAPL"},
           "event_type": "earnings", "event_date": "2026-10-30", "date_kind": "event", "event_at": None,
           "published_at": None, "observed_at": "2026-10-15T01:00:00Z", "event_timezone": "America/New_York",
           "source": "company-ir", "source_event_id": "fy2026-q4", "source_url": "https://example.org/report",
           "title": "财报", "status": "expected", "provenance": "manual", "notes": ""}
    return {**row, **changes}


def loaded(app):
    client = Client()
    page = EventCalendarPage(client)
    page.activate()
    client.reply("event_list", {"events": [record()], "total": 25})
    client.reply("event_status", {"sources": [], "running": False})
    return client, page


def test_open_and_instrument_navigation_read_only(app):
    client = Client()
    page = EventCalendarPage(client)
    assert client.commands == []
    page.set_instrument("HK:00700")
    assert client.commands == []
    page.activate()
    assert [c[0] for c in client.commands] == ["event_list", "event_status"]
    assert client.commands[0][1]["instrument"] == "HK:00700"
    assert "event_refresh" not in [c[0] for c in client.commands]
    page.deactivate()
    assert not page._timer.isActive()
    page.close()


def test_coverage_displays_scope_failure_and_original_cache_clock_without_fetch(app):
    client, page = loaded(app)
    page.instrument_input.setText("US:AAPL")
    page.query_button.click()
    value = {"instrument": "US:AAPL", "status": "partial", "ranges": [
        {"event_type": "dividend", "date_from": "2026-10-13", "date_to": "2026-10-13",
         "status": "empty", "checked_at": "2026-10-16T10:00:00Z", "last_success_at": "2026-10-16T10:00:00Z"},
        {"event_type": "dividend", "date_from": "2026-10-14", "date_to": "2026-10-14",
         "status": "failed", "detail": "rate_limited", "last_success_at": "2026-10-15T10:00:00Z"},
        {"event_type": "split", "date_from": "2026-10-13", "date_to": "2026-10-14",
         "status": "stale", "cache_source_at": "2026-10-12T10:00:00Z"},
        {"event_type": "earnings", "date_from": "2026-10-16", "date_to": "2026-10-20",
         "status": "unsupported"},
    ]}
    client.reply("event_list", {"events": [], "total": 0, "query_coverage": value})
    text = page.coverage_detail.toPlainText()
    assert all(label in text for label in ("已查无记录", "查询失败", "来源限流", "缓存已过期", "来源不支持"))
    assert "2026-10-15T10:00:00Z" in text and "2026-10-12T10:00:00Z" in text
    assert all(command != "event_refresh" for command, _ in client.commands)
    page.close()


def test_coverage_is_escaped_and_old_response_cannot_replace_new_query(app):
    client, page = loaded(app)
    page.instrument_input.setText("US:AAPL")
    page.query_button.click()
    page.instrument_input.setText("HK:00700")
    page.query_button.click()
    client.reply("event_list", {"events": [], "query_coverage": {"instrument": "wrong", "ranges": []}})
    assert "wrong" not in page.coverage_detail.toPlainText()
    client.reply("event_list", {"events": [], "query_coverage": {"instrument": "HK:00700 <b>literal</b>", "ranges": []}})
    assert "<b>literal</b>" in page.coverage_detail.toPlainText()
    page.close()


def test_local_query_pagination_and_unknown_time(app):
    client, page = loaded(app)
    page.table.selectRow(0)
    assert "时间未知（仅日期）" in page.detail.toPlainText()
    assert "发布时间：未知" in page.detail.toPlainText()
    assert client.commands[-1][0] == "event_history"
    client.reply("event_history", {"events": [record()], "total": 1})
    page.next_button.click()
    assert client.commands[-1] == ("event_list", {"offset": 20, "limit": 20})
    client.reply("event_list", {"events": [record()], "total": 25})
    assert "21–21 / 25" in page.page_label.text()
    page.close()


def test_manual_save_remains_offline_and_has_stable_source_id(app):
    client, page = loaded(app)
    page.table.selectRow(0)
    page.manual_date.setText("2026-11-03")
    page.save_button.click()
    command, fields = client.commands[-1]
    assert command == "event_save"
    assert fields["event"]["source_event_id"] == "fy2026-q4"
    assert fields["event"]["event_at"] is None
    assert fields["event"]["published_at"] is None
    assert not any(c[0] == "event_refresh" for c in client.commands)
    page.close()


def test_dividend_requires_named_date_and_preserves_four_dates(app):
    client, page = loaded(app)
    page.manual_instrument.setText("US:AAPL")
    page.manual_type.setCurrentIndex(page.manual_type.findData("dividend"))
    page.save_button.click()
    assert "分红必须选择" in page.message.text()
    page.manual_date_kind.setCurrentIndex(page.manual_date_kind.findData("payment"))
    page.save_button.click()
    command, fields = client.commands[-1]
    assert command == "event_save"
    assert fields["event"]["dividend_dates"] == {"payment": page.manual_date.text()}
    page.close()


def test_explicit_refresh_and_failure_do_not_clear_old_rows(app):
    client, page = loaded(app)
    page.instrument_input.setText("US:AAPL")
    page.refresh_button.click()
    assert client.commands[-1] == ("event_refresh", {"instrument": "US:AAPL"})
    client.reply("event_refresh", {"status": "running"})
    client.reply("event_status", {"running": True, "sources": [{"instrument": "US:AAPL", "status": "running"}]})
    assert page._timer.isActive() and page.stop_button.isEnabled()
    client.reply("event_list", {})  # Unsolicited data is ignored.
    assert page.table.rowCount() == 1
    page._poll()
    client.reply("event_status", {"running": False, "sources": [{"instrument": "US:AAPL", "status": "failed", "detail": "rate_limited"}]})
    assert "失败，保留旧记录" in page.source_label.text()
    assert "rate_limited" in page.source_label.text()
    assert page.table.rowCount() == 1 and not page._timer.isActive()
    page.close()


def test_late_local_query_is_discarded_after_selection_changed(app):
    client = Client()
    page = EventCalendarPage(client)
    page.activate()
    page.set_instrument("HK:00700")
    client.reply("event_list", {"events": [record()], "total": 1})
    assert page.table.rowCount() == 0
    assert client.commands[-1][1]["instrument"] == "HK:00700"
    client.reply("event_list", {"events": [], "total": 0})
    assert page.table.rowCount() == 0
    page.close()


def test_import_limit_error_preserves_draft(app):
    client, page = loaded(app)
    page.import_text.setPlainText("x" * 120001)
    count = len(client.commands)
    page.import_button.click()
    assert len(client.commands) == count and "120 KB" in page.message.text()
    assert len(page.import_text.toPlainText()) == 120001
    page.import_text.setPlainText('[{"event_type":"earnings"}]')
    page.import_button.click()
    assert client.commands[-1][0] == "event_import"
    client.request_failed.emit("research-event_import", "invalid")
    assert "保留" in page.message.text() and page.import_text.toPlainText()
    page.close()


def test_automatic_event_cannot_be_overwritten_as_manual(app):
    client = Client()
    page = EventCalendarPage(client)
    page.activate()
    client.reply("event_list", {"events": [record(provenance="automatic")], "total": 1})
    page.table.selectRow(0)
    assert not page.save_button.isEnabled()
    page.new_button.click()
    assert page.save_button.isEnabled() and page.manual_source.text() == "manual"
    page.close()


def test_manual_dividend_revision_keeps_other_dates_after_local_refresh(app):
    client = Client()
    page = EventCalendarPage(client)
    row = record(event_type="dividend", date_kind="payment", amount=1.2, currency="USD",
                 dividend_dates={"declaration": "2026-10-01", "ex_dividend": "2026-10-10",
                                 "record": "2026-10-11", "payment": "2026-10-30"})
    page.activate()
    client.reply("event_list", {"events": [row], "total": 1})
    page.table.selectRow(0)
    page.query_button.click()
    client.reply("event_list", {"events": [row], "total": 1})
    page.manual_date.setText("2026-11-02")
    page.save_button.click()
    payload = client.commands[-1][1]["event"]
    assert payload["dividend_dates"] == {**row["dividend_dates"], "payment": "2026-11-02"}
    assert payload["amount"] == 1.2 and payload["currency"] == "USD"
    page.close()


def test_automatic_draft_stays_read_only_after_local_refresh(app):
    client = Client()
    page = EventCalendarPage(client)
    row = record(provenance="automatic")
    page.activate()
    client.reply("event_list", {"events": [row], "total": 1})
    page.table.selectRow(0)
    page.query_button.click()
    client.reply("event_list", {"events": [row], "total": 1})
    assert not page.save_button.isEnabled()
    page.new_button.click()
    assert page.save_button.isEnabled()
    page.close()


def test_same_security_return_preserves_page_and_manual_draft(app):
    client = Client()
    page = EventCalendarPage(client)
    page.set_instrument("US:AAPL")
    page.activate()
    client.reply("event_list", {"events": [record()], "total": 25})
    client.reply("event_status", {"sources": [], "running": False})
    page.next_button.click()
    client.reply("event_list", {"events": [record()], "total": 25})
    page.manual_title.setText("Apple draft")
    page.manual_timezone.setText("UTC")
    before = page._form_signature()
    page.deactivate()
    page.set_instrument("US:AAPL")
    page.activate()
    assert page._offset == 20
    assert client.commands[-2] == ("event_list", {"instrument": "US:AAPL", "offset": 20, "limit": 20})
    client.reply("event_list", {"events": [record()], "total": 25})
    assert page._form_signature() == before
    assert not any(command == "event_refresh" for command, _fields in client.commands)
    page.close()


def test_other_security_navigation_preserves_draft_and_explains_block(app):
    client = Client()
    page = EventCalendarPage(client)
    page.set_instrument("US:AAPL")
    page.manual_title.setText("Apple draft")
    page.manual_timezone.setText("UTC")
    before = page._form_signature()
    page.set_instrument("HK:00700")
    assert page.instrument_input.text() == "US:AAPL"
    assert page._form_signature() == before
    assert "尚未切换证券" in page.message.text()
    page.activate()
    client.reply("event_list", {"events": [], "total": 0})
    assert "尚未切换证券" in page.message.text()
    page.new_button.click()
    page.set_instrument("HK:00700")
    assert page.instrument_input.text() == page.manual_instrument.text() == "HK:00700"
    assert not page.manual_title.text()
    assert page.manual_timezone.text() == "Asia/Hong_Kong"
    page.close()


def test_save_acknowledgement_does_not_mark_later_edits_as_saved(app):
    client, page = loaded(app)
    page.table.selectRow(0)
    page.manual_title.setText("submitted title")
    page.save_button.click()
    page.manual_title.setText("new unsaved title")
    client.reply("event_save", {"events": [record(title="submitted title")], "saved": 1, "changed": 1})
    page.set_instrument("HK:00700")
    assert page.manual_instrument.text() == "US:AAPL"
    assert page.manual_title.text() == "new unsaved title"
    assert "尚未切换证券" in page.message.text()
    page.save_button.click()
    client.reply("event_save", {"events": [record(title="new unsaved title")], "saved": 1, "changed": 1})
    page.set_instrument("HK:00700")
    assert page.manual_instrument.text() == "HK:00700"
    assert page.manual_title.text() == ""
    page.close()


def test_sec_refresh_is_explicit_and_details_show_separate_period(app):
    client, page = loaded(app)
    page.instrument_input.setText("US:SPCX")
    page.sec_button.click()
    assert client.commands[-1] == ("event_refresh", {"instrument": "US:SPCX", "source": "sec_submissions"})
    assert not page.refresh_button.isEnabled() and not page.sec_button.isEnabled()
    page._show_details([record(filing={"form": "10-Q/A", "cik": "0001181412",
        "report_date": "2026-06-30", "amended": True, "acceptance_original": "<unknown>",
        "acceptance_precision": "unknown_timezone", "mapping_source": "https://data.sec.gov"})])
    assert "报告期：2026-06-30" in page.detail.toPlainText()
    assert "<unknown>" in page.detail.toPlainText()
    page.close()
