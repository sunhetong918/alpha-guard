from __future__ import annotations

from copy import deepcopy

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication

from desktop.ui.client import GuardianClient
from desktop.ui.generation import generation_html
from desktop.ui.models import ActionReceipt
from desktop.ui.progress_page import RunProgressPage


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication(["progress-ui-tests"])


class Client(GuardianClient):
    def __init__(self):
        super().__init__()
        self.commands = []

    def research_command(self, command, fields=None):
        self.commands.append((command, deepcopy(fields or {})))


def emit(client, command, result, *, accepted=True):
    client.action_completed.emit(ActionReceipt("research-" + command, accepted, "fixture", "test", result))


def run(identity="a" * 32, **changes):
    return {"run_id": identity, "status": "partial", "created_at": "2026-10-15T01:00:00Z",
            "completed": 1, "total": 2, "failed": 0, "pending": 1, "uncertain": 0,
            "model_calls": 1, "max_ai_calls": 5, "resumable": True, "incompatible_reason": None, **changes}


def detail(record, *, title="Saved material", reports=None, offset=0, next_offset=None):
    return {"run": record, "offset": offset, "next_offset": next_offset,
            "items": [{"title": title, "status": "completed", "model_calls": 1,
                       "report_ids": ["report-1"] if reports is None else reports}]}


def listed(qt_app, records=None):
    client = Client()
    page = RunProgressPage(client)
    assert client.commands == []
    page.activate()
    emit(client, "run_list", {"items": records or [run()], "offset": 0, "next_offset": None})
    page.table.selectRow(0)
    qt_app.processEvents()
    return client, page


def close(page):
    page.deactivate()
    page.close()


def test_open_select_refresh_and_pagination_are_local_reads(qt_app):
    client, page = listed(qt_app)
    emit(client, "run_detail", detail(run(), next_offset=20))
    page.detail_next.click()
    assert client.commands[-1] == ("run_detail", {"run_id": "a" * 32, "offset": 20})
    emit(client, "run_detail", detail(run(), offset=20))
    page.detail_previous.click()
    assert client.commands[-1][1]["offset"] == 0
    emit(client, "run_detail", detail(run()))
    page.refresh_button.click()
    emit(client, "run_list", {"items": [run()], "offset": 0, "next_offset": 20})
    emit(client, "run_detail", detail(run()))
    page.next_button.click()
    assert client.commands[-1] == ("run_list", {"offset": 20})
    emit(client, "run_list", {"items": [run("b" * 32)], "offset": 20, "next_offset": None})
    assert page._run_id == "" and not page.resume_button.isEnabled()
    assert {command for command, _ in client.commands} == {"run_list", "run_detail"}
    page.set_snapshot({"runtime": {"state": "RUNNING"}})
    close(page)
    count = len(client.commands)
    page._poll()
    assert len(client.commands) == count


def test_uncertain_resume_requires_current_detail_checkbox_and_explicit_click(qt_app):
    record = run(uncertain=1, pending=0)
    client, page = listed(qt_app, [record])
    page.ack.setChecked(True)
    page.resume_button.click()
    assert not any(command == "resume" for command, _ in client.commands)
    emit(client, "run_detail", detail(record))
    page.ack.setChecked(False)
    page.resume_button.click()
    assert not any(command == "resume" for command, _ in client.commands)
    page.ack.setChecked(True)
    assert not any(command == "resume" for command, _ in client.commands)
    page.resume_button.click()
    assert client.commands[-1] == ("resume", {"run_id": record["run_id"], "ack_uncertain": True})
    page.resume_button.click()
    assert [command for command, _ in client.commands].count("resume") == 1
    emit(client, "resume", {"run_id": record["run_id"], "state": "RUNNING"})
    assert not page.resume_button.isEnabled() and not page.ack.isChecked()
    assert [command for command, _ in client.commands].count("resume") == 1
    close(page)


def test_incompatible_batch_never_enables_resume(qt_app):
    record = run(resumable=False, incompatible_reason="configuration_changed")
    client, page = listed(qt_app, [record])
    emit(client, "run_detail", detail(record))
    page.ack.setChecked(True)
    page.resume_button.click()
    assert "配置已变化" in page.summary.text()
    assert not any(command == "resume" for command, _ in client.commands)
    close(page)


def test_late_and_duplicate_other_batch_detail_do_not_replace_pending_selection(qt_app):
    one, two = run(), run("b" * 32)
    client, page = listed(qt_app, [one, two])
    page.table.selectRow(1)
    qt_app.processEvents()
    assert [command for command, _ in client.commands].count("run_detail") == 1
    emit(client, "run_detail", detail(one, title="Old batch", reports=["old-report"]))
    assert client.commands[-1] == ("run_detail", {"run_id": two["run_id"], "offset": 0})
    assert "Old batch" not in page.details.toPlainText()
    count = len(client.commands)
    emit(client, "run_detail", detail(one, title="Duplicate old batch"))
    assert len(client.commands) == count  # A duplicate must not consume B's pending request.
    assert page._pending["run_detail"]["run_id"] == two["run_id"]
    emit(client, "run_detail", detail(two, title="Current batch", reports=["new-report"]))
    assert "Current batch" in page.details.toPlainText() and page._run_id == two["run_id"]
    assert page._report_ids == {"new-report"}
    close(page)


def test_late_page_response_does_not_consume_the_new_page_request(qt_app):
    client, page = listed(qt_app)
    emit(client, "run_detail", detail(run(), title="Page one", next_offset=20))
    page.detail_next.click()
    count = len(client.commands)
    emit(client, "run_detail", detail(run(), title="Stale page one", next_offset=20))
    assert len(client.commands) == count and page._pending["run_detail"]["offset"] == 20
    emit(client, "run_detail", detail(run(), title="Page two", offset=20))
    assert "Page two" in page.details.toPlainText() and "Stale" not in page.details.toPlainText()
    close(page)


def test_disconnect_keeps_content_and_requires_refresh_before_another_resume(qt_app):
    record = run(uncertain=1)
    client, page = listed(qt_app, [record])
    emit(client, "run_detail", detail(record))
    page.ack.setChecked(True)
    page.resume_button.click()
    before = page.details.toPlainText()
    client.request_failed.emit("research-resume", "disconnected")
    assert page.details.toPlainText() == before
    assert not page.resume_button.isEnabled() and not page.ack.isChecked()
    count = len(client.commands)
    page._poll()
    page.resume_button.click()
    assert len(client.commands) == count
    assert "不会自动重试" in page.message.text()
    page.refresh_button.click()
    emit(client, "run_list", {"items": [record], "offset": 0, "next_offset": None})
    emit(client, "run_detail", detail(record))
    assert not page.resume_button.isEnabled()
    page.ack.setChecked(True)
    assert page.resume_button.isEnabled()
    close(page)


def test_failed_detail_retains_saved_rows_without_enabling_stale_resume(qt_app):
    client, page = listed(qt_app)
    emit(client, "run_detail", detail(run(), title="Saved result"))
    page.refresh()
    client.request_failed.emit("research-run_detail", "disconnected")
    assert "Saved result" in page.details.toPlainText()
    assert not page.resume_button.isEnabled()
    close(page)


def test_report_links_are_saved_ids_only_and_html_is_escaped(qt_app):
    client, page = listed(qt_app)
    captured = []
    page.report_requested.connect(captured.append)
    emit(client, "run_detail", detail(run(), title='<a href="https://evil.test">untrusted</a>',
                                      reports=["report-1", "report:two"]))
    text = page.details.toPlainText()
    assert '<a href="https://evil.test">untrusted</a>' in text
    assert not page.details.openLinks() and not page.details.openExternalLinks()
    for url in ("https://evil.test", "file:///tmp/private", "javascript:bad()", "report:missing"):
        page.details.anchorClicked.emit(QUrl(url))
    assert captured == []
    page.details.anchorClicked.emit(QUrl("report:report-1"))
    page.details.anchorClicked.emit(QUrl("report:report%3Atwo"))
    assert captured == ["report-1", "report:two"]
    close(page)


def test_generation_html_missing_values_escape_output_and_omit_extra_fields():
    assert "生成配置未记录" in generation_html(None)
    value = {"provider": "openai_compatible", "model": '<a href="https://evil.test">model</a>',
             "horizons": ["swing"], "parameters": {"max_output_tokens": 6000, "tools_enabled": False,
             "api_key": "secret-value"}, "base_url": "https://private.example.test", "api_key": "secret-value"}
    rendered = generation_html(value)
    assert "&lt;a href=" in rendered and '<a href="https://evil.test">' not in rendered
    assert "工具：关闭" in rendered and "波段" in rendered and "6000" in rendered
    assert "secret-value" not in rendered and "private.example.test" not in rendered
    assert "未记录 / 未指定" in rendered


@pytest.mark.parametrize("parameters,horizons", [("invalid", None), ([], "swing"), (None, None)])
def test_generation_html_handles_partial_legacy_shapes(parameters, horizons):
    rendered = generation_html({"model": "legacy", "parameters": parameters, "horizons": horizons})
    assert "legacy" in rendered and "未记录 / 未指定" in rendered


def test_late_list_summary_does_not_override_newer_selected_detail(qt_app):
    client, page = listed(qt_app)
    emit(client, "run_detail", detail(run()))
    page.refresh()
    current = run(status="running", resumable=False, incompatible_reason="run_active")
    emit(client, "run_detail", detail(current))
    emit(client, "run_list", {"items": [run()], "offset": 0, "next_offset": None})
    assert page._run["status"] == "running" and not page.resume_button.isEnabled()
    assert "仍在运行" in page.summary.text()
    close(page)


def test_disconnect_suspends_local_polling_until_explicit_refresh(qt_app):
    current = run(status="running", resumable=False)
    client, page = listed(qt_app, [current])
    emit(client, "run_detail", detail(current))
    page._poll()
    client.request_failed.emit("research-run_list", "disconnected")
    client.request_failed.emit("research-run_detail", "disconnected")
    count = len(client.commands)
    page._poll()
    assert len(client.commands) == count
    page.refresh_button.click()
    assert len(client.commands) == count + 2
    close(page)
