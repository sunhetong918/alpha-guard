from __future__ import annotations

import copy
import json

import pytest
from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import QApplication

from desktop.ui.client import GuardianClient
from desktop.ui.models import ActionReceipt
from desktop.ui.review_page import MAX_IMPORT_BYTES, ReviewPage


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(["review-ui-tests"])
    yield app


class Client(GuardianClient):
    def __init__(self):
        super().__init__()
        self.commands = []

    def research_command(self, command, fields=None):
        self.commands.append((command, copy.deepcopy(fields or {})))


def emit(client, command, result, *, accepted=True, message=""):
    client.action_completed.emit(ActionReceipt(f"research-{command}", accepted, message, "test", result))


def item(identity="r1", *, revision=0, notes=""):
    return {"report_id": identity, "instrument_key": "US:AAPL", "created_at": "2026-09-29T14:00:00+00:00",
            "horizon": "swing", "summary": "原始报告摘要", "review": {"status": "pending", "notes": notes,
            "revision": revision, "next_review_at": None, "updated_at": None}}


def populated(qt_app):
    client = Client()
    page = ReviewPage(client)
    page.activate()
    emit(client, "review_list", {"items": [item()], "total": 1, "offset": 0, "has_more": False})
    emit(client, "replay_list", {"items": [{"id": "set1", "source_label": "manual.json", "sample_count": 1,
                                           "imported_at": "2026-09-30T00:00:00+00:00"}]})
    page.table.selectRow(0)
    qt_app.processEvents()
    return client, page


def test_activate_and_refresh_only_request_local_lists(qt_app):
    client, page = populated(qt_app)
    assert [command for command, _ in client.commands] == ["review_list", "replay_list"]
    page.notes.setPlainText("尚未保存的笔记")
    page.set_snapshot({"reports": []})
    page.deactivate()
    page.activate()
    assert page.notes.toPlainText() == "尚未保存的笔记"
    assert all(command not in {"review_save", "replay_import", "replay_run"} for command, _ in client.commands)
    page.close()


def test_save_uses_revision_and_conflict_retains_draft(qt_app):
    client, page = populated(qt_app)
    page.notes.setPlainText("已核对原文")
    page.review_status.setCurrentIndex(page.review_status.findData("verified"))
    page.next_review_enabled.setChecked(True)
    page.next_review.setDateTime(QDateTime.currentDateTime().addDays(2))
    page.save_button.click()
    command, fields = client.commands[-1]
    assert command == "review_save"
    assert fields["revision"] == 0 and fields["notes"] == "已核对原文"
    assert fields["next_review_at"].endswith("Z")
    emit(client, "review_save", None, accepted=False, message="记录已被其他窗口修改，请重新读取后重试。")
    assert page.notes.toPlainText() == "已核对原文"
    assert "重新读取" in page.message.text()
    page.reload_button.click()
    assert client.commands[-1][0] == "review_detail"
    emit(client, "review_detail", {"report_id": "r1", "review": item(revision=2, notes="另一窗口的备注")["review"],
                                   "content": "{}", "offset": 0, "next_offset": None})
    assert page.notes.toPlainText() == "另一窗口的备注"
    page.save_button.click()
    assert client.commands[-1][1]["revision"] == 2
    page.close()


def test_direct_report_navigation_handles_stale_detail_and_has_reader_route(qt_app):
    client = Client()
    page = ReviewPage(client)
    page.set_report("older-id")
    page.set_report("target-id")
    emit(client, "review_detail", {"report_id": "older-id", "review": item("older-id")["review"], "content": "old"})
    assert client.commands[-1] == ("review_detail", {"report_id": "target-id", "offset": 0})
    assert "old" not in page.original.toPlainText()
    emit(client, "review_detail", {"report_id": "target-id", "review": item("target-id", revision=3)["review"],
                                   "content": "target content", "offset": 0, "next_offset": 20000})
    captured = []
    page.view_report_requested.connect(captured.append)
    page.read_report_button.click()
    assert captured == ["target-id"]
    page.more_original_button.click()
    assert client.commands[-1][1] == {"report_id": "target-id", "offset": 20000}
    page.close()


def test_import_has_file_limit_and_does_not_automatically_run(qt_app, tmp_path):
    client, page = populated(qt_app)
    oversized = tmp_path / "large.json"
    oversized.write_bytes(b" " * (MAX_IMPORT_BYTES + 1))
    count = len(client.commands)
    page.import_file(oversized)
    assert len(client.commands) == count
    assert "256 KB" in page.message.text()
    valid = tmp_path / "prices.json"
    valid.write_text("[]")
    page.import_file(valid)
    assert client.commands[-1] == ("replay_import", {"source_label": "prices.json", "json_text": "[]"})
    emit(client, "replay_import", {"id": "new-set", "sample_count": 2})
    assert client.commands[-1][0] == "replay_list"
    assert not any(command == "replay_run" for command, _ in client.commands)
    emit(client, "replay_list", {"items": [{"id": "new-set", "source_label": "prices.json", "sample_count": 2,
                                           "imported_at": "2026-10-01T00:00:00+00:00"}]})
    page.run_button.click()
    assert client.commands[-1][0] == "replay_run"
    assert client.commands[-1][1]["sample_set_id"] == "new-set"
    assert client.commands[-1][1]["as_of"].endswith("Z")
    page.close()


def test_followup_result_labels_pending_excluded_and_observed_honestly(qt_app):
    client, page = populated(qt_app)
    for status, label, reason, change in [("pending", "待样本", "end_price_unavailable", None),
                                         ("excluded", "已排除", "corporate_action_requires_adjustment", None),
                                         ("observed", "已观察", None, 10.0)]:
        page.run_button.click()
        emit(client, "replay_run", {"report_id": "r1", "source_label": "manual.json", "sample_count": 1,
             "as_of": "2026-10-01T00:00:00Z", "result": {"status": status, "reason": reason,
             "price_change_pct": change, "reference_price": 100, "baseline_source_as_of": "2026-09-29T13:59:00Z",
             "baseline_observed_at": "2026-09-29T13:59:40Z", "end_price": 110 if change else None,
             "end_provider": "user-source", "end_source_as_of": "2026-09-29T15:00:00Z",
             "end_observed_at": "2026-09-29T15:01:00Z"}})
        text = page.result.toPlainText()
        assert label in text and "manual.json" in text and "来源时刻" in text
        assert "不是交易收益" in text
        assert ("10.00%" in text) is (change is not None)
    page.close()


def test_list_paging_and_new_filter_ignore_old_page(qt_app):
    client, page = populated(qt_app)
    page.search_button.click()
    page.set_instrument("HK:00700")
    emit(client, "review_list", {"items": [item("stale")], "total": 1, "offset": 0, "has_more": False})
    assert client.commands[-1][1]["instrument"] == "HK:00700"
    assert page.table.item(0, 0).text() == "US:AAPL"  # old page not applied
    emit(client, "review_list", {"items": [item("hk")], "total": 21, "offset": 0, "has_more": True})
    page.next_button.click()
    assert client.commands[-1][1]["offset"] == 1
    page.close()


def test_example_copies_format_only(qt_app):
    client, page = populated(qt_app)
    count = len(client.commands)
    page.example_button.click()
    example = json.loads(QApplication.clipboard().text())
    assert example[0]["source_as_of"].endswith("+00:00")
    assert "provider" in example[0] and "observed_at" in example[0]
    assert len(client.commands) == count
    assert "不会自动导入" in page.message.text()
    page.close()


def test_import_refreshes_samples_after_an_older_list_response(qt_app, tmp_path):
    client = Client()
    page = ReviewPage(client)
    page.activate()
    path = tmp_path / "new.json"
    path.write_text("[]")
    page.import_file(path)
    emit(client, "replay_import", {"id": "new", "sample_count": 1})
    emit(client, "replay_list", {"items": []})
    assert [command for command, _ in client.commands].count("replay_list") == 2
    emit(client, "replay_list", {"items": [{"id": "new", "source_label": "new.json", "sample_count": 1,
                                           "imported_at": "2026-10-01T00:00:00Z"}]})
    assert page.sample_sets.currentData() == "new"
    page.close()


def test_cross_page_navigation_keeps_unsaved_review_on_its_original_report(qt_app):
    client, page = populated(qt_app)
    page.notes.setPlainText("尚未保存的重要核验记录")
    before = list(client.commands)
    page.set_report("different-report")
    page.set_instrument("HK:00700")
    assert page.notes.toPlainText() == "尚未保存的重要核验记录"
    assert client.commands == before
    assert "未保存" in page.message.text()
    page.save_button.click()
    assert client.commands[-1][1]["report_id"] == "r1"
    page.close()


def test_generation_button_reads_saved_configuration_without_saving_draft(qt_app):
    client, page = populated(qt_app)
    page.notes.setPlainText("尚未保存的核验笔记")
    before = len(client.commands)
    page.generation_button.click()
    assert client.commands[before:] == [("review_detail", {"report_id": "r1", "offset": 0})]
    emit(client, "review_detail", {"report_id": "r1", "review": item(notes="旧的已保存笔记")["review"],
        "content": "{}", "generation_context": None, "offset": 0, "next_offset": None})
    assert "生成配置未记录" in page.generation.toPlainText()
    assert "当前设置" in page.generation.toPlainText()
    assert page.notes.toPlainText() == "尚未保存的核验笔记" and page._dirty
    assert all(command not in {"review_save", "replay_run"} for command, _ in client.commands)
    page.close()


def test_generation_view_escapes_original_snapshot_and_ignores_current_settings(qt_app):
    client, page = populated(qt_app)
    page.notes.setPlainText("保留原笔记")
    page.generation_button.click()
    saved = {"provider": "openai_compatible", "model": '<a href="https://evil.test">original-model</a>',
             "profile_id": "saved-profile", "horizons": ["swing"],
             "parameters": {"max_output_tokens": 6000, "tools_enabled": False},
             "base_url": "https://private.test", "api_key": "fixture-secret"}
    emit(client, "review_detail", {"report_id": "r1", "review": item()["review"],
        "content": "{}", "generation_context": saved, "offset": 0})
    text = page.generation.toPlainText()
    assert saved["model"] in text and "saved-profile" in text and "波段" in text
    assert "private.test" not in text and "fixture-secret" not in text
    assert not page.generation.openExternalLinks()
    page.set_snapshot({"settings": {"model": "new-current-model"}})
    assert page.generation.toPlainText() == text and page.notes.toPlainText() == "保留原笔记"
    assert "new-current-model" not in page.generation.toPlainText()
    page.close()


def test_generation_for_another_report_ignores_late_previous_detail(qt_app):
    client = Client()
    page = ReviewPage(client)
    page.activate()
    emit(client, "review_list", {"items": [item("r1"), item("r2")], "total": 2,
                                "offset": 0, "has_more": False})
    page.table.selectRow(0)
    page.generation_button.click()
    page.table.selectRow(1)
    qt_app.processEvents()
    assert not page._generation_visible
    emit(client, "review_detail", {"report_id": "r1", "review": item("r1")["review"],
                                   "generation_context": {"model": "wrong-report-model"}})
    assert "wrong-report-model" not in page.generation.toPlainText()
    page.generation_button.click()
    assert client.commands[-1][1]["report_id"] == "r2"
    emit(client, "review_detail", {"report_id": "r2", "review": item("r2")["review"],
                                   "generation_context": {"model": "right-report-model", "parameters": {}}})
    assert "right-report-model" in page.generation.toPlainText()
    assert "wrong-report-model" not in page.generation.toPlainText()
    page.close()


def test_generation_read_disconnect_preserves_notes_and_does_not_auto_retry(qt_app):
    client, page = populated(qt_app)
    page.notes.setPlainText("断线也不能丢失这条笔记")
    page.generation_button.click()
    count = len(client.commands)
    client.request_failed.emit("research-review_detail", "disconnected")
    qt_app.processEvents()
    assert len(client.commands) == count
    assert page.notes.toPlainText() == "断线也不能丢失这条笔记"
    assert page.generation_button.isEnabled()
    page.generation_button.click()
    assert len(client.commands) == count + 1
    emit(client, "review_detail", {"report_id": "r1", "review": item()["review"],
                                   "generation_context": None})
    assert page.notes.toPlainText() == "断线也不能丢失这条笔记"
    page.close()
