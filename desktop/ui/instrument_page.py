"""Read-only security context with explicit links to existing workflows."""
from __future__ import annotations

import html
from typing import Any
from urllib.parse import quote, unquote

from PySide6.QtCore import QUrl, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from .client import GuardianClient
from .models import ActionReceipt
from .widgets import PageHeading

_STATUS = {"ready": "可查看", "empty": "暂无记录", "not_collected": "尚未采集",
           "failed": "读取失败", "expired": "已过期", "success": "最近检查成功",
           "partial": "部分结果不可用", "unknown": "无法确定", "UNKNOWN": "无法确定",
           "expected": "预计", "confirmed": "来源已确认", "cancelled": "已取消",
           "running": "刷新中", "stopped": "已停止", "permission_required": "来源访问受限"}
_STATUS.update(waiting_content="等待完整内容", needs_configuration="需要配置",
               invalid_output="分析结构无效", insufficient_evidence="证据不足")
_TYPES = {"earnings": "财报", "announcement": "公告", "dividend": "分红", "split": "拆股"}


def _text(value: Any) -> str:
    return html.escape(str(value)) if value is not None else "未记录"


def _status(section: dict[str, Any]) -> str:
    return _STATUS.get(str(section.get("status")), "状态未确认")


class InstrumentOverviewPage(QWidget):
    back_requested = Signal()
    review_requested = Signal(str)
    events_requested = Signal(str)
    report_requested = Signal(str)

    def __init__(self, client: GuardianClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self.instrument = ""
        self._pending: str | None = None
        self._value: dict[str, Any] | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.addWidget(PageHeading("SECURITY / 证券", "证券研究概览", "在一处查看关注、规则、报告和事件；读取本页不联网。"))
        self.identity_label = QLabel("从自选选择一只证券")
        self.identity_label.setWordWrap(True)
        root.addWidget(self.identity_label)
        controls = QHBoxLayout()
        self.back_button = QPushButton("返回自选列表")
        self.refresh_button = QPushButton("重新读取本地记录")
        self.review_button = QPushButton("核验与复盘")
        self.events_button = QPushButton("事件日历")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.refresh_button.clicked.connect(self.refresh)
        self.review_button.clicked.connect(lambda: self.review_requested.emit(self.instrument))
        self.events_button.clicked.connect(lambda: self.events_requested.emit(self.instrument))
        for button in (self.back_button, self.refresh_button, self.review_button, self.events_button):
            controls.addWidget(button)
        controls.addStretch(1)
        root.addLayout(controls)
        self.content = QTextBrowser()
        self.content.setOpenLinks(False)
        self.content.setOpenExternalLinks(False)
        self.content.setMinimumHeight(430)
        self.content.anchorClicked.connect(self._open_report)
        root.addWidget(self.content, 1)
        self.message = QLabel("证券概览只读取本地记录；各功能的启用状态分别保留。")
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        self.client.action_completed.connect(self._completed)
        self.client.request_failed.connect(self._failed)
        self._controls()

    def show_instrument(self, instrument: str) -> None:
        if instrument != self.instrument:
            self.instrument = instrument
            self._value = None
            self.content.setPlainText("正在读取该证券的本地记录…")
        self.identity_label.setText(instrument)
        self._controls()
        self.refresh()

    def refresh(self) -> None:
        if not self.instrument or self._pending is not None:
            return
        self._pending = self.instrument
        self.message.setText("正在读取本地记录…")
        self._controls()
        self.client.research_command("instrument_overview", {"instrument": self.instrument})

    def _controls(self) -> None:
        self.refresh_button.setEnabled(bool(self.instrument) and self._pending is None)
        self.review_button.setEnabled(bool(self.instrument))
        self.events_button.setEnabled(bool(self.instrument))

    def _completed(self, receipt: object) -> None:
        if (not isinstance(receipt, ActionReceipt) or receipt.action != "research-instrument_overview"
                or self._pending is None):
            return
        expected, self._pending = self._pending, None
        self._controls()
        if expected != self.instrument:
            self.refresh()
            return
        value = receipt.result
        if (not receipt.accepted or not isinstance(value, dict)
                or not isinstance(value.get("instrument"), dict)
                or value["instrument"].get("key") != expected):
            self._read_failed()
            return
        self._value = value
        self.identity_label.setText(f"{expected} · {value['instrument'].get('name') or ''}")
        self.content.setHtml(self._html(value))
        self.message.setText("本地快照 · " + str(value.get("as_of") or "时间未记录") + "；报告、事件和采集状态分别显示。")

    def _read_failed(self) -> None:
        self.message.setText("读取失败，保留上次本地快照；可重试。" if self._value else "读取失败，请重试；不能据此判断没有报告或事件。")
        if not self._value:
            self.content.setPlainText("本地记录暂不可读。")

    def _failed(self, purpose: str, _code: str) -> None:
        if purpose != "research-instrument_overview" or self._pending is None:
            return
        expected, self._pending = self._pending, None
        self._controls()
        if expected != self.instrument:
            self.refresh()
        else:
            self._read_failed()

    def _open_report(self, url: QUrl) -> None:
        if url.scheme() != "alpha-guard-report" or self._value is None:
            return
        identity = unquote(url.toString().split(":", 1)[1])
        allowed = {str(item.get("id")) for item in (self._value.get("reports") or {}).get("items", [])}
        if identity in allowed:
            self.report_requested.emit(identity)

    @staticmethod
    def _html(value: dict[str, Any]) -> str:
        member, rules = value.get("membership") or {}, value.get("rules") or {}
        scan = value.get("scan") or {}
        research = "已关注" if member.get("research_enabled") else "未关注"
        if member.get("research_enabled") and not member.get("research_market_enabled", True):
            research += " · 该市场研究未启用"
        monitoring = "规则监控已启用" if rules.get("monitoring_enabled") else "规则监控未启用"
        if rules.get("status") == "failed":
            monitoring = "规则配置读取失败"
        if member.get("status") == "failed":
            research = "关注状态读取失败"
        parts = [f"<h3>关注与监控</h3><p>{research} · {monitoring} · {_text(rules.get('rule_count'))} 条条件</p>",
                 f"<p>最近证券检查：{_status(scan)} · {_text(scan.get('observed_at'))}</p>"]
        if scan.get("detail") == "per_instrument_scan_not_recorded":
            parts.append("<p>此前未记录该证券的单独检查时间；不能用其他证券的扫描时间代替。</p>")
        reports = value.get("reports") or {}
        reviews = value.get("reviews") or {}
        parts.append(f"<h3>研究报告 · {_status(reports)}</h3><p>共 {_text(reports.get('total'))} 份，显示最近最多 5 份</p>")
        for report in reports.get("items") or []:
            identity = quote(str(report.get("id") or ""), safe="")
            horizon = {"short": "短线", "swing": "波段", "long": "长期"}.get(report.get("horizon"), "周期未记录")
            parts.append(f'<p><a href="alpha-guard-report:{identity}">{_text(report.get("created_at"))} · '
                         f'{horizon} · {_status(report)}</a><br>{_text(report.get("summary"))}</p>')
        parts.append(f"<h3>核验 · {_status(reviews)}</h3><p>待核验 {_text(reviews.get('pending_count'))} 份 · "
                     f"最近安排 {_text(reviews.get('next_review_at'))}</p>")
        events = value.get("events") or {}
        parts.append(f"<h3>未来事件 · {_status(events)}</h3><p>共 {_text(events.get('total'))} 项，显示最近最多 5 项</p>")
        for event in events.get("items") or []:
            parts.append(f"<p>{_text(event.get('event_date'))} · {_text(_TYPES.get(event.get('event_type'), '事件'))} · "
                         f"{_status(event)} · {_text(event.get('title'))}<br>来源 {_text(event.get('source'))} · "
                         f"取得于 {_text(event.get('observed_at'))}</p>")
        for source in events.get("sources") or []:
            state = _status(source)
            if source.get("status") == "failed":
                state = "刷新失败，保留旧记录"
            parts.append(f"<p>来源 {_text(source.get('source'))} · {state} · "
                         f"最后成功 {_text(source.get('last_success_at'))}</p>")
        if events.get("coverage"):
            parts.append(f"<p>{_text(events['coverage'])}</p>")
        return "".join(parts)
