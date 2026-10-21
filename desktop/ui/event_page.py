"""Investment event calendar: local reads, explicit refresh, versioned manual input."""
from __future__ import annotations

import html
import json
import uuid
from datetime import date
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from .client import GuardianClient
from .models import ActionReceipt

_TYPES = {"earnings": "财报", "announcement": "公告", "dividend": "分红", "split": "拆股"}
_STATES = {"expected": "预计", "confirmed": "来源已确认", "cancelled": "已取消"}
_DATE_KINDS = {"event": "发生日期", "declaration": "公告日", "ex_dividend": "除息日", "record": "登记日", "payment": "派息日"}
_SOURCE_STATES = {"running": "正在刷新", "success": "刷新成功", "empty": "成功，范围内无历史事件",
                  "failed": "刷新失败，保留旧记录", "cancelled": "已停止，保留旧记录", "interrupted": "刷新中断，保留旧记录"}


class EventCalendarPage(QWidget):
    def __init__(self, client: GuardianClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self._active = False
        self._offset = 0
        self._limit = 20
        self._total = 0
        self._events: list[dict[str, Any]] = []
        self._query: dict[str, Any] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._selected: dict[str, Any] | None = None
        self._editing: dict[str, Any] | None = None
        self._running = False
        self._history_offset = 0
        self._history_total = 0
        self._navigation_notice = ""
        self._saving_form: tuple[str, ...] | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._poll)
        root = QVBoxLayout(self)
        intro = QLabel("事件日历 · 阅读与录入仅访问本地。主动刷新仅获取港/美股近一年历史分红、拆股；另可主动获取美股 SEC 已提交披露；未来财报仍需人工提供。")
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.instrument_input = QLineEdit()
        self.instrument_input.setPlaceholderText("全部证券 / US:AAPL / HK:00700")
        self.instrument_input.setAccessibleName("事件证券筛选")
        self.type_filter = QComboBox()
        self.type_filter.addItem("全部事件", "")
        for key, label in _TYPES.items():
            self.type_filter.addItem(label, key)
        self.date_from = QLineEdit()
        self.date_from.setPlaceholderText("开始日期 YYYY-MM-DD")
        self.date_to = QLineEdit()
        self.date_to.setPlaceholderText("结束日期 YYYY-MM-DD")
        filters = QHBoxLayout()
        for widget in (self.instrument_input, self.type_filter, self.date_from, self.date_to):
            filters.addWidget(widget)
        root.addLayout(filters)
        self.query_button = QPushButton("查询本地事件")
        self.refresh_button = QPushButton("刷新所选证券历史事件（联网）")
        self.sec_button = QPushButton("刷新 SEC 披露（联网）")
        self.stop_button = QPushButton("停止刷新")
        self.stop_button.setEnabled(False)
        buttons = QHBoxLayout()
        for button in (self.query_button, self.refresh_button, self.sec_button, self.stop_button):
            buttons.addWidget(button)
        root.addLayout(buttons)
        self.message = QLabel("尚未读取；没有自动扫描或通知。")
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        self.source_label = QLabel("自动来源尚未运行；不保证港美覆盖完整，不提供未来事件提醒。")
        self.source_label.setWordWrap(True)
        root.addWidget(self.source_label)
        self.coverage_detail = QTextBrowser()
        self.coverage_detail.setOpenLinks(False)
        self.coverage_detail.setOpenExternalLinks(False)
        self.coverage_detail.setMinimumHeight(100)
        self.coverage_detail.setMaximumHeight(180)
        self.coverage_detail.setPlainText("选择单个证券查询，可查看各事件类型和日期范围的来源覆盖。")
        root.addWidget(self.coverage_detail)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["日期", "证券", "类型", "状态", "标题", "来源方式"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(150)
        root.addWidget(self.table)
        page_row = QHBoxLayout()
        self.previous_button, self.next_button = QPushButton("上一页"), QPushButton("下一页")
        self.page_label = QLabel("0 条")
        for page_widget in (self.previous_button, self.page_label, self.next_button):
            page_row.addWidget(page_widget)
        root.addLayout(page_row)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        detail_box = QWidget()
        detail_layout = QVBoxLayout(detail_box)
        self.detail = QTextBrowser()
        self.detail.setOpenLinks(False)
        self.detail.anchorClicked.connect(self._open_source)
        detail_layout.addWidget(self.detail)
        history_row = QHBoxLayout()
        self.history_previous = QPushButton("较新修订")
        self.history_next = QPushButton("更早修订")
        self.history_label = QLabel("选中事件查看修订")
        for history_widget in (self.history_previous, self.history_label, self.history_next):
            history_row.addWidget(history_widget)
        detail_layout.addLayout(history_row)
        splitter.addWidget(detail_box)
        forms = QTabWidget()
        forms.addTab(self._manual_form(), "人工事件 / 修订")
        import_page = QWidget()
        import_layout = QVBoxLayout(import_page)
        import_layout.addWidget(QLabel("粘贴 JSON 数组（最多 100 条 / 120 KB）。source_event_id 保持不变才能关联修订。"))
        self.import_text = QPlainTextEdit()
        self.import_text.setPlaceholderText('[{"instrument":{"market":"US","ticker":"AAPL"},"event_type":"earnings",\n"source":"company-ir","source_event_id":"fy2026-q4",\n"source_url":"https://example.org/results","title":"财报","event_date":"2026-10-30"}]')
        import_layout.addWidget(self.import_text)
        self.import_button = QPushButton("导入到本地")
        self.import_button.clicked.connect(self._import)
        import_layout.addWidget(self.import_button)
        forms.addTab(import_page, "JSON 导入")
        splitter.addWidget(forms)
        root.addWidget(splitter, 1)
        self.query_button.clicked.connect(self._apply_query)
        self.refresh_button.clicked.connect(self._refresh)
        self.sec_button.clicked.connect(self._refresh_sec)
        self.stop_button.clicked.connect(lambda: self._send("event_stop", {}))
        self.previous_button.clicked.connect(lambda: self._change_page(-1))
        self.next_button.clicked.connect(lambda: self._change_page(1))
        self.history_previous.clicked.connect(lambda: self._change_history(-1))
        self.history_next.clicked.connect(lambda: self._change_history(1))
        self.table.itemSelectionChanged.connect(self._select)
        client.action_completed.connect(self._completed)
        client.request_failed.connect(self._failed)
        self._saved_form = self._form_signature()
        self._controls()

    def _manual_form(self) -> QWidget:
        widget = QWidget()
        layout = QFormLayout(widget)
        self.manual_instrument = QLineEdit()
        self.manual_instrument.setPlaceholderText("US:AAPL")
        self.manual_type = QComboBox()
        for key, label in _TYPES.items():
            self.manual_type.addItem(label, key)
        self.manual_title = QLineEdit()
        self.manual_source = QLineEdit("manual")
        self.manual_id = QLineEdit(uuid.uuid4().hex)
        self.manual_url = QLineEdit()
        self.manual_url.setPlaceholderText("公开原文 URL")
        self.manual_date = QLineEdit(date.today().isoformat())
        self.manual_date_kind = QComboBox()
        for key, label in _DATE_KINDS.items():
            self.manual_date_kind.addItem(label, key)
        self.manual_time = QLineEdit()
        self.manual_time.setPlaceholderText("可留空；已知时填写带时区 ISO 时刻")
        self.manual_timezone = QLineEdit("UTC")
        self.manual_published = QLineEdit()
        self.manual_published.setPlaceholderText("发布时间未知请留空；已知须带时区")
        self.manual_status = QComboBox()
        for key, label in _STATES.items():
            self.manual_status.addItem(label, key)
        self.manual_notes = QLineEdit()
        for label, field in (("证券", self.manual_instrument), ("类型", self.manual_type), ("标题", self.manual_title),
                             ("来源名称", self.manual_source), ("稳定来源事件 ID", self.manual_id), ("来源链接", self.manual_url),
                             ("日期", self.manual_date), ("日期含义", self.manual_date_kind), ("精确时刻（可空）", self.manual_time),
                             ("事件时区", self.manual_timezone), ("发布时刻（可空）", self.manual_published),
                             ("状态", self.manual_status), ("备注", self.manual_notes)):
            layout.addRow(label, field)
        actions = QHBoxLayout()
        self.new_button, self.save_button = QPushButton("新建事件"), QPushButton("保存人工事件")
        self.new_button.clicked.connect(self._new)
        self.save_button.clicked.connect(self._save)
        actions.addWidget(self.new_button)
        actions.addWidget(self.save_button)
        layout.addRow(actions)
        return widget

    def activate(self) -> None:
        self._active = True
        query = self._filter_values()
        if query != self._query:
            self._query = query
            self._offset = 0
        self._read()
        self._send("event_status", {})

    def deactivate(self) -> None:
        self._active = False
        self._timer.stop()

    def set_snapshot(self, snapshot: object) -> None:
        """Calendar state is read through command receipts, not cockpit snapshots."""

    def set_instrument(self, key: str) -> None:
        key = key.strip().upper()
        if key == self.instrument_input.text().strip().upper():
            return
        if self._form_signature() != self._saved_form:
            self._navigation_notice = "事件草稿有未保存修改，尚未切换证券；请先保存，或点“新建事件”放弃当前草稿后再切换。"
            self.message.setText(self._navigation_notice)
            return
        self._new()
        self.instrument_input.setText(key)
        self.manual_instrument.setText(key)
        self.manual_timezone.setText("Asia/Hong_Kong" if key.startswith("HK:") else "Asia/Shanghai" if key.startswith("CN:") else "America/New_York")
        self._saved_form = self._form_signature()
        if self._active:
            self._apply_query()

    def _form_signature(self) -> tuple[str, ...]:
        fields = (self.manual_instrument, self.manual_title, self.manual_source, self.manual_id,
                  self.manual_url, self.manual_date, self.manual_time, self.manual_timezone,
                  self.manual_published, self.manual_notes)
        choices = (self.manual_type, self.manual_date_kind, self.manual_status)
        return (*[field.text() for field in fields], *[str(choice.currentData()) for choice in choices])

    def _send(self, command: str, fields: dict[str, Any]) -> None:
        if command in self._pending:
            return
        self._pending[command] = dict(fields)
        self._controls()
        self.client.research_command(command, fields)

    def _filter_values(self) -> dict[str, Any]:
        return {key: value for key, value in {
            "instrument": self.instrument_input.text().strip().upper(), "event_type": self.type_filter.currentData(),
            "date_from": self.date_from.text().strip(), "date_to": self.date_to.text().strip(),
        }.items() if value}

    def _apply_query(self) -> None:
        self._offset = 0
        self._query = self._filter_values()
        self._read()

    def _read(self) -> None:
        self._send("event_list", {**self._query, "offset": self._offset, "limit": self._limit})

    def _change_page(self, direction: int) -> None:
        self._offset = max(0, self._offset + direction * self._limit)
        self._read()

    def _refresh(self) -> None:
        key = self.instrument_input.text().strip().upper()
        if not key or ":" not in key:
            self.message.setText("请输入单个证券，如 US:AAPL；自动来源目前仅支持港股/美股历史行动。")
            return
        self.message.setText("已请求联网刷新；只读取历史分红/拆股，旧记录保留。")
        self._send("event_refresh", {"instrument": key})

    def _refresh_sec(self) -> None:
        key = self.instrument_input.text().strip().upper()
        if not key.startswith("US:") or not key[3:]:
            self.message.setText("SEC 披露仅支持美股，请选择 US:SPCX 等单个证券。")
            return
        self.message.setText("已请求 SEC 已提交披露；不包含未来公告计划，旧记录保留。")
        self._send("event_refresh", {"instrument": key, "source": "sec_submissions"})

    def _poll(self) -> None:
        if self._active:
            self._send("event_status", {})

    def _save(self) -> None:
        if "event_save" in self._pending:
            return
        parts = self.manual_instrument.text().strip().upper().split(":")
        if len(parts) != 2:
            self.message.setText("证券格式应为 US:AAPL、HK:00700 或 CN:600519。")
            return
        kind = self.manual_type.currentData()
        date_kind = self.manual_date_kind.currentData()
        payload: dict[str, Any] = {
            "instrument": {"market": parts[0], "ticker": parts[1]}, "event_type": kind,
            "source": self.manual_source.text().strip(), "source_event_id": self.manual_id.text().strip(),
            "source_url": self.manual_url.text().strip(), "title": self.manual_title.text().strip(),
            "event_date": self.manual_date.text().strip(), "event_at": self.manual_time.text().strip() or None,
            "event_timezone": self.manual_timezone.text().strip(), "published_at": self.manual_published.text().strip() or None,
            "date_kind": date_kind, "status": self.manual_status.currentData(), "notes": self.manual_notes.text().strip(),
        }
        if kind == "dividend":
            if date_kind == "event":
                self.message.setText("分红必须选择公告日、除息日、登记日或派息日。")
                return
            previous = (self._editing or {}).get("dividend_dates") or {}
            payload["dividend_dates"] = {**previous, date_kind: payload["event_date"]}
            if self._editing and self._editing.get("amount") is not None:
                payload.update(amount=self._editing["amount"], currency=self._editing.get("currency"))
        self._saving_form = self._form_signature()
        self._send("event_save", {"event": payload})

    def _import(self) -> None:
        value = self.import_text.toPlainText()
        if len(value.encode()) > 120_000:
            self.message.setText("导入内容超过 120 KB，请拆分后重试。")
            return
        try:
            items = json.loads(value)
            if not isinstance(items, list) or not 1 <= len(items) <= 100:
                raise ValueError
        except ValueError:
            self.message.setText("请输入 1～100 条事件组成的有效 JSON 数组。")
            return
        self._send("event_import", {"items": items})

    def _new(self) -> None:
        self._selected = None
        self._editing = None
        self.manual_id.setText(uuid.uuid4().hex)
        self.manual_source.setText("manual")
        self.manual_title.clear()
        self.manual_url.clear()
        self.manual_time.clear()
        self.manual_published.clear()
        self.manual_notes.clear()
        self.manual_status.setCurrentIndex(0)
        self._saved_form = self._form_signature()
        self._navigation_notice = ""
        self._controls()

    def _select(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._events):
            self._selected = None
            return
        event = self._events[row]
        self._selected = event
        self._editing = event
        self._history_offset = 0
        self._history_total = 0
        self._show_details([event])
        for field, value in ((self.manual_instrument, _key(event)), (self.manual_title, event.get("title")),
                             (self.manual_source, event.get("source")), (self.manual_id, event.get("source_event_id")),
                             (self.manual_url, event.get("source_url")), (self.manual_date, event.get("event_date")),
                             (self.manual_time, event.get("event_at")), (self.manual_timezone, event.get("event_timezone")),
                             (self.manual_published, event.get("published_at")), (self.manual_notes, event.get("notes"))):
            field.setText(str(value or ""))
        for combo, key in ((self.manual_type, "event_type"), (self.manual_date_kind, "date_kind"), (self.manual_status, "status")):
            combo.setCurrentIndex(max(0, combo.findData(event.get(key))))
        self._saved_form = self._form_signature()
        self._navigation_notice = ""
        self._read_history()
        self._controls()

    def _read_history(self) -> None:
        if self._selected:
            self._send("event_history", {"event_id": self._selected["event_id"], "offset": self._history_offset, "limit": 5})

    def _change_history(self, direction: int) -> None:
        self._history_offset = max(0, self._history_offset + direction * 5)
        self._read_history()

    def _show_details(self, events: list[dict[str, Any]]) -> None:
        sections = []
        for event in events:
            lines = [f"修订 {event.get('version')} · {event.get('title', '')}",
                     f"{_key(event)} · {_TYPES.get(str(event.get('event_type') or ''), '')} · {_STATES.get(str(event.get('status') or ''), '')}",
                     f"{_DATE_KINDS.get(str(event.get('date_kind') or ''), '发生日期')}：{event.get('event_date')}",
                     f"精确时刻：{event.get('event_at') or '时间未知（仅日期）'}；时区：{event.get('event_timezone')}",
                     f"发布时间：{event.get('published_at') or '未知'}", f"本版本首次观测：{event.get('observed_at')}",
                     f"来源：{event.get('source')}；来源ID：{event.get('source_event_id')}"]
            for kind, day in (event.get("dividend_dates") or {}).items():
                lines.append(f"{_DATE_KINDS.get(kind, kind)}：{day or '未知'}")
            if event.get("amount") is not None:
                lines.append(f"金额：{event['amount']} {event.get('currency')}")
            filing = event.get("filing") or {}
            if filing:
                lines.extend([f"表单：{filing.get('form')}；CIK：{filing.get('cik')}",
                              f"报告期：{filing.get('report_date') or '未知'}；修订稿：{'是' if filing.get('amended') else '否'}",
                              f"接收时间原值：{filing.get('acceptance_original') or '未知'}；精度：{filing.get('acceptance_precision')}",
                              f"证券映射来源：{filing.get('mapping_source')}"])
            lines.append(str(event.get("notes") or ""))
            url = html.escape(str(event.get("source_url") or ""), quote=True)
            sections.append("<p>" + "<br>".join(html.escape(line) for line in lines) + f'<br><a href="{url}">打开来源（浏览器）</a></p>')
        self.detail.setHtml("<hr>".join(sections))

    def _completed(self, receipt: object) -> None:
        if not isinstance(receipt, ActionReceipt) or not receipt.action.startswith("research-event_"):
            return
        command = receipt.action.removeprefix("research-")
        request = self._pending.pop(command, None)
        if request is None:
            return
        result = receipt.result or {}
        if not receipt.accepted:
            self.message.setText(receipt.message or "事件操作未完成，已保留原数据。")
        elif command == "event_list":
            wanted = {**self._query, "offset": self._offset, "limit": self._limit}
            if request != wanted:
                self._read()
                return
            self._events = list(result.get("events") or [])
            self._total = int(result.get("total") or 0)
            self.table.blockSignals(True)
            self.table.setRowCount(len(self._events))
            for row, event in enumerate(self._events):
                values = (event.get("event_date"), _key(event), _TYPES.get(str(event.get("event_type") or "")),
                          _STATES.get(str(event.get("status") or "")), event.get("title"),
                          "自动来源" if event.get("provenance") == "automatic" else "人工提供")
                for col, value in enumerate(values):
                    self.table.setItem(row, col, QTableWidgetItem(str(value or "")))
            self.table.blockSignals(False)
            self._selected = None
            self.detail.clear()
            self.page_label.setText(f"{self._offset + 1 if self._events else 0}–{self._offset + len(self._events)} / {self._total} 条")
            self.message.setText(self._navigation_notice or (
                "本地查询完成。" if self._events else "本地没有符合筛选的记录；不代表来源已经采集或没有事件。"))
            self._show_coverage(result.get("query_coverage"), result.get("filing_coverage"))
        elif command == "event_history":
            if self._selected and (request["event_id"] != self._selected["event_id"] or request["offset"] != self._history_offset):
                self._read_history()
                return
            if self._selected:
                self._show_details(list(result.get("events") or []))
                self._history_total = int(result.get("total") or 0)
                self.history_label.setText(f"修订 {self._history_offset + 1}–{min(self._history_offset + 5, self._history_total)} / {self._history_total}")
        elif command == "event_status":
            was_running = self._running
            self._running = result.get("running") is True
            states = result.get("sources") or []
            self.source_label.setText("\n".join(
                f"{s.get('instrument')} · {s.get('source')}：{_SOURCE_STATES.get(s.get('status'), '状态未知')}；最后成功：{s.get('last_success_at') or '尚无'}"
                + (f"；原因：{s.get('detail')}" if s.get("detail") else "") for s in states
            ) or "自动来源尚未运行；未来财报需人工提供，不保证市场完整覆盖。")
            if self._running and self._active:
                self._timer.start()
            else:
                self._timer.stop()
            if was_running and not self._running and self._active:
                self._read()
        elif command == "event_refresh":
            self._running = True
            self._send("event_status", {})
            if self._active:
                self._timer.start()
        elif command == "event_stop":
            self.message.setText("正在停止，等待有界请求结束；尚未宣称已经停止。")
            self._send("event_status", {})
        elif command in {"event_save", "event_import"}:
            if command == "event_save" and result.get("events"):
                self._editing = result["events"][0]
                if self._saving_form is not None:
                    self._saved_form = self._saving_form
                self._saving_form = None
                if self._form_signature() == self._saved_form:
                    self._navigation_notice = ""
            self.message.setText(f"已保存 {result.get('saved', 0)} 条；新增/修订 {result.get('changed', 0)} 条，重复内容不刷新观测时间。")
            self._read()
        self._controls()

    def _show_coverage(self, value: Any, filings: Any = None) -> None:
        if not isinstance(value, dict) or value.get("status") == "selection_required":
            self.coverage_detail.setPlainText("选择单个证券查询，可查看各事件类型和日期范围的来源覆盖。")
            return
        states = {"success": "已查询", "empty": "已查无记录", "not_queried": "未查询",
                  "unsupported": "来源不支持", "failed": "查询失败", "stale": "缓存已过期",
                  "running": "刷新中", "unknown": "覆盖情况无法确认"}
        reasons = {"permission_required": "来源访问受限", "rate_limited": "来源限流",
                   "request_timeout": "请求超时", "cancelled": "已停止刷新",
                   "refresh_interrupted": "刷新中断", "coverage_query_limit": "历史范围过多，请缩小查询日期",
                   "coverage_record_invalid": "覆盖记录无效"}
        def esc(value: Any) -> str:
            return html.escape(str(value))
        lines = [f"<b>{esc(value.get('instrument', ''))} · 自动来源覆盖</b>",
                 "<p>Yahoo 历史行动；新鲜度预算 24 小时。以下范围仅截至取得时刻，不代表整个市场完整覆盖。</p>"]
        rows: list[dict[str, Any]] = [{**row, "_provider": "Yahoo"} for row in value.get("ranges") or []]
        if isinstance(filings, dict):
            lines.append("<p>SEC 已提交披露；新鲜度预算 6 小时。公告覆盖单独记录，不包含未来计划。</p>")
            rows.extend({**row, "_provider": "SEC"} for row in filings.get("ranges") or [])
        for row in rows:
            lines.append(f"<p><b>{esc(row.get('_provider'))} · {esc(_TYPES.get(str(row.get('event_type') or ''), '未知类型'))}："
                         f"{esc(states.get(str(row.get('status') or ''), '覆盖情况无法确认'))}</b> · "
                         f"{esc(row.get('date_from'))} 至 {esc(row.get('date_to'))}<br>"
                         f"最近查询：{esc(row.get('checked_at') or '未记录')}；"
                         f"上次成功取得：{esc(row.get('last_success_at') or '未记录')}</p>")
            if row.get("cache_source_at"):
                lines.append(f"<p>缓存来源时间：{esc(row['cache_source_at'])}</p>")
            if row.get("detail"):
                lines.append(f"<p>{esc(reasons.get(row['detail'], '来源未成功返回完整结果'))}</p>")
            if row.get("history_truncated"):
                lines.append("<p>仅核对最近 200 次范围记录；更早的成功时间可能未包含。</p>")
        self.coverage_detail.setHtml("".join(lines))

    def _failed(self, purpose: str, code: str) -> None:
        command = purpose.removeprefix("research-")
        if command not in self._pending:
            return
        self._pending.pop(command, None)
        if command == "event_save":
            self._saving_form = None
        self.message.setText("事件请求失败，保留上次记录与草稿；请重试。")
        self._controls()

    def _controls(self) -> None:
        self.previous_button.setEnabled(self._offset > 0 and "event_list" not in self._pending)
        self.next_button.setEnabled(self._offset + self._limit < self._total and "event_list" not in self._pending)
        self.refresh_button.setEnabled(not self._running and "event_refresh" not in self._pending)
        self.sec_button.setEnabled(not self._running and "event_refresh" not in self._pending)
        self.stop_button.setEnabled(self._running and "event_stop" not in self._pending)
        self.save_button.setEnabled("event_save" not in self._pending and not (self._editing and self._editing.get("provenance") == "automatic"))
        self.import_button.setEnabled("event_import" not in self._pending)
        self.history_previous.setEnabled(self._selected is not None and self._history_offset > 0 and "event_history" not in self._pending)
        self.history_next.setEnabled(self._selected is not None and self._history_offset + 5 < self._history_total and "event_history" not in self._pending)

    def _open_source(self, url: QUrl) -> None:
        if url.scheme() in {"http", "https"} and url.host() and not url.userInfo():
            QDesktopServices.openUrl(url)


def _key(event: dict[str, Any]) -> str:
    instrument = event.get("instrument") or {}
    return f"{instrument.get('market', '')}:{instrument.get('ticker', '')}"
