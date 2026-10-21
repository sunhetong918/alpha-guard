"""Local security lists and explicit, previewed rule editing."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .client import GuardianClient
from .models import ActionReceipt, DashboardSnapshot
from .widgets import PageHeading

RULE_LABELS = {
    "price_above": "价格 ≥", "price_below": "价格 ≤", "pe_above": "市盈率 ≥",
    "pe_below": "市盈率 ≤", "roe_above": "ROE ≥", "price_drop_pct": "相对成本跌幅 ≥",
}


def _unit(kind: str, market: str) -> str:
    if kind in {"price_above", "price_below"}:
        return "USD" if market == "US" else "HKD"
    return "倍" if kind in {"pe_above", "pe_below"} else "%"


def _table(labels: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(labels))
    table.setHorizontalHeaderLabels(labels)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


class RulesEditorDialog(QDialog):
    """Every existing rule is shown; preview binds the full edited configuration."""

    saved = Signal(object)

    def __init__(self, client: GuardianClient, item: dict[str, Any], revision: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self.market = str(item["market"])
        self.ticker = str(item["ticker"])
        self.revision = revision
        self._preview: dict[str, Any] | None = None
        self._pending: str | None = None
        self._loading = False
        self._too_many_rules = False
        self.setWindowTitle(f"监控规则 · {self.market}:{self.ticker}")
        self.resize(1080, 720)
        root = QVBoxLayout(self)
        note = QLabel("规则仅生成待人工核验的提醒。预览不会扫描；确认保存后，后续监控采用新配置。研究关注独立保存。")
        note.setWordWrap(True)
        root.addWidget(note)
        self.editor = QWidget()
        editor_layout = QVBoxLayout(self.editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.name_input = QLineEdit()
        self.name_input.setMaxLength(200)
        self.enabled = QCheckBox("启用此证券的规则监控")
        self.cost_input = QLineEdit()
        self.cost_input.setPlaceholderText("可留空；成本跌幅规则需要成本价")
        self.cooldown_input = QLineEdit("24.0")
        form.addRow("名称", self.name_input)
        form.addRow("监控", self.enabled)
        form.addRow(f"成本价（{_unit('price_above', self.market)}）", self.cost_input)
        form.addRow("提醒冷却（小时）", self.cooldown_input)
        editor_layout.addLayout(form)
        self.rules_table = _table(["核验方向", "条件", "阈值", "单位", "稳定规则 ID", "备注"])
        self.rules_table.setMinimumHeight(220)
        editor_layout.addWidget(self.rules_table)
        row_actions = QHBoxLayout()
        self.add_rule_button = QPushButton("增加条件")
        self.remove_rule_button = QPushButton("删除选中条件")
        self.add_rule_button.clicked.connect(lambda: self._add_rule("buy_rules", {}))
        self.remove_rule_button.clicked.connect(self._remove_rule)
        row_actions.addWidget(self.add_rule_button)
        row_actions.addWidget(self.remove_rule_button)
        row_actions.addStretch(1)
        editor_layout.addLayout(row_actions)
        root.addWidget(self.editor)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setPlaceholderText("修改完成后先预览，核对证券、方向、阈值和启用状态。")
        root.addWidget(self.preview_text, 1)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        controls = QHBoxLayout()
        self.preview_button = QPushButton("预览规则")
        self.save_button = QPushButton("确认保存规则")
        self.save_button.setProperty("primary", True)
        self.save_button.setEnabled(False)
        cancel = QPushButton("关闭")
        self.preview_button.clicked.connect(self._request_preview)
        self.save_button.clicked.connect(self._save)
        cancel.clicked.connect(self.reject)
        controls.addWidget(self.preview_button)
        controls.addWidget(self.save_button)
        controls.addStretch(1)
        controls.addWidget(cancel)
        root.addLayout(controls)
        for edit in (self.name_input, self.cost_input, self.cooldown_input):
            edit.textChanged.connect(self._changed)
        self.enabled.toggled.connect(self._changed)
        configuration = item.get("configuration") or {
            "name": item.get("name") or self.ticker, "market": self.market,
            "currency": "USD" if self.market == "US" else "HKD", "enabled": False,
            "cost_basis": None, "alert_cooldown_hours": 24.0, "buy_rules": [], "sell_rules": [],
        }
        self._populate(configuration)
        self.client.action_completed.connect(self._completed)
        self.client.request_failed.connect(self._failed)

    def _populate(self, configuration: dict[str, Any]) -> None:
        self._too_many_rules = (sum(len(configuration.get(side) or []) for side in ("buy_rules", "sell_rules")) > 200
                               or any(len(configuration.get(side) or []) > 100 for side in ("buy_rules", "sell_rules")))
        self._loading = True
        try:
            self.name_input.setText(str(configuration["name"]))
            self.enabled.setChecked(configuration.get("enabled") is True)
            cost = configuration.get("cost_basis")
            self.cost_input.setText("" if cost is None else str(cost))
            self.cooldown_input.setText(str(configuration.get("alert_cooldown_hours", 24.0)))
            self.rules_table.setRowCount(0)
            for side in ("buy_rules", "sell_rules"):
                for rule in configuration.get(side) or []:
                    self._add_rule(side, rule)
        finally:
            self._loading = False
        if self._too_many_rules:
            self.preview_button.setEnabled(False)
            self.editor.setEnabled(False)
            self.message.setText("此证券的规则超出向导编辑范围，请使用原配置文件；向导不会保存不完整规则。")

    def _add_rule(self, side: str, values: dict[str, Any]) -> None:
        row = self.rules_table.rowCount()
        if row >= 200:
            self.message.setText("最多编辑 200 条规则。")
            return
        self.rules_table.insertRow(row)
        direction = QComboBox()
        direction.addItem("买入核验", "buy_rules")
        direction.addItem("卖出核验", "sell_rules")
        direction.setCurrentIndex(direction.findData(side))
        kind = QComboBox()
        for value, label in RULE_LABELS.items():
            kind.addItem(label, value)
        kind.setCurrentIndex(kind.findData(values.get("type", "price_below")))
        threshold = QLineEdit(str(values.get("value", "")))
        threshold.setPlaceholderText("填写阈值")
        unit = QLabel(_unit(str(kind.currentData()), self.market))
        identity = QLineEdit(str(values.get("id", "")))
        identity.setReadOnly(True)
        identity.setPlaceholderText("预览时分配")
        note = QLineEdit(str(values.get("note", "")))
        note.setMaxLength(300)
        for column, widget in enumerate((direction, kind, threshold, unit, identity, note)):
            self.rules_table.setCellWidget(row, column, widget)
        direction.currentIndexChanged.connect(self._changed)
        kind.currentIndexChanged.connect(lambda: unit.setText(_unit(str(kind.currentData()), self.market)))
        kind.currentIndexChanged.connect(self._changed)
        threshold.textChanged.connect(self._changed)
        note.textChanged.connect(self._changed)
        self._changed()

    def _remove_rule(self) -> None:
        row = self.rules_table.currentRow()
        if row >= 0:
            self.rules_table.removeRow(row)
            self._changed()

    def _changed(self, *_args: object) -> None:
        if not self._loading:
            self._preview = None
            self.save_button.setEnabled(False)
            self.preview_text.clear()

    def _configuration(self) -> dict[str, Any]:
        cost = self.cost_input.text().strip()
        result: dict[str, Any] = {
            "name": self.name_input.text().strip(), "market": self.market,
            "currency": "USD" if self.market == "US" else "HKD", "enabled": self.enabled.isChecked(),
            "cost_basis": float(cost) if cost else None,
            "alert_cooldown_hours": float(self.cooldown_input.text()), "buy_rules": [], "sell_rules": [],
        }
        for row in range(self.rules_table.rowCount()):
            direction = self.rules_table.cellWidget(row, 0)
            kind = self.rules_table.cellWidget(row, 1)
            threshold = self.rules_table.cellWidget(row, 2)
            identity = self.rules_table.cellWidget(row, 4)
            note = self.rules_table.cellWidget(row, 5)
            assert isinstance(direction, QComboBox) and isinstance(kind, QComboBox)
            assert isinstance(threshold, QLineEdit) and isinstance(identity, QLineEdit) and isinstance(note, QLineEdit)
            rule = {"type": kind.currentData(), "value": float(threshold.text()), "note": note.text()}
            if identity.text():
                rule["id"] = identity.text()
            result[str(direction.currentData())].append(rule)
        return result

    def _busy(self, command: str | None) -> None:
        self._pending = command
        self.editor.setEnabled(command is None)
        self.preview_button.setEnabled(command is None and not self._too_many_rules)
        self.save_button.setEnabled(command is None and self._preview is not None)

    def _request_preview(self) -> None:
        if self._pending is not None or self._too_many_rules:
            return
        try:
            configuration = self._configuration()
        except ValueError:
            self.message.setText("阈值、成本和冷却时间需要填写有效数字。")
            return
        self._preview = None
        self._busy("rules_preview")
        self.message.setText("正在校验本地规则，尚未保存…")
        self.client.research_command("rules_preview", {"market": self.market, "ticker": self.ticker,
            "configuration": configuration, "expected_revision": self.revision})

    def _save(self) -> None:
        if self._pending is not None or self._preview is None:
            return
        self._busy("rules_save")
        self.message.setText("正在备份并保存规则…")
        self.client.research_command("rules_save", {"market": self.market, "ticker": self.ticker,
            "configuration": deepcopy(self._preview["configuration"]), "expected_revision": self.revision,
            "preview_token": self._preview["preview_token"]})

    def _completed(self, receipt: object) -> None:
        if not isinstance(receipt, ActionReceipt) or receipt.action != f"research-{self._pending}":
            return
        command = self._pending
        self._busy(None)
        if not receipt.accepted or not isinstance(receipt.result, dict):
            self._preview = None
            self.save_button.setEnabled(False)
            self.message.setText(receipt.message or "请求未完成，请关闭并刷新配置后重试。")
            return
        if command == "rules_save":
            self.saved.emit(receipt.result)
            self.accept()
            return
        result = receipt.result
        if not isinstance(result.get("configuration"), dict) or not result.get("preview_token"):
            self.message.setText("预览回执不完整，请重试。")
            return
        self._populate(result["configuration"])
        self._preview = result
        configuration = result["configuration"]
        lines = [f"证券：{result.get('key', self.market + ':' + self.ticker)} · {configuration['name']}",
                 f"监控：{'启用' if configuration['enabled'] else '关闭'}；研究关注保持不变",
                 f"成本：{configuration.get('cost_basis') if configuration.get('cost_basis') is not None else '未设置'} {configuration['currency']}",
                 f"提醒冷却：{configuration['alert_cooldown_hours']} 小时"]
        for side, label in (("buy_rules", "买入核验"), ("sell_rules", "卖出核验")):
            for rule in configuration[side]:
                lines.append(f"{label}：{RULE_LABELS[rule['type']]} {rule['value']} {_unit(rule['type'], self.market)} · {rule['id']}")
        lines.append("确认保存会先备份原规则文件；不会立即扫描或下单。")
        self.preview_text.setPlainText("\n".join(lines))
        self.message.setText("预览校验通过，请核对后确认保存。修改任何内容都需要重新预览。")
        self.save_button.setEnabled(True)

    def _failed(self, purpose: str, _code: str) -> None:
        if purpose != f"research-{self._pending}":
            return
        self._busy(None)
        self._preview = None
        self.save_button.setEnabled(False)
        self.message.setText("规则操作失败；请检查阈值、成本和规则 ID，配置有变化时需关闭并重新加载。")


class WatchlistPage(QWidget):
    overview_requested = Signal(str)
    view_research_requested = Signal(str)
    view_events_requested = Signal(str)

    def __init__(self, client: GuardianClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self._items: list[dict[str, Any]] = []
        self._revision = ""
        self._selected_key: str | None = None
        self._pending: str | None = None
        self._rule_dialog: RulesEditorDialog | None = None
        self._open_rules_key: str | None = None
        self._offset = 0
        self._next_offset: int | None = None
        self._form_dirty = False
        self._draft_revision: str | None = None
        self._loading_form = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._refresh)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.addWidget(PageHeading("WATCHLIST / 自选", "自选与规则", "先添加证券，再分别选择研究关注与规则监控。读取和保存不会联网或开始扫描。"))
        controls = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索全部本地自选：代码、名称或市场")
        self.search_input.textChanged.connect(lambda: self._search_timer.start())
        self.refresh_button = QPushButton("重新加载")
        self.refresh_button.clicked.connect(self._refresh)
        self.add_button = QPushButton("新增证券")
        self.add_button.clicked.connect(self._new)
        controls.addWidget(self.search_input, 1)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.add_button)
        root.addLayout(controls)
        self.table = _table(["证券", "名称", "研究关注", "规则监控", "条件数"])
        self.table.itemSelectionChanged.connect(self._select)
        self.table.itemDoubleClicked.connect(lambda _item: self._view_overview())
        root.addWidget(self.table, 1)
        pager = QHBoxLayout()
        self.page_label = QLabel("尚未读取自选")
        self.previous_button = QPushButton("上一页")
        self.next_button = QPushButton("下一页")
        self.previous_button.clicked.connect(lambda: self._request_page(max(0, self._offset - 20)))
        self.next_button.clicked.connect(lambda: self._request_page(self._next_offset) if self._next_offset is not None else None)
        pager.addWidget(self.page_label, 1)
        pager.addWidget(self.previous_button)
        pager.addWidget(self.next_button)
        root.addLayout(pager)
        self.form = QWidget()
        form = QFormLayout(self.form)
        self.market_input = QComboBox()
        for market, name in (("US", "美股 US"), ("HK", "港股 HK"), ("CN", "A 股 CN（日线研究）")):
            self.market_input.addItem(name, market)
        self.market_input.currentIndexChanged.connect(self._update_controls)
        self.ticker_input = QLineEdit()
        self.ticker_input.setMaxLength(64)
        self.ticker_input.setPlaceholderText("直接输入代码，例如 AAPL、00700、600519")
        self.name_input = QLineEdit()
        self.name_input.setMaxLength(200)
        self.research_enabled = QCheckBox("加入研究关注（不会自动开启研究）")
        self.research_enabled.setChecked(True)
        self.ticker_input.textChanged.connect(self._mark_draft)
        self.name_input.textChanged.connect(self._mark_draft)
        self.research_enabled.toggled.connect(self._mark_draft)
        form.addRow("市场", self.market_input)
        form.addRow("证券代码", self.ticker_input)
        form.addRow("名称", self.name_input)
        form.addRow("研究", self.research_enabled)
        root.addWidget(self.form)
        actions = QHBoxLayout()
        self.save_button = QPushButton("保存研究关注")
        self.save_button.setProperty("primary", True)
        self.rules_button = QPushButton("编辑监控规则")
        self.overview_button = QPushButton("证券概览")
        self.overview_button.clicked.connect(self._view_overview)
        self.research_button = QPushButton("查看相关研究")
        self.events_button = QPushButton("查看事件")
        self.save_button.clicked.connect(self._save_research)
        self.rules_button.clicked.connect(self._edit_rules)
        self.research_button.clicked.connect(self._view_research)
        self.events_button.clicked.connect(lambda: self.view_events_requested.emit(self._selected_key) if self._selected_key else None)
        for button in (self.save_button, self.rules_button, self.overview_button,
                       self.research_button, self.events_button):
            actions.addWidget(button)
        actions.addStretch(1)
        root.addLayout(actions)
        self.message = QLabel("通过代码添加证券；当前搜索仅查找本地自选。A 股仅支持研究。")
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        self.client.action_completed.connect(self._completed)
        self.client.request_failed.connect(self._failed)
        self._update_controls()

    def activate(self) -> None:
        self._request_page(self._offset)

    def deactivate(self) -> None:
        self._search_timer.stop()

    def set_snapshot(self, _snapshot: DashboardSnapshot) -> None:
        pass  # Watchlist data comes only from its authenticated local command.

    def _refresh(self) -> None:
        self._request_page(0)

    def _request_page(self, offset: int) -> None:
        fields: dict[str, Any] = {}
        if offset:
            fields["offset"] = offset
        query = self.search_input.text().strip()
        if query:
            fields["query"] = query
        self._send("watchlist_get", fields)

    def _mark_draft(self, *_args: object) -> None:
        if not self._loading_form:
            if not self._form_dirty:
                self._draft_revision = self._revision
            self._form_dirty = True

    def _send(self, command: str, fields: dict[str, Any]) -> None:
        if self._pending is not None:
            return
        self._pending = command
        self._update_controls()
        self.message.setText("正在读取或保存本地配置…")
        self.client.research_command(command, fields)

    def _set_data(self, result: object) -> None:
        if not isinstance(result, dict) or not isinstance(result.get("items"), list) or not isinstance(result.get("revision"), str):
            self.message.setText("配置回执不完整，请重新加载。")
            return
        self._items = result["items"]
        self._revision = result["revision"]
        self._offset = int(result.get("offset", 0))
        self._next_offset = result.get("next_offset")
        saved_key = result.get("saved_key")
        if isinstance(saved_key, str):
            self._selected_key = saved_key
        self.page_label.setText(f"共 {result.get('total', len(self._items))} 项 · 第 {self._offset // 20 + 1} 页")
        self._render()
        # Background list/page responses must not replace an unfinished form.
        if not self._form_dirty and self._selected_key is not None:
            row = next((index for index, item in enumerate(self._items)
                        if item["key"] == self._selected_key), None)
            if row is not None:
                self.table.selectRow(row)
                self._select()
        self._update_controls()
        if isinstance(saved_key, str) and not any(item["key"] == saved_key for item in self._items):
            self.search_input.blockSignals(True)
            self.search_input.setText(saved_key)
            self.search_input.blockSignals(False)
            QTimer.singleShot(0, self._refresh)

    def _render(self, *_args: object) -> None:
        visible = self._items
        self.table.blockSignals(True)
        self.table.setRowCount(len(visible))
        self.table.clearSelection()
        for row, item in enumerate(visible):
            values = [item["key"], item["name"], "已关注" if item["research_enabled"] else "未关注",
                      "已启用" if item["monitoring_enabled"] else "关闭", item["rule_count"]]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.ItemDataRole.UserRole, item["key"])
                self.table.setItem(row, column, cell)
            if item["key"] == self._selected_key:
                self.table.selectRow(row)
        self.table.blockSignals(False)

    def _select(self) -> None:
        if not self.table.selectedItems():
            return
        cell = self.table.item(self.table.currentRow(), 0)
        if cell is None:
            return
        key = str(cell.data(Qt.ItemDataRole.UserRole))
        item = next((item for item in self._items if item["key"] == key), None)
        if item is None:
            return
        self._selected_key = key
        self._loading_form = True
        self.market_input.setCurrentIndex(self.market_input.findData(item["market"]))
        self.market_input.setEnabled(False)
        self.ticker_input.setText(item["ticker"])
        self.ticker_input.setReadOnly(True)
        self.name_input.setText(item["name"])
        self.research_enabled.setChecked(item["research_enabled"])
        self._loading_form = False
        self._form_dirty = False
        self._draft_revision = None
        self._update_controls()

    def _new(self) -> None:
        self._loading_form = True
        self._selected_key = None
        self.table.clearSelection()
        self.market_input.setEnabled(True)
        self.ticker_input.setReadOnly(False)
        self.ticker_input.clear()
        self.name_input.clear()
        self.research_enabled.setChecked(True)
        self._loading_form = False
        self._form_dirty = False
        self._draft_revision = None
        self._update_controls()

    def _save_research(self) -> None:
        self._send("watchlist_save", {"market": self.market_input.currentData(),
            "ticker": self.ticker_input.text().strip(), "name": self.name_input.text().strip(),
            "research_enabled": self.research_enabled.isChecked(),
            "expected_revision": self._draft_revision if self._draft_revision is not None else self._revision})

    def _edit_rules(self) -> None:
        if self._rule_dialog is not None:
            self._rule_dialog.raise_()
            self._rule_dialog.activateWindow()
            return
        if self._selected_key is not None:
            self._open_rules_key = self._selected_key
            self._send("watchlist_get", {"key": self._selected_key})
            return
        item = {"market": self.market_input.currentData(), "ticker": self.ticker_input.text().strip(),
                "name": self.name_input.text().strip(), "configuration": None}
        self._open_rule_editor(item, self._revision)

    def _open_rule_editor(self, item: dict[str, Any], revision: str) -> None:
        if item.get("rules_editable") is False:
            self.message.setText(str(item.get("rules_edit_message", "此证券规则数超出向导上限，请使用原配置文件。")))
            return
        if item["market"] == "CN" or not item["ticker"]:
            self.message.setText("请先填写美股或港股代码；A 股当前仅支持研究。")
            return
        dialog = RulesEditorDialog(self.client, item, revision, self)
        self._rule_dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.saved.connect(self._set_data)
        dialog.finished.connect(lambda: setattr(self, "_rule_dialog", None))
        dialog.open()

    def _view_research(self) -> None:
        if self._selected_key is not None:
            self.view_research_requested.emit(self._selected_key)

    def _view_overview(self) -> None:
        if self._selected_key and self._pending is None:
            self.overview_requested.emit(self._selected_key)

    def _update_controls(self, *_args: object) -> None:
        ready = bool(self._revision) and self._pending is None
        self.form.setEnabled(self._pending is None)
        self.save_button.setEnabled(ready)
        self.rules_button.setEnabled(ready and self.market_input.currentData() != "CN")
        self.research_button.setEnabled(ready and self._selected_key is not None)
        self.events_button.setEnabled(ready and self._selected_key is not None)
        self.overview_button.setEnabled(ready and self._selected_key is not None)
        self.refresh_button.setEnabled(self._pending is None)
        self.add_button.setEnabled(self._pending is None)
        self.previous_button.setEnabled(self._pending is None and self._offset > 0)
        self.next_button.setEnabled(self._pending is None and self._next_offset is not None)

    def _completed(self, receipt: object) -> None:
        if not isinstance(receipt, ActionReceipt) or receipt.action != f"research-{self._pending}":
            return
        command = self._pending
        self._pending = None
        if receipt.accepted:
            result = receipt.result
            if command == "watchlist_get" and self._open_rules_key is not None:
                expected_key, self._open_rules_key = self._open_rules_key, None
                if isinstance(result, dict) and isinstance(result.get("item"), dict) and result["item"].get("key") == expected_key:
                    self._open_rule_editor(result["item"], str(result["revision"]))
                else:
                    self.message.setText("规则详情读取失败，请重试。")
                self._update_controls()
                return
            if command == "watchlist_save":
                self._form_dirty = False
                self._draft_revision = None
            self._set_data(result)
            self.message.setText("研究关注已保存；规则监控保持不变。" if command == "watchlist_save"
                                 else "本地配置已加载。研究关注与规则监控分别设置。")
            if isinstance(result, dict) and result.get("query", "") != self.search_input.text().strip():
                QTimer.singleShot(0, self._refresh)
        else:
            self._open_rules_key = None
            self.message.setText(receipt.message)
        self._update_controls()

    def _failed(self, purpose: str, _code: str) -> None:
        if purpose != f"research-{self._pending}":
            return
        self._pending = None
        self._open_rules_key = None
        self.message.setText("配置操作未完成，请检查市场、代码或任务状态后重新加载。")
        self._update_controls()


__all__ = ["WatchlistPage", "RulesEditorDialog"]
