"""Read persisted batches and explicitly resume compatible unfinished work."""
from __future__ import annotations

import html
from typing import Any
from urllib.parse import quote, unquote

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from .client import GuardianClient
from .models import ActionReceipt
from .widgets import PageHeading

STATES = {"running": "处理中", "completed": "已完成", "success": "已完成", "partial": "部分完成",
          "cancelled": "已取消", "interrupted": "已中断", "pending": "待处理", "failed": "失败",
          "uncertain": "调用结果未确认", "needs_configuration": "需要配置"}
REASONS = {
    "run_not_found": "找不到该批次，请刷新列表。", "run_active": "该批次仍在运行。",
    "collection_not_resumable": "采集阶段未完成，请从研究页重新扫描。",
    "mode_not_resumable": "只支持单次扫描的分析阶段续跑，请重新扫描。",
    "configuration_changed": "配置已变化，请按当前配置重新扫描。",
    "batch_date_expired": "已跨过原批次的 UTC 研究日期，请重新扫描。",
    "material_changed": "来源材料版本已变化，请重新扫描。",
    "market_snapshot_expired": "原行情快照已过期，请重新扫描取得新行情。",
    "model_budget_exhausted": "本批次模型调用预算已用完；如需继续，请设置预算后新建扫描。",
    "nothing_remaining": "该批次没有待处理材料。",
    "uncertain_model_call": "有调用已发出但结果未确认，重试可能产生额外费用。",
    "result_unconfirmed": "调用结果未确认", "process_interrupted": "进程中断",
    "analysis_failed": "分析失败", "analysis_cancelled": "分析已取消",
    "model_unavailable": "模型未配置或不可用；补齐配置后再继续。",
    "material_missing": "原材料已不存在，请重新扫描。", "cancelled": "分析已取消",
    "model_call_unconfirmed": "模型调用结果未确认",
}


def _text(value: Any) -> str:
    return html.escape(str(value)) if value is not None else "未记录"


class RunProgressPage(QWidget):
    report_requested = Signal(str)

    def __init__(self, client: GuardianClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self._active = False
        self._connection_failed = False
        self._pending: dict[str, dict[str, Any]] = {}
        self._rows: list[dict[str, Any]] = []
        self._run: dict[str, Any] | None = None
        self._run_id = ""
        self._offset = 0
        self._next: int | None = None
        self._detail_offset = 0
        self._detail_next: int | None = None
        self._detail_valid = False
        self._report_ids: set[str] = set()
        root = QVBoxLayout(self)
        root.addWidget(PageHeading("RESEARCH RUNS", "任务与续跑", "查看本地进度；继续只处理原批次中仍未完成且适用的材料。"))
        actions = QHBoxLayout()
        self.refresh_button = QPushButton("刷新本地进度")
        self.previous_button = QPushButton("上一页")
        self.next_button = QPushButton("下一页")
        self.page_label = QLabel("尚未读取")
        self.refresh_button.clicked.connect(self.refresh)
        self.previous_button.clicked.connect(lambda: self._page(-1))
        self.next_button.clicked.connect(lambda: self._page(1))
        for widget in (self.refresh_button, self.previous_button, self.page_label, self.next_button):
            actions.addWidget(widget)
        root.addLayout(actions)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["开始时间", "状态", "完成 / 总数", "失败 / 未确认", "模型调用 / 上限"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(150)
        self.table.setMaximumHeight(240)
        self.table.itemSelectionChanged.connect(self._select)
        root.addWidget(self.table)
        self.summary = QLabel("选择一批任务查看材料进度。")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.ack = QCheckBox("我理解未确认调用可能已经计费，继续可能增加调用费用")
        self.ack.toggled.connect(self._controls)
        self.ack.hide()
        root.addWidget(self.ack)
        self.resume_button = QPushButton("继续所选批次")
        self.resume_button.setProperty("primary", True)
        self.resume_button.clicked.connect(self._resume)
        root.addWidget(self.resume_button)
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.setOpenExternalLinks(False)
        self.details.setMinimumHeight(220)
        self.details.anchorClicked.connect(self._open_report)
        root.addWidget(self.details, 1)
        detail_pager = QHBoxLayout()
        self.detail_previous = QPushButton("上一页材料")
        self.detail_next = QPushButton("下一页材料")
        self.detail_previous.clicked.connect(lambda: self._page_detail(-1))
        self.detail_next.clicked.connect(lambda: self._page_detail(1))
        detail_pager.addWidget(self.detail_previous)
        detail_pager.addWidget(self.detail_next)
        root.addLayout(detail_pager)
        self.message = QLabel("打开页面或重启不会自动续跑。已完成报告可从材料列表打开。")
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._poll)
        self.client.action_completed.connect(self._completed)
        self.client.request_failed.connect(self._failed)
        self._controls()

    def activate(self) -> None:
        self._active = True
        self._timer.start()
        self.refresh()

    def deactivate(self) -> None:
        self._active = False
        self._timer.stop()

    def set_snapshot(self, _snapshot: object) -> None:
        pass

    def _poll(self) -> None:
        if self._active and not self._connection_failed and any(row.get("status") == "running" for row in self._rows):
            self.refresh()

    def _send(self, command: str, fields: dict[str, Any]) -> None:
        if command in self._pending:
            return
        self._pending[command] = fields
        self._controls()
        self.client.research_command(command, fields)

    def refresh(self) -> None:
        self._connection_failed = False
        self._send("run_list", {"offset": self._offset})
        self._read_detail()

    def _page(self, direction: int) -> None:
        if "run_list" in self._pending:
            return
        self._offset = max(0, self._offset - 20) if direction < 0 else self._next or self._offset
        self._send("run_list", {"offset": self._offset})

    def _select(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self._rows):
            return
        run = self._rows[row]
        if run["run_id"] != self._run_id:
            self._run_id = str(run["run_id"])
            self._detail_offset = 0
            self._detail_next = None
            self._detail_valid = False
            self._report_ids.clear()
            self.details.setPlainText("正在读取所选批次…")
            self.ack.setChecked(False)
        self._run = run
        self._show_summary()
        self._read_detail()

    def _read_detail(self) -> None:
        if self._run_id:
            self._send("run_detail", {"run_id": self._run_id, "offset": self._detail_offset})

    def _page_detail(self, direction: int) -> None:
        if "run_detail" in self._pending:
            return
        self._detail_offset = max(0, self._detail_offset - 20) if direction < 0 else self._detail_next or self._detail_offset
        self._read_detail()

    def _show_summary(self) -> None:
        run = self._run or {}
        reason = REASONS.get(str(run.get("incompatible_reason")), "可显式继续未完成材料。")
        self.summary.setText(
            f"已完成 {run.get('completed', 0)} · 待处理 {run.get('pending', 0)} · 失败 {run.get('failed', 0)} · "
            f"调用未确认 {run.get('uncertain', 0)}\n模型调用 {run.get('model_calls', 0)} / {run.get('max_ai_calls', 0)}；{reason}")
        self.ack.setVisible(bool(run.get("uncertain")))
        self._controls()

    def _resume(self) -> None:
        if not self.resume_button.isEnabled():
            return
        self._send("resume", {"run_id": self._run_id, "ack_uncertain": self.ack.isChecked()})
        self.ack.setChecked(False)

    def _completed(self, receipt: object) -> None:
        if not isinstance(receipt, ActionReceipt) or not receipt.action.startswith("research-"):
            return
        command = receipt.action.removeprefix("research-")
        fields = self._pending.get(command)
        if fields is None:
            return
        result = receipt.result if isinstance(receipt.result, dict) else {}
        # Another view or a delayed duplicate can emit the same action name.
        # Only consume the pending read that this receipt actually answers.
        if receipt.accepted:
            if command == "run_detail":
                received_run = result.get("run") or {}
                if (received_run.get("run_id") != fields.get("run_id")
                        or result.get("offset", 0) != fields.get("offset", 0)):
                    return
            elif command == "run_list" and result.get("offset", 0) != fields.get("offset", 0):
                return
            elif command == "resume" and result.get("run_id") != fields.get("run_id"):
                return
        elif result.get("run_id") is not None and result.get("run_id") != fields.get("run_id"):
            return
        self._pending.pop(command)
        if not receipt.accepted or not isinstance(receipt.result, dict):
            self.message.setText(REASONS.get(str(result.get("reason")), receipt.message or "读取失败，请刷新重试。"))
            if command in {"run_detail", "resume"}:
                self._detail_valid = False
                self.ack.setChecked(False)
            if command == "resume":
                self.refresh()
            self._controls()
            return
        if command == "run_list":
            self._rows = result.get("items") or []
            self._offset = int(result.get("offset", 0))
            self._next = result.get("next_offset")
            self.table.blockSignals(True)
            self.table.setRowCount(len(self._rows))
            selected = -1
            for index, row in enumerate(self._rows):
                values = (row.get("created_at", ""), STATES.get(str(row.get("status")), "状态未确认"),
                          f"{row.get('completed', 0)} / {row.get('total', 0)}",
                          f"{row.get('failed', 0)} / {row.get('uncertain', 0)}",
                          f"{row.get('model_calls', 0)} / {row.get('max_ai_calls', 0)}")
                for column, value in enumerate(values):
                    self.table.setItem(index, column, QTableWidgetItem(str(value)))
                if row["run_id"] == self._run_id:
                    selected = index
            if selected >= 0:
                self.table.selectRow(selected)
                # The detail read is authoritative for the selected batch. A
                # list request started earlier may arrive after a newer detail.
                if not self._detail_valid:
                    self._run = self._rows[selected]
                self._show_summary()
            else:
                self._run_id, self._run, self._detail_valid = "", None, False
                self._report_ids.clear()
                self.details.clear()
                self.summary.setText("选择一批任务查看材料进度。" if self._rows else "暂无任务记录；可从研究页运行一次扫描。")
                self.ack.setChecked(False)
                self.ack.hide()
            self.table.blockSignals(False)
            self.page_label.setText(f"第 {self._offset // 20 + 1} 页")
        elif command == "run_detail":
            run = result.get("run") or {}
            if run.get("run_id") == self._run_id and result.get("offset", 0) == self._detail_offset:
                self._run = run
                self._detail_valid = True
                self._detail_next = result.get("next_offset")
                self._report_ids.clear()
                parts = []
                for item in result.get("items") or []:
                    status = STATES.get(str(item.get("status")), "状态未确认")
                    detail = REASONS.get(str(item.get("detail")), str(item.get("detail") or ""))
                    parts.append(f"<p><b>{_text(item.get('title'))}</b><br>{status} · 模型调用 {_text(item.get('model_calls', 0))} · {_text(detail)}</p>")
                    for index, report_id in enumerate(item.get("report_ids") or []):
                        self._report_ids.add(report_id)
                        parts.append(f'<a href="report:{quote(report_id, safe="")}">阅读报告 {index + 1}</a>　')
                self.details.setHtml("".join(parts) or "<p>此阶段尚无材料条目。</p>")
                self._show_summary()
            elif self._run_id:
                self._read_detail()
        elif command == "resume":
            self.message.setText("已开始继续未完成材料；调用次数沿用本批次预算。可用研究页的停止按钮取消。")
            if self._run and result.get("run_id") == self._run_id:
                self._run.update(status="running", resumable=False, incompatible_reason="run_active")
                self._show_summary()
            self.ack.setChecked(False)
            self.refresh()
        self._controls()

    def _failed(self, purpose: str, _code: str) -> None:
        command = purpose.removeprefix("research-")
        if self._pending.pop(command, None) is not None:
            self._connection_failed = True
            self._detail_valid = False
            self.ack.setChecked(False)
            self.message.setText("连接中断，当前记录已保留。请刷新确认最新状态；不会自动重试续跑。")
            self._controls()

    def _controls(self, *_args: object) -> None:
        run = self._run or {}
        self.refresh_button.setEnabled("run_list" not in self._pending)
        self.previous_button.setEnabled(self._offset > 0 and "run_list" not in self._pending)
        self.next_button.setEnabled(self._next is not None and "run_list" not in self._pending)
        self.detail_previous.setEnabled(bool(self._run_id) and self._detail_offset > 0 and "run_detail" not in self._pending)
        self.detail_next.setEnabled(bool(self._run_id) and self._detail_next is not None and "run_detail" not in self._pending)
        self.resume_button.setEnabled(bool(run.get("resumable")) and self._detail_valid
                                      and not self._connection_failed
                                      and not any(key in self._pending for key in ("resume", "run_detail"))
                                      and (not run.get("uncertain") or self.ack.isChecked()))

    def _open_report(self, url: QUrl) -> None:
        if url.scheme() == "report":
            identity = unquote(url.toString().partition(":")[2])
            if identity in self._report_ids:
                self.report_requested.emit(identity)
