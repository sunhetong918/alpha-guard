"""Preview a complete local holding snapshot before any portfolio write."""
from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .client import GuardianClient
from .models import ActionReceipt
from .widgets import PageHeading

FIELDS = {"market": "市场", "ticker": "证券代码", "quantity": "数量",
          "unit_cost": "单位成本（可选）", "cost_currency": "成本币种（可选）"}
ERRORS = {"column_count": "列数与表头不一致", "instrument": "市场或证券代码无效",
          "duplicate_instrument": "同一证券重复出现", "quantity": "数量须为非负数，最多八位小数",
          "unit_cost_or_currency": "单位成本与受支持币种须成对填写"}
CURRENCIES = ("USD", "HKD", "CNY", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "SGD")
STATUSES = {"estimated": "已估值", "quote_missing": "缺价", "quote_invalid": "报价无效",
            "quote_currency_invalid": "报价币种无效", "stale": "报价过期", "time_unknown": "报价时间未知",
            "time_in_future": "报价时间异常", "fx_missing": "缺汇率", "fx_stale": "汇率过期",
            "quantity_invalid": "数量无效", "budget_exhausted": "超出取价预算"}
MISSING_HINTS = {"缺价": "点击“联网取价（显式）”后仍失败的证券保持不可估。",
                 "缺汇率": "基准币种所需汇率未取得；该持仓不折算、不计零。"}
MAX_BYTES = 256_000


class PortfolioPage(QWidget):
    def __init__(self, client: GuardianClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self._pending: dict[str, dict[str, Any]] = {}
        self._preview: dict[str, Any] | None = None
        self._current: dict[str, Any] | None = None
        self._concentration: dict[str, Any] | None = None
        self._unknown: list[str] = []
        self._source_label = "手工输入.csv"
        root = QVBoxLayout(self)
        root.addWidget(PageHeading("LOCAL POSITIONS", "只读持仓", "本地完整快照；导入不连接券商、不取价，也不修改监控规则。"))
        form = QFormLayout()
        self.account = QLineEdit("我的持仓")
        self.snapshot_date = QLineEdit(date.today().isoformat())
        form.addRow("账户别名", self.account)
        form.addRow("快照日期", self.snapshot_date)
        root.addLayout(form)
        actions = QHBoxLayout()
        self.read_button = QPushButton("读取该账户快照")
        self.file_button = QPushButton("选择 CSV 文件")
        self.example_button = QPushButton("填入示例 CSV")
        self.read_button.clicked.connect(self._read)
        self.file_button.clicked.connect(self._choose_file)
        self.example_button.clicked.connect(lambda: self.csv_text.setPlainText(
            "market,ticker,quantity,unit_cost,cost_currency\nUS,AAPL,10,100,USD\nHK,00700,100,,\n"))
        for button in (self.read_button, self.file_button, self.example_button):
            actions.addWidget(button)
        root.addLayout(actions)
        self.csv_text: QPlainTextEdit = QPlainTextEdit()
        self.csv_text.setPlaceholderText("也可以在这里手工粘贴 CSV（最多 500 行 / 256 KB）。成本是每股单位成本，不是当前市价。")
        self.csv_text.setMaximumHeight(110)
        root.addWidget(self.csv_text)
        mapping = QHBoxLayout()
        self.mapping: dict[str, QComboBox] = {}
        for key, title in FIELDS.items():
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            selector = QComboBox()
            selector.addItem("未映射", "")
            selector.currentIndexChanged.connect(self._invalidate)
            self.mapping[key] = selector
            column.addWidget(selector)
            mapping.addLayout(column)
        root.addLayout(mapping)
        self.full_snapshot = QCheckBox("这是该账户的完整持仓；未列出的证券将移除，数量为 0 也会移除")
        self.confirm_unknown = QCheckBox("我已逐项核对下方未识别证券的市场与代码")
        root.addWidget(self.full_snapshot)
        root.addWidget(self.confirm_unknown)
        self.preview_button = QPushButton("校验并预览变更")
        self.commit_button = QPushButton("提交完整快照")
        self.commit_button.setProperty("primary", True)
        self.undo_button = QPushButton("撤回最近一次快照")
        self.preview_button.clicked.connect(self._request_preview)
        self.commit_button.clicked.connect(self._commit)
        self.undo_button.clicked.connect(self._undo)
        controls = QHBoxLayout()
        for button in (self.preview_button, self.commit_button, self.undo_button):
            controls.addWidget(button)
        root.addLayout(controls)
        self.summary = QLabel("尚未读取。首次导入也需要预览和提交。")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.summary)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["证券", "数量", "单位成本", "成本币种", "变更"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(160)
        root.addWidget(self.table)
        self.message = QLabel("")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        root.addWidget(PageHeading("CONCENTRATION", "只读集中度", "在已保存快照上计算单股与币种占比；读取和计算不联网，取价/取汇率是显式动作且有调用预算。缺价或缺汇率的持仓列为不可估，不用 0 填充，也不冒充完整组合占比。"))
        options = QHBoxLayout()
        self.base_currency = QComboBox()
        self.base_currency.addItem("不统一基准", "")
        for code in CURRENCIES:
            self.base_currency.addItem(code, code)
        self.single_threshold = QDoubleSpinBox()
        self.single_threshold.setRange(0.0, 99.0)
        self.single_threshold.setDecimals(1)
        self.single_threshold.setSpecialValueText("不启用")
        self.currency_threshold = QDoubleSpinBox()
        self.currency_threshold.setRange(0.0, 99.0)
        self.currency_threshold.setDecimals(1)
        self.currency_threshold.setSpecialValueText("不启用")
        self.top_n = QSpinBox()
        self.top_n.setRange(1, 20)
        for label, widget in (("基准币种", self.base_currency), ("单股阈值 %", self.single_threshold),
                              ("币种阈值 %", self.currency_threshold), ("前 N 大", self.top_n)):
            column = QVBoxLayout()
            column.addWidget(QLabel(label))
            column.addWidget(widget)
            options.addLayout(column)
        self.save_options = QPushButton("保存集中度设置")
        self.price_button = QPushButton("联网取价（显式）")
        self.price_button.setProperty("danger", True)
        self.load_concentration = QPushButton("读取集中度")
        self.save_options.clicked.connect(self._save_options)
        self.price_button.clicked.connect(self._price)
        self.load_concentration.clicked.connect(self._load_concentration)
        for button in (self.save_options, self.price_button, self.load_concentration):
            options.addWidget(button)
        options.addStretch(1)
        root.addLayout(options)
        self.concentration_summary = QLabel("尚未计算；需先有快照并显式取价。")
        self.concentration_summary.setWordWrap(True)
        self.concentration_summary.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.concentration_summary)
        self.concentration_table = QTableWidget(0, 8)
        self.concentration_table.setHorizontalHeaderLabels(
            ["证券", "数量", "价格", "币种", "市值(原币)", "基准市值", "占比 %", "状态"])
        self.concentration_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.concentration_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.concentration_table.setMinimumHeight(140)
        root.addWidget(self.concentration_table)
        self.currency_table = QTableWidget(0, 3)
        self.currency_table.setHorizontalHeaderLabels(["币种", "市值", "占比 %"])
        self.currency_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.currency_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.currency_table.setMaximumHeight(120)
        root.addWidget(self.currency_table)
        self.concentration_message = QLabel("")
        self.concentration_message.setTextFormat(Qt.TextFormat.PlainText)
        self.concentration_message.setWordWrap(True)
        root.addWidget(self.concentration_message)
        self.csv_text.textChanged.connect(self._headers_changed)
        self.account.textChanged.connect(self._account_changed)
        self.snapshot_date.textChanged.connect(self._invalidate)
        self.full_snapshot.toggled.connect(self._invalidate)
        self.confirm_unknown.toggled.connect(self._invalidate)
        self.client.action_completed.connect(self._completed)
        self.client.request_failed.connect(self._failed)
        self._controls()

    def activate(self) -> None:
        if self._current is None and not self.csv_text.toPlainText():
            self._read()

    def deactivate(self) -> None:
        pass

    def set_snapshot(self, _snapshot: object) -> None:
        pass

    def _account_changed(self, *_args: object) -> None:
        self._current = None
        self._unknown = []
        self.confirm_unknown.setChecked(False)
        self._invalidate()

    def _invalidate(self, *_args: object) -> None:
        self._preview = None
        self._controls()

    def _headers_changed(self) -> None:
        self._unknown = []
        self.confirm_unknown.setChecked(False)
        try:
            headers = next(csv.reader(io.StringIO(self.csv_text.toPlainText().lstrip("\ufeff"))), [])
        except csv.Error:
            headers = []
        headers = headers[:40]
        for key, selector in self.mapping.items():
            previous = selector.currentData()
            selector.blockSignals(True)
            selector.clear()
            selector.addItem("未映射", "")
            for name in headers:
                selector.addItem(name[:100], name)
            preferred = previous if previous in headers else key if key in headers else ""
            selector.setCurrentIndex(max(0, selector.findData(preferred)))
            selector.blockSignals(False)
        self._invalidate()

    def _choose_file(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "导入完整持仓", "", "CSV (*.csv)")
        if not name:
            return
        try:
            path = Path(name)
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("too large")
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError, ValueError):
            self.message.setText("文件须为 UTF-8 CSV，大小不超过 256 KB。")
            return
        self._source_label = path.name
        self.csv_text.setPlainText(content)

    def _draft(self) -> dict[str, Any]:
        return {"account": self.account.text().strip(), "snapshot_date": self.snapshot_date.text().strip(),
                "source_label": self._source_label, "csv_text": self.csv_text.toPlainText(),
                "mapping": {key: selector.currentData() for key, selector in self.mapping.items()},
                "full_snapshot": self.full_snapshot.isChecked(),
                "confirmed_instruments": self._unknown if self.confirm_unknown.isChecked() else []}

    def _send(self, command: str, fields: dict[str, Any]) -> None:
        if command in self._pending:
            return
        self._pending[command] = fields
        self._controls()
        self.client.research_command(command, fields)

    def _read(self) -> None:
        self._send("portfolio_get", {"account": self.account.text().strip()})

    def _request_preview(self) -> None:
        draft = self._draft()
        if not draft["full_snapshot"]:
            self.message.setText("请先确认这是完整持仓快照；局部文件不能作为增量补丁导入。")
            return
        if len(draft["csv_text"].encode("utf-8")) > MAX_BYTES:
            self.message.setText("CSV 不能超过 256 KB。")
            return
        self._send("portfolio_preview", draft)

    def _commit(self) -> None:
        if self._preview is not None and self.commit_button.isEnabled():
            self._send("portfolio_commit", {**self._draft(), "expected_revision": self._preview["revision"],
                                            "preview_token": self._preview["preview_token"]})

    def _undo(self) -> None:
        if self._current is not None and self.undo_button.isEnabled():
            self._send("portfolio_undo", {"account": self.account.text().strip(), "expected_revision": self._current["revision"]})

    def _options(self) -> dict[str, Any]:
        single = None if self.single_threshold.value() <= 0 else self.single_threshold.value()
        currency = None if self.currency_threshold.value() <= 0 else self.currency_threshold.value()
        return {"base_currency": self.base_currency.currentData() or None,
                "single_threshold": single, "currency_threshold": currency,
                "top_n": self.top_n.value()}

    def _save_options(self) -> None:
        self._send("concentration_settings", {"settings": self._options()})

    def _price(self) -> None:
        self._send("concentration_price", {"account": self.account.text().strip()})

    def _load_concentration(self) -> None:
        self._send("concentration_get", {"account": self.account.text().strip()})

    def _render_concentration(self, result: dict[str, Any]) -> None:
        self._concentration = result
        estimated, total = int(result.get("estimated_count") or 0), int(result.get("position_count") or 0)
        base = result.get("base_currency")
        scope = f"基准 {base}" if base else "各报价币种原值"
        self.concentration_summary.setText(
            f"快照 {result.get('snapshot_date') or '暂无'} · 已估值 {estimated}/{total} 项 · 分母为已估值部分（{scope}）"
            f" · 合计 {result.get('estimated_total') or '0'} · 报价/汇率有效期 {result.get('quote_max_age_days')} 天")
        rows = list(result.get("positions") or [])
        self.concentration_table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            values = (item.get("instrument"), item.get("quantity"), item.get("price") or "—",
                      item.get("currency") or "—", item.get("value_native") or "—",
                      item.get("value_base") or "—", item.get("weight") or "—",
                      STATUSES.get(str(item.get("status")), str(item.get("status"))))
            for column, text in enumerate(values):
                self.concentration_table.setItem(row, column, QTableWidgetItem(str(text)))
        currencies = list(result.get("currencies") or [])
        self.currency_table.setRowCount(len(currencies))
        for row, item in enumerate(currencies):
            for column, text in enumerate((item.get("currency"), item.get("value"), item.get("weight"))):
                self.currency_table.setItem(row, column, QTableWidgetItem(str(text)))
        top = "、".join(f"{item.get('instrument')} {item.get('weight')}%" for item in result.get("top") or [])
        breaches = list(result.get("breaches") or [])
        lines = [f"前 N 大：{top or '无'}", result.get("currencies_note") or ""]
        lines += [f"超阈值：{item.get('instrument')} {item.get('weight')}% > {item.get('threshold')}%"
                  for item in breaches]
        missing = list(result.get("missing") or [])
        lines += [f"不可估：{item.get('instrument')}（{STATUSES.get(str(item.get('reason')), str(item.get('reason')))}）"
                  for item in missing]
        for item in missing:
            hint = MISSING_HINTS.get(STATUSES.get(str(item.get("reason")), ""))
            if hint:
                lines.append(hint)
                break
        self.concentration_message.setText("\n".join(line for line in lines if line))

    def _show_rows(self, rows: list[dict[str, Any]], changes: list[dict[str, Any]] | None = None) -> None:
        by_key = {v["instrument"]: v for v in rows}
        change_map = {v["instrument"]: v["change"] for v in changes or []}
        for change in changes or []:
            if change["change"] == "removed":
                by_key[change["instrument"]] = change["before"]
        self.table.setRowCount(len(by_key))
        for row, (key, value) in enumerate(sorted(by_key.items())):
            values = (key, value["quantity"], value.get("unit_cost") or "未提供", value.get("cost_currency") or "未提供",
                      {"added": "新增", "changed": "变化", "removed": "移除"}.get(change_map.get(key, ""), "不变" if changes is not None else "已保存"))
            for column, text in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(text)))

    def _completed(self, receipt: object) -> None:
        if not isinstance(receipt, ActionReceipt):
            return
        command = receipt.action.removeprefix("research-")
        request = self._pending.pop(command, None)
        if request is None:
            return
        result = receipt.result or {}
        if command.startswith("concentration_"):
            if not receipt.accepted:
                self.concentration_message.setText(receipt.message or "集中度操作未完成。")
            elif command == "concentration_settings":
                self.concentration_message.setText("集中度设置已保存。")
            elif command == "concentration_price":
                receipt_data = result.get("receipt") or {}
                failed = len(receipt_data.get("failed") or [])
                self.concentration_message.setText(
                    f"取价完成：{receipt_data.get('stored_quotes')} 项报价、{receipt_data.get('stored_fx')} 项汇率；"
                    f"{failed} 项失败保持不可估。读取与计算不联网。")
                self._render_concentration(result.get("concentration") or {})
            else:
                self._render_concentration(result)
            self._controls()
            return
        if request["account"] != self.account.text().strip():
            self._controls()
            return
        if not receipt.accepted:
            self._preview = None
            self.message.setText(receipt.message or "操作未完成；保留输入，请重新读取并预览。")
        elif command == "portfolio_preview":
            if request != self._draft():
                self.message.setText("预览期间输入已改变，请重新预览。")
                self._controls()
                return
            self._preview = result
            self._unknown = [value["instrument"] for value in result.get("unconfirmed") or []]
            # Keep an explicit confirmation bound to the reviewed security set.
            if request.get("confirmed_instruments"):
                self._unknown = request["confirmed_instruments"]
            self._show_rows(result.get("positions") or [], result.get("changes") or [])
            self.summary.setText(f"预览：{len(result.get('positions') or [])} 项持仓；{len(result.get('changes') or [])} 项变更。单位成本不作为市价。")
            messages = [f"第 {v['line']} 行：{ERRORS.get(v['code'], '无效数据')}" for v in result.get("errors") or []]
            messages += [f"第 {v['line']} 行：未识别证券 {v['instrument']}，请核对并勾选确认后重新预览。" for v in result.get("unconfirmed") or []]
            self.message.setText("\n".join(messages) if messages else "与当前快照重复，无需提交。" if result.get("duplicate") else "预览通过；点击提交后才写入完整快照。")
        else:
            self._current = result
            self._preview = None
            self._show_rows(result.get("positions") or [])
            self.summary.setText(f"{result.get('account')} · 快照日期 {result.get('snapshot_date') or '暂无'} · {len(result.get('positions') or [])} 项持仓")
            self.message.setText("已撤回到前一个快照。" if command == "portfolio_undo" else "快照已保存。" if command == "portfolio_commit" else "已读取本地快照。")
        self._controls()

    def _failed(self, purpose: str, _code: str) -> None:
        command = purpose.removeprefix("research-")
        if self._pending.pop(command, None) is None:
            return
        if command.startswith("concentration_"):
            self.concentration_message.setText("集中度操作失败；已保存的报价与设置保持不变。")
        else:
            self._preview = None
            self.message.setText("读取或保存失败，输入已保留；请重新读取确认结果后再预览。")
        self._controls()

    def _controls(self) -> None:
        writing = any(key in self._pending for key in ("portfolio_commit", "portfolio_undo"))
        busy = any(key in self._pending for key in self._pending)
        self.commit_button.setEnabled(bool(self._preview and self._preview.get("can_commit")) and not self._pending)
        self.undo_button.setEnabled(bool(self._current and self._current.get("can_undo")) and not self._pending)
        self.preview_button.setEnabled(not writing and "portfolio_preview" not in self._pending)
        self.read_button.setEnabled(not writing and "portfolio_get" not in self._pending)
        self.save_options.setEnabled("concentration_settings" not in self._pending)
        self.price_button.setEnabled(not self._pending)
        self.load_concentration.setEnabled(not self._pending and not busy)
        for widget in (self.account, self.snapshot_date, self.csv_text, self.file_button, self.example_button,
                       self.full_snapshot, self.confirm_unknown, *self.mapping.values()):
            widget.setEnabled(not writing)
