"""Human research review and explicitly requested, local price follow-up."""
from __future__ import annotations

from datetime import datetime
import html
import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from .client import GuardianClient
from .generation import generation_html
from .models import ActionReceipt
from .widgets import PageHeading

STATUSES = {"pending": "待核验", "verified": "已核验", "deferred": "暂不采纳"}
FOLLOWUP = {"pending": "待样本", "excluded": "已排除", "observed": "已观察"}
REASONS = {
    "not_a_directional_candidate": "原报告不是方向性候选",
    "original_evidence_unavailable": "原始证据不可核实",
    "evidence_not_available_at_creation": "报告生成时证据尚不可见",
    "market_not_available_at_creation": "报告生成时行情尚不可见",
    "market_clock_inconsistent": "基准行情时间不一致",
    "market_dependency_not_available_at_creation": "报告生成时依赖数据尚不可见",
    "baseline_security_unverifiable": "基准证券不匹配",
    "market_stale_at_creation": "生成时行情已过期",
    "baseline_currency_unverifiable": "基准币种不可核实",
    "reference_price_unverifiable": "基准价格不可核实",
    "cn_intraday_not_validated": "A 股盘中数据尚未验收",
    "observation_window_not_complete": "尚未到报告观察时点",
    "end_price_unavailable": "目标时点附近没有可用样本",
    "conflicting_end_prices": "同一时点的价格样本冲突",
    "corporate_action_requires_adjustment": "公司行动需要复权处理",
    "price_change_unrepresentable": "价格变化不能可靠计算",
}
MAX_IMPORT_BYTES = 256_000
SAMPLE_EXAMPLE = [{
    "instrument": {"market": "US", "ticker": "AAPL"}, "price": 100.0,
    "currency": "USD", "source_as_of": "2026-10-01T16:00:00+00:00",
    "observed_at": "2026-10-01T16:01:00+00:00", "provider": "填写实际价格来源",
    "granularity": "daily", "corporate_action": False,
}]


class ReviewPage(QWidget):
    view_report_requested = Signal(str)

    def __init__(self, client: GuardianClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self._active = False
        self._rows: list[dict[str, Any]] = []
        self._selected: dict[str, Any] | None = None
        self._offset = 0
        self._page_size = 20
        self._has_more = False
        self._pending: set[str] = set()
        self._query_fields: dict[str, Any] = {}
        self._list_queued = False
        self._desired_report = ""
        self._detail_offset = 0
        self._original_visible = False
        self._generation_visible = False
        self._next_detail: int | None = None
        self._detail_queued = False
        self._dirty = False
        self._loading_form = False
        self._preferred_samples = ""
        self._samples_queued = False
        layout = QVBoxLayout(self)
        layout.addWidget(PageHeading("RESEARCH REVIEW", "核验与复盘", "保存自己的判断，再用明确来源的价格样本回看原报告。"))
        filters = QHBoxLayout()
        self.instrument_filter = QLineEdit()
        self.instrument_filter.setPlaceholderText("证券，例如 US:AAPL；留空为全部")
        self.status_filter = QComboBox()
        self.status_filter.addItem("全部核验状态", "")
        for key, label in STATUSES.items():
            self.status_filter.addItem(label, key)
        self.date_from = QLineEdit()
        self.date_from.setPlaceholderText("起始日期 YYYY-MM-DD")
        self.date_to = QLineEdit()
        self.date_to.setPlaceholderText("截止日期（不含当天）")
        self.search_button = QPushButton("查询历史")
        self.search_button.clicked.connect(lambda: self._load_list(reset=True))
        for widget in (self.instrument_filter, self.status_filter, self.date_from, self.date_to, self.search_button):
            filters.addWidget(widget)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["证券", "报告时间", "周期", "核验状态", "下次核验"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(160)
        self.table.itemSelectionChanged.connect(self._select_row)
        layout.addWidget(self.table)
        paging = QHBoxLayout()
        self.previous_button = QPushButton("上一页")
        self.next_button = QPushButton("下一页")
        self.page_label = QLabel("尚未读取")
        self.previous_button.clicked.connect(lambda: self._page(-1))
        self.next_button.clicked.connect(lambda: self._page(1))
        for pager_widget in (self.previous_button, self.page_label, self.next_button):
            paging.addWidget(pager_widget)
        layout.addLayout(paging)
        self.selection_label = QLabel("选择一份报告后填写核验记录。")
        self.selection_label.setWordWrap(True)
        layout.addWidget(self.selection_label)
        form = QFormLayout()
        self.review_status = QComboBox()
        for key, label in STATUSES.items():
            self.review_status.addItem(label, key)
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("记录核对过的来源、疑问或暂不采纳的原因（最多 4,000 字）")
        self.notes.setMaximumHeight(90)
        self.next_review_enabled = QCheckBox("安排下次核验")
        self.next_review = QDateTimeEdit(QDateTime.currentDateTime().addDays(7))
        self.next_review.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.next_review.setCalendarPopup(True)
        self.next_review.setEnabled(False)
        self.next_review_enabled.toggled.connect(self.next_review.setEnabled)
        form.addRow("人工核验", self.review_status)
        form.addRow("备注", self.notes)
        form.addRow(self.next_review_enabled, self.next_review)
        layout.addLayout(form)
        for signal in (self.review_status.currentIndexChanged, self.notes.textChanged,
                       self.next_review.dateTimeChanged, self.next_review_enabled.toggled):
            signal.connect(self._mark_dirty)
        actions = QHBoxLayout()
        self.save_button = QPushButton("保存核验记录")
        self.save_button.setProperty("primary", True)
        self.original_button = QPushButton("查看原始快照（高级）")
        self.read_report_button = QPushButton("阅读报告与引用")
        self.reload_button = QPushButton("重新读取所选记录")
        self.save_button.clicked.connect(self._save)
        self.original_button.clicked.connect(self._open_original)
        self.read_report_button.clicked.connect(self._read_report)
        self.reload_button.clicked.connect(self._reload_selected)
        for widget in (self.save_button, self.read_report_button, self.original_button, self.reload_button):
            actions.addWidget(widget)
        layout.addLayout(actions)
        self.generation_button = QPushButton("查看原报告生成配置")
        self.generation_button.clicked.connect(self._open_generation)
        layout.addWidget(self.generation_button)
        self.generation = QTextBrowser()
        self.generation.setOpenExternalLinks(False)
        self.generation.setMaximumHeight(180)
        self.generation.hide()
        layout.addWidget(self.generation)
        samples = QHBoxLayout()
        self.sample_sets = QComboBox()
        self.sample_sets.setMinimumWidth(250)
        self.import_button = QPushButton("导入价格样本 JSON")
        self.example_button = QPushButton("复制样本 JSON 示例")
        self.import_button.clicked.connect(self._choose_samples)
        self.example_button.clicked.connect(self._copy_example)
        for widget in (self.sample_sets, self.import_button, self.example_button):
            samples.addWidget(widget)
        layout.addLayout(samples)
        explanation = QLabel("样本为 JSON 数组，最多 500 条 / 256 KB。每条须有证券、价格、币种、来源名称、来源时间和观测时间；时间须含时区。")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        followup = QHBoxLayout()
        followup.addWidget(QLabel("复盘截止时间"))
        self.as_of = QDateTimeEdit(QDateTime.currentDateTime())
        self.as_of.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.as_of.setCalendarPopup(True)
        self.run_button = QPushButton("运行所选报告复盘")
        self.run_button.clicked.connect(self._run)
        followup.addWidget(self.as_of)
        followup.addWidget(self.run_button)
        layout.addLayout(followup)
        self.result = QTextBrowser()
        self.result.setMinimumHeight(130)
        self.result.setOpenExternalLinks(False)
        self.result.setPlainText("仅计算所提供样本的未复权价格变化，不是交易收益、胜率或模型准确率。")
        layout.addWidget(self.result, 1)
        self.original = QPlainTextEdit()
        self.original.setReadOnly(True)
        self.original.setMaximumHeight(150)
        self.original.hide()
        layout.addWidget(self.original)
        self.more_original_button = QPushButton("读取原始报告下一段")
        self.more_original_button.clicked.connect(self._more_original)
        self.more_original_button.hide()
        layout.addWidget(self.more_original_button)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.client.action_completed.connect(self._completed)
        self.client.request_failed.connect(self._failed)
        self._controls()

    def activate(self) -> None:
        self._active = True
        if not self._dirty:
            self._load_list()
        self._load_samples()

    def deactivate(self) -> None:
        self._active = False

    def set_snapshot(self, _snapshot: object) -> None:
        # Research refreshes never reset a human's draft or run a follow-up.
        pass

    def set_instrument(self, key: str) -> None:
        if self.instrument_filter.text().strip() == key:
            return
        if self._dirty:
            self.message.setText("核验记录有未保存修改；请先保存，或重新读取所选记录后切换证券。")
            return
        self._selected = None
        self._generation_visible = False
        self.generation.hide()
        self._desired_report = ""
        self._loading_form = True
        self.notes.clear()
        self._loading_form = False
        self.original.hide()
        self.more_original_button.hide()
        self.selection_label.setText("选择一份报告后填写核验记录。")
        self._show_followup(None)
        self._controls()
        self.instrument_filter.setText(key)
        self._offset = 0
        if self._active:
            self._load_list()

    def set_report(self, report_id: str) -> None:
        if self._dirty:
            self.message.setText("核验记录有未保存修改；请先保存，或重新读取所选记录后切换报告。")
            return
        self._original_visible = False
        self._generation_visible = False
        self.generation.hide()
        self.original.hide()
        self.more_original_button.hide()
        self._desired_report = report_id
        self._selected = None
        self._dirty = False
        self._detail_offset = 0
        self._request_detail()

    def _mark_dirty(self, *_args: object) -> None:
        if not self._loading_form:
            self._dirty = True

    def _send(self, command: str, fields: dict[str, Any]) -> None:
        if command in self._pending:
            return
        self._pending.add(command)
        self._controls()
        self.client.research_command(command, fields)

    def _filters(self) -> dict[str, Any]:
        fields: dict[str, Any] = {"instrument": self.instrument_filter.text().strip(),
                                  "status": self.status_filter.currentData(),
                                  "offset": self._offset, "limit": self._page_size}
        for key, widget in (("created_from", self.date_from), ("created_to", self.date_to)):
            if widget.text().strip():
                parsed = datetime.strptime(widget.text().strip(), "%Y-%m-%d").astimezone()
                fields[key] = parsed.isoformat()
        return fields

    def _load_list(self, *, reset: bool = False) -> None:
        if reset:
            self._offset = 0
        if "review_list" in self._pending:
            self._list_queued = True
            return
        try:
            self._query_fields = self._filters()
        except ValueError:
            self.message.setText("日期请填写 YYYY-MM-DD。")
            return
        self._send("review_list", self._query_fields)

    def _page(self, direction: int) -> None:
        self._offset = max(0, self._offset - self._page_size) if direction < 0 else self._offset + len(self._rows)
        self._load_list()

    def _select_row(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self._rows):
            return
        if self._dirty and self._selected and self._rows[row]["report_id"] != self._selected["report_id"]:
            self.message.setText("核验记录有未保存修改；请先保存，或重新读取所选记录后切换报告。")
            previous = next((index for index, item in enumerate(self._rows)
                             if item["report_id"] == self._selected["report_id"]), None)
            self.table.blockSignals(True)
            self.table.clearSelection()
            if previous is not None:
                self.table.selectRow(previous)
            self.table.blockSignals(False)
            return
        self._selected = self._rows[row]
        self._generation_visible = False
        self.generation.hide()
        self._desired_report = str(self._selected["report_id"])
        self._dirty = False
        self._fill_review()
        self._show_followup(self._selected.get("last_followup"))
        self._original_visible = False
        self.original.hide()
        self.more_original_button.hide()
        self._controls()

    def _fill_review(self) -> None:
        if self._selected is None:
            return
        record = self._selected.get("review") or {}
        self._loading_form = True
        self.review_status.setCurrentIndex(max(0, self.review_status.findData(record.get("status", "pending"))))
        self.notes.setPlainText(str(record.get("notes") or ""))
        next_at = record.get("next_review_at")
        self.next_review_enabled.setChecked(bool(next_at))
        if next_at:
            self.next_review.setDateTime(QDateTime.fromString(str(next_at), Qt.DateFormat.ISODate).toLocalTime())
        self.selection_label.setText(f"原始报告：{self._selected['report_id']} · {str(self._selected.get('summary') or '')[:400]}")
        self._loading_form = False

    def _save(self) -> None:
        if self._selected is None:
            return
        self._send("review_save", {"report_id": self._selected["report_id"],
                                   "status": self.review_status.currentData(), "notes": self.notes.toPlainText(),
                                   "next_review_at": (self.next_review.dateTime().toUTC().toString(Qt.DateFormat.ISODate)
                                                      if self.next_review_enabled.isChecked() else None),
                                   "revision": (self._selected.get("review") or {}).get("revision", 0)})

    def _open_original(self) -> None:
        if self._selected:
            self._original_visible = True
            self._desired_report = self._selected["report_id"]
            self._detail_offset = 0
            self._request_detail()

    def _read_report(self) -> None:
        if self._selected:
            self.view_report_requested.emit(self._selected["report_id"])

    def _open_generation(self) -> None:
        if self._selected:
            self._generation_visible = True
            self.generation.setPlainText("正在读取原报告的生成配置…")
            self.generation.show()
            self._desired_report = self._selected["report_id"]
            self._request_detail()

    def _reload_selected(self) -> None:
        if self._desired_report:
            self._dirty = False
            self._detail_offset = 0
            self._request_detail()

    def _request_detail(self) -> None:
        if "review_detail" in self._pending:
            self._detail_queued = True
            return
        self._send("review_detail", {"report_id": self._desired_report, "offset": self._detail_offset})

    def _more_original(self) -> None:
        if self._next_detail is not None:
            self._detail_offset = self._next_detail
            self._request_detail()

    def _copy_example(self) -> None:
        QApplication.clipboard().setText(json.dumps(SAMPLE_EXAMPLE, ensure_ascii=False, indent=2))
        self.message.setText("格式示例已复制。请替换为自己的真实价格与来源，再保存为 .json 文件；示例不会自动导入。")

    def _load_samples(self) -> None:
        if "replay_list" in self._pending:
            self._samples_queued = True
        else:
            self._send("replay_list", {})

    def _choose_samples(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "选择价格样本", "", "JSON (*.json)")
        if selected:
            self.import_file(Path(selected))

    def import_file(self, path: Path) -> None:
        try:
            if path.suffix.lower() != ".json":
                raise ValueError("json required")
            with path.open("rb") as source:
                content = source.read(MAX_IMPORT_BYTES + 1)
            if len(content) > MAX_IMPORT_BYTES:
                raise ValueError("too large")
            text = content.decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            self.message.setText("请选择 UTF-8 JSON 文件，大小不能超过 256 KB。")
            return
        self._send("replay_import", {"source_label": path.name, "json_text": text})

    def _run(self) -> None:
        if self._selected is not None and self.sample_sets.currentData():
            self._send("replay_run", {"report_id": self._selected["report_id"],
                                      "sample_set_id": self.sample_sets.currentData(),
                                      "as_of": self.as_of.dateTime().toUTC().toString(Qt.DateFormat.ISODate)})

    def _show_followup(self, record: Any) -> None:
        if not isinstance(record, dict):
            self.result.setPlainText("尚无已保存复盘。导入样本后点击运行；价格变化不是交易收益。")
            return
        row = record.get("result") or {}
        change = row.get("price_change_pct")
        lines = [FOLLOWUP.get(str(row.get("status")), "状态未知"),
                 REASONS.get(str(row.get("reason")), str(row.get("reason") or "")),
                 f"样本来源：{record.get('source_label', '')}；{record.get('sample_count', 0)} 条",
                 f"截止时间：{record.get('as_of', '')}",
                 f"基准价格：{row.get('reference_price') if row.get('reference_price') is not None else '未知'}；来源时刻：{row.get('baseline_source_as_of') or '未知'}；观测时刻：{row.get('baseline_observed_at') or '未知'}",
                 f"目标时刻：{row.get('target_at') or '未知'}",
                 f"后续价格：{row.get('end_price') if row.get('end_price') is not None else '未取得'}；来源：{row.get('end_provider') or '未取得'}；来源时刻：{row.get('end_source_as_of') or '未取得'}；观测时刻：{row.get('end_observed_at') or '未取得'}",
                 f"未复权价格变化：{float(change):.2f}%" if isinstance(change, (int, float)) else "未复权价格变化：无可核实结果",
                 "样本由用户提供，尚未独立核实；结果不是交易收益、胜率或模型准确率。"]
        self.result.setHtml("<br>".join(html.escape(line) for line in lines if line))

    def _completed(self, receipt: object) -> None:
        if not isinstance(receipt, ActionReceipt):
            return
        command = receipt.action.removeprefix("research-")
        if command not in self._pending:
            return
        self._pending.discard(command)
        result = receipt.result
        if not receipt.accepted or not isinstance(result, dict):
            self.message.setText(receipt.message or "操作未完成；保留当前内容，请重试。")
            self._controls()
            return
        if command == "review_list":
            if self._list_queued:
                self._list_queued = False
                self._load_list()
                return
            self._rows = result.get("items") or []
            self._offset = int(result.get("offset", 0))
            self._has_more = result.get("has_more") is True
            self.table.blockSignals(True)
            self.table.setRowCount(len(self._rows))
            selected_row = -1
            for index, item in enumerate(self._rows):
                review = item.get("review") or {}
                values = [item.get("instrument_key", ""), item.get("created_at", ""), item.get("horizon", ""),
                          STATUSES.get(str(review.get("status")), "待核验"), review.get("next_review_at") or "未设定"]
                for column, value in enumerate(values):
                    self.table.setItem(index, column, QTableWidgetItem(str(value)))
                if item["report_id"] == self._desired_report:
                    selected_row = index
                    if not self._dirty:
                        self._selected = item
            if selected_row >= 0:
                self.table.selectRow(selected_row)
            self.table.blockSignals(False)
            if self._selected and not self._dirty:
                self._fill_review()
            self.page_label.setText(f"共 {result.get('total', 0)} 份 · 当前 {self._offset + 1 if self._rows else 0}–{self._offset + len(self._rows)}")
        elif command == "review_save":
            if self._selected and result.get("report_id") == self._selected["report_id"]:
                self._selected["review"] = result
                self._dirty = False
                self._fill_review()
            self.message.setText("核验记录已保存，原报告保持原样。")
            self._load_list()
        elif command == "review_detail":
            if result.get("report_id") == self._desired_report:
                if self._selected is None or self._selected.get("report_id") != self._desired_report:
                    self._selected = {"report_id": self._desired_report, "review": result.get("review") or {}}
                elif not self._dirty:
                    self._selected["review"] = result.get("review") or {}
                if not self._dirty:
                    self._fill_review()
                self.original.setPlainText(str(result.get("content") or ""))
                self.original.setVisible(self._original_visible)
                self._next_detail = result.get("next_offset")
                self.more_original_button.setVisible(self._original_visible and self._next_detail is not None)
                self._show_followup(result.get("last_followup"))
                if self._generation_visible:
                    self.generation.setHtml(generation_html(result.get("generation_context")))
            if self._detail_queued:
                self._detail_queued = False
                self._request_detail()
        elif command == "replay_import":
            self._preferred_samples = str(result.get("id") or "")
            self.message.setText(f"已导入 {result.get('sample_count', 0)} 条样本；尚未运行复盘。")
            self._load_samples()
        elif command == "replay_list":
            if self._samples_queued:
                self._samples_queued = False
                self._load_samples()
                return
            selected = self._preferred_samples or self.sample_sets.currentData()
            self.sample_sets.clear()
            for item in result.get("items") or []:
                self.sample_sets.addItem(f"{item['source_label']} · {item['sample_count']} 条 · {item['imported_at']}", item["id"])
            index = self.sample_sets.findData(selected)
            if index >= 0:
                self.sample_sets.setCurrentIndex(index)
            if result.get("truncated"):
                self.message.setText("样本集选择器显示最近 50 次导入。")
        elif command == "replay_run":
            if result.get("report_id") == self._desired_report:
                self._show_followup(result)
                if self._selected:
                    self._selected["last_followup"] = result
            self.message.setText("复盘结果已保存；没有修改原报告或生成交易动作。")
        self._controls()

    def _failed(self, purpose: str, _code: str) -> None:
        command = purpose.removeprefix("research-")
        if command in self._pending:
            self._pending.discard(command)
            self.message.setText("读取或保存失败，当前内容已保留；请重新读取后重试。")
            self._controls()

    def _controls(self) -> None:
        selected = self._selected is not None
        self.save_button.setEnabled(selected and "review_save" not in self._pending and "review_detail" not in self._pending)
        self.original_button.setEnabled(selected and "review_detail" not in self._pending)
        self.read_report_button.setEnabled(selected)
        self.generation_button.setEnabled(selected and "review_detail" not in self._pending)
        self.reload_button.setEnabled(bool(self._desired_report) and "review_detail" not in self._pending)
        self.previous_button.setEnabled(self._offset > 0 and "review_list" not in self._pending)
        self.next_button.setEnabled(self._has_more and "review_list" not in self._pending)
        self.run_button.setEnabled(selected and bool(self.sample_sets.currentData()) and "replay_run" not in self._pending)
        self.import_button.setEnabled("replay_import" not in self._pending)
