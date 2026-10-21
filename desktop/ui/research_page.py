"""Explicitly activated research controls; opening this page only reads local IPC."""

from __future__ import annotations

import html
import ipaddress
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from PySide6.QtCore import QTime, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from .client import GuardianClient
from .generation import generation_html
from .models import ActionReceipt, DashboardSnapshot
from .widgets import PageHeading


MARKETS = {"US": "美股", "HK": "港股", "CN": "A 股"}
HORIZONS = {"short": "短线 / 日内", "swing": "几天至几周", "long": "中长期"}
PLATFORMS = {
    "youtube": "YouTube",
    "bilibili": "哔哩哔哩",
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "rss": "RSS / Atom",
}
DIGEST_AI_STATUSES = {
    "generated": "已生成",
    "rejected": "模型输出未通过核验，已回退确定性摘要",
    "unavailable": "模型不可用，保留确定性摘要",
    "disabled": "未开启",
}
DIGEST_DELIVERY_STATUSES = {
    "sent": "服务已接受（旧记录），接收待确认",
    "already_sent": "服务此前已接受（旧记录），接收待确认",
    "accepted": "服务已接受，接收待确认",
    "already_accepted": "服务此前已接受，接收待确认",
    "partial": "部分通道未接受，请查看各通道结果",
    "not_configured": "未配置通知通道",
    "failed": "发送失败",
    "cancelled": "已取消后续投递",
    "skipped": "未开启投递",
}
STATUSES = {
    "STOPPED": "已停止",
    "RUNNING": "运行中",
    "STOPPING": "正在停止",
    "success": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "cancelled": "已取消",
    "disabled": "未开启",
    "ready": "可供核验",
    "waiting_content": "等待完整内容",
    "needs_configuration": "需要配置",
    "insufficient_evidence": "证据不足",
    "invalid_output": "分析结构无效",
    "expired": "已过期",
    "permission_required": "需要来源授权",
    "unsupported": "暂不支持",
    "waiting_for_content": "等待完整内容",
}
STAGES = {
    "idle": "未采集",
    "queued": "准备中",
    "collecting": "采集信息",
    "reading": "读取原文",
    "market": "获取行情",
    "market_data": "获取行情",
    "capturing": "原速播放并提取音轨",
    "analyzing": "分析证据",
    "waiting": "等待下次检查",
    "transcribing": "本地转写",
}
ACTIONS = {
    "buy_candidate": "买入候选",
    "watch": "继续观察",
    "hold_review": "持有核验",
    "reduce_review": "减仓核验",
    "insufficient_evidence": "证据不足",
}
ISSUES = {
    "visual_content_not_extracted": "图片内容尚未核对，正文可供研究，结论需复核",
    "local_media_transcription": "由原视频完整音轨在本机转写",
    "media_unavailable": "视频尚未正常加载，可稍后重试或导入完整字幕",
    "media_budget_exhausted": "本轮视频数量额度已用完",
    "media_duration_budget_exhausted": "视频超过本轮时长上限，可导入完整字幕或音视频",
    "media_capture_incomplete": "视频未完整播放，未作为完整转写入库",
    "media_capture_protected": "受保护媒体无法提取，请使用已获准的字幕材料",
    "media_capture_unavailable": "当前播放器无法提取音轨，可导入字幕",
    "media_audio_unavailable": "播放器未提供可读取音轨",
    "media_source_changed": "播放中的视频源发生变化，未接受为完整音轨",
    "media_playback_changed": "播放进度或速度发生变化，需重新获取完整内容",
    "media_capture_timeout": "视频播放超时，可重试或导入字幕",
    "media_capture_security_error": "播放器不允许读取音轨，可导入已取得的字幕",
    "media_duration_mismatch": "实际音轨与视频时长不符，等待核验",
    "browser_media_advertisement": "正在播放广告，尚未读取视频正片",
    "browser_media_identity_mismatch": "视频身份未通过核验，未读取音轨",
    "recording_start_offset_negligible": "音轨起点有少量播放器启动偏移",
    "transcription_duration_mismatch": "转写时间点超出音频，等待核验",
    "invalid_transcriber_configuration": "尚未配置有效的本地转写器",
    "invalid_transcription_output": "转写文字或时间片段无效，未作为完整材料入库",
    "multipart_video_first_part_only": "只取得分 P 视频的首段，仍需补齐其余内容",
    "transcript_unavailable": "尚未取得完整字幕",
    "browser_transcript_unavailable": "平台页面尚未提供可读取的完整字幕",
    "browser_transcript_extent_unverified": "尚未确认文字稿覆盖完整视频",
    "complete_content_required": "需要完整正文或带时间点的访谈内容",
    "publication_time_unknown": "发布时间未知",
    "publication_time_in_future": "发布时间异常",
    "evidence_too_old_for_horizon": "材料时效不适合所选周期",
    "opinion_or_inference_requires_factual_verification": "观点或推断还需事实核验",
    "conflicting_evidence_requires_review": "正反证据存在冲突",
    "analysis_budget_exhausted": "本轮分析额度已用完，后续扫描继续",
    "conflicting_sources_requires_review": "不同材料给出相反方向，需交叉核实",
    "market_price_missing": "缺少参考价格",
    "market_source_time_unknown": "行情时间未知",
    "market_source_time_invalid_or_stale": "行情来源时间异常或过期",
    "market_observation_invalid_or_stale": "行情观测时间异常或过期",
    "market_provider_unavailable": "行情来源不可用",
    "market_data_security_mismatch": "行情与研究标的不匹配",
    "intraday_data_not_validated": "尚未验证盘中数据",
    "daily_data_cannot_support_short_horizon": "日线数据不足以支持短线判断",
    "intraday_volume_missing": "缺少盘中成交量",
    "cn_intraday_market_rules_not_validated": "A 股短线交易约束尚未验证",
    "fundamentals_and_valuation_incomplete": "基本面和估值数据不完整",
    "fundamentals_observation_invalid_or_stale": "基本面观测时间异常或过期",
    "source_url_missing": "缺少原始来源链接",
    "source_evidence_superseded": "来源已有更新版本，旧判断仅供历史参考",
    "transcript_timestamps_unknown": "转写文本缺少时间点",
    "machine_transcription_unverified": "机器转写尚未经人工核对",
    "local_transcriber_not_configured": "尚未配置本地转写器，可先导入字幕文件",
    "authorized_feed_or_content_import_required": "需要授权订阅源，或导入正文 / 字幕",
    "waiting_for_content": "已发现更新，等待完整正文或字幕",
    "missing_credentials": "尚未配置来源凭据",
    "invalid_response": "来源返回无效数据",
    "provider_error": "来源请求失败",
    "http_error": "来源请求被拒绝",
    "browser_disabled": "尚未启用浏览器读取",
    "browser_not_installed": "未找到 Chrome，请安装后重试",
    "browser_unavailable": "无法连接专用浏览器，请重试或检查 Chrome",
    "browser_login_required": "平台要求登录，请在 Alpha Guard 专用 Chrome 窗口登录后重新扫描",
    "subtitle_login_required": "读取字幕需要登录，请在 Alpha Guard 专用 Chrome 窗口登录 B站后重试",
    "browser_verification_required": "平台要求验证，请在专用 Chrome 窗口自行完成后重试",
    "browser_source_unavailable": "平台暂未提供作者作品，请稍后重试",
    "browser_content_unavailable": "作品正文暂不可读取，可导入已取得的材料",
    "browser_metadata_only": "只取得作品信息，等待正文或字幕",
    "browser_timeout": "浏览器读取超时，请稍后重试",
    "browser_page_error": "页面尚未正常加载，请检查专用 Chrome 窗口",
    "browser_navigation_rejected": "页面跳转离开所选平台，已停止读取",
    "research_failed": "研究未完成，请检查来源和模型配置后重试",
}


def _label(text: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def _safe_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme not in {"https", "http"}
            or not host
            or parsed.username
            or parsed.password
            or len(value) > 2048
            or host in {"localhost", "localhost.localdomain"}
            or host.endswith((".localhost", ".local", ".internal"))
        ):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return "." in host
        return address.is_global
    except ValueError:
        return False


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else "—"))


def _time(value: object) -> str:
    if not value:
        return "未知"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (TypeError, ValueError):
        return "未知"


def _source_link(
    value: object, label: str = "打开原文", start_seconds: object = None
) -> str:
    url = str(value or "")
    if (
        _safe_url(url)
        and isinstance(start_seconds, (int, float))
        and 0 <= start_seconds <= 86400
    ):
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if host in {
            "youtube.com",
            "www.youtube.com",
            "youtu.be",
            "www.bilibili.com",
            "bilibili.com",
        }:
            query = [
                (key, value) for key, value in parse_qsl(parsed.query) if key != "t"
            ]
            query.append(("t", str(int(start_seconds))))
            url = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")
            )
    return (
        f'<a href="{html.escape(url, quote=True)}">{_escape(label)}</a>'
        if _safe_url(url)
        else "原文链接不可用"
    )


def _table(columns: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(
        QHeaderView.ResizeMode.ResizeToContents
    )
    table.horizontalHeader().setStretchLastSection(True)
    table.setAlternatingRowColors(True)
    return table


class ResearchPage(QWidget):
    """The form drafts are independent of read-only status refreshes."""

    review_requested = Signal(str)
    instrument_requested = Signal(str)

    def __init__(self, client: GuardianClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("researchPage")
        self.client = client
        self._snapshot: dict[str, Any] = {}
        self._loaded = False
        self._active = False
        self._dirty = False
        self._digest_dirty = False
        self._loading_settings = False
        self._pending: set[str] = set()
        self._selected: dict[str, str] = {}
        self._detail_items: dict[str, dict[str, Any]] = {}
        self._detail_requests: dict[str, tuple[int, int]] = {}
        self._detail_pending: set[str] = set()
        self._page_history: dict[str, list[int]] = {
            "content": [],
            "segment": [],
            "claim": [],
        }
        self._page_moves: dict[str, tuple[str, int, int]] = {}
        self._evidence_view_initialized = False
        self._page_labels: dict[str, QLabel] = {}
        self._previous_buttons: dict[str, QPushButton] = {}
        self._next_buttons: dict[str, QPushButton] = {}
        self._reload_buttons: dict[str, QPushButton] = {}
        self._digest_history_offset = 0
        self._digest_history_requested: int | None = None
        self._digest_history_page: dict[str, Any] | None = None
        self._digest_history_ids: list[str] = []
        self._digest_display: dict[str, Any] = {}
        self._digest_detail_requested: tuple[str, int] | None = None
        self._digest_detail_expected: tuple[str, int] | None = None
        self._runtime_state = "STOPPED"
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.client.refresh_research)

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 20, 26, 24)
        root.setSpacing(12)
        root.addWidget(
            PageHeading(
                "06 / RESEARCH",
                "投资研究",
                "按需跟踪新闻和访谈，按投资周期查看证据与条件。启动应用不会开启监控。",
            )
        )
        self.runtime_label = _label("监控默认关闭 · 等待读取本地状态")
        self.runtime_label.setObjectName("researchRuntime")
        root.addWidget(self.runtime_label)
        controls = QHBoxLayout()
        self.once_button = QPushButton("立即扫描一次")
        self.start_button = QPushButton("开启自动跟踪")
        self.stop_button = QPushButton("停止跟踪")
        self.refresh_button = QPushButton("刷新状态")
        self.once_button.setProperty("primary", True)
        self.once_button.clicked.connect(lambda: self._send("once"))
        self.start_button.clicked.connect(lambda: self._send("start"))
        self.stop_button.clicked.connect(lambda: self._send("stop"))
        self.refresh_button.clicked.connect(self.client.refresh_research)
        for button in (
            self.once_button,
            self.start_button,
            self.stop_button,
            self.refresh_button,
        ):
            controls.addWidget(button)
        controls.addStretch(1)
        root.addLayout(controls)
        self.message_label = _label(
            "先保存关注标的、市场和周期，再选择扫描一次或自动跟踪。"
        )
        self.message_label.setObjectName("helpText")
        root.addWidget(self.message_label)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.report_table, self.report_detail = self._results_tab("report", "研究报告")
        self.evidence_table, self.evidence_detail = self._results_tab(
            "evidence", "信息材料"
        )
        self._build_subscriptions()
        self._build_settings()
        self._build_import()
        self._build_models()
        self._build_digest()
        self.run_detail = QTextBrowser()
        self.run_detail.setOpenExternalLinks(False)
        self.run_detail.setPlainText(
            "尚无运行记录。主动扫描后可在此查看来源状态、失败原因和历史结果。"
        )
        self.tabs.addTab(self.run_detail, "来源与运行")
        self.client.research_ready.connect(self.set_research_snapshot)
        self.client.research_detail_ready.connect(self.set_detail)
        self.client.action_completed.connect(self._action_completed)
        self.client.request_failed.connect(self._request_failed)
        self._update_controls()

    def set_snapshot(self, _snapshot: DashboardSnapshot) -> None:
        """The standard cockpit refresh does not mutate research form drafts."""

    def activate(self) -> None:
        self._active = True
        self.client.refresh_research()
        if self._runtime_state != "STOPPED":
            self._timer.start()

    def deactivate(self) -> None:
        self._active = False
        self._timer.stop()

    @property
    def close_requires_confirmation(self) -> bool:
        return self._runtime_state in {"RUNNING", "STOPPING"} or bool(
            self._pending & {"start", "once", "transcribe"}
        )

    @property
    def launch_pending(self) -> bool:
        return bool(self._pending & {"start", "once", "transcribe"})

    def request_stop(self) -> None:
        """Use the same deduplicated stop request as the research controls."""
        self._send("stop")

    def _results_tab(self, kind: str, title: str) -> tuple[QTableWidget, QTextBrowser]:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 12, 10, 12)
        columns = (
            ["标的", "周期", "建议", "状态", "有效至"]
            if kind == "report"
            else ["标题", "来源", "内容完整度", "发布时间"]
        )
        table = _table(columns)
        detail = QTextBrowser()
        detail.document().setDefaultStyleSheet("a { color: #71A8FF; }")
        detail.setOpenLinks(False)
        detail.setOpenExternalLinks(False)
        detail.anchorClicked.connect(self._open_source)
        detail.setPlainText(
            "暂无研究报告。保存研究设置后运行一次扫描。"
            if kind == "report"
            else "暂无信息材料。添加订阅或导入正文 / 字幕后开始研究。"
        )
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(table)
        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        pager = QHBoxLayout()
        if kind == "evidence":
            self.evidence_view = QComboBox()
            self.evidence_view.setAccessibleName("材料详情展示方式")
            self.evidence_view.addItem("原文正文", "content")
            self.evidence_view.addItem("字幕时间点", "segment")
            self.evidence_view.currentIndexChanged.connect(self._change_evidence_view)
            pager.addWidget(self.evidence_view)
        page_label = _label("选择一项查看详情")
        previous = QPushButton("上一段" if kind == "evidence" else "上一组论据")
        following = QPushButton("下一段" if kind == "evidence" else "下一组论据")
        previous.setEnabled(False)
        following.setEnabled(False)
        reload_button = QPushButton("重读详情")
        reload_button.setEnabled(False)
        previous.clicked.connect(lambda: self._turn_detail_page(kind, -1))
        following.clicked.connect(lambda: self._turn_detail_page(kind, 1))
        reload_button.clicked.connect(lambda: self._reload_detail(kind))
        self._page_labels[kind] = page_label
        self._previous_buttons[kind] = previous
        self._next_buttons[kind] = following
        self._reload_buttons[kind] = reload_button
        pager.addWidget(page_label, 1)
        pager.addWidget(previous)
        pager.addWidget(following)
        pager.addWidget(reload_button)
        if kind == "report":
            self.review_report_button = QPushButton("核验与复盘")
            self.review_report_button.setEnabled(False)
            self.review_report_button.clicked.connect(
                lambda: self.review_requested.emit(self._selected.get("report", "")))
            pager.addWidget(self.review_report_button)
            self.report_instrument_button = QPushButton("证券概览")
            self.report_instrument_button.setEnabled(False)
            self.report_instrument_button.clicked.connect(self._open_report_instrument)
            pager.addWidget(self.report_instrument_button)
        detail_layout.addLayout(pager)
        detail_layout.addWidget(detail, 1)
        splitter.addWidget(detail_panel)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter)
        table.itemSelectionChanged.connect(lambda: self._selection_changed(kind, table))
        self.tabs.addTab(panel, title)
        return table, detail

    def _build_settings(self) -> None:
        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(18, 18, 18, 18)
        form.setVerticalSpacing(16)
        self.market_checks = self._checks(MARKETS)
        self.horizon_checks = self._checks(HORIZONS)
        form.addRow("市场", self._check_row(self.market_checks))
        form.addRow("投资周期", self._check_row(self.horizon_checks))
        self.instruments_input = QLineEdit()
        self.instruments_input.setPlaceholderText("US:AAPL, HK:00700, CN:600519")
        self.instruments_input.setAccessibleName("关注股票，使用市场冒号代码，逗号分隔")
        self.instruments_input.textChanged.connect(self._mark_dirty)
        form.addRow("关注股票", self.instruments_input)
        form.addRow("", _label("使用 US、HK、CN 区分市场，可同时研究不同市场。"))
        self.interval_input = QSpinBox()
        self.interval_input.setRange(1, 1440)
        self.interval_input.setValue(15)
        self.interval_input.setSuffix(" 分钟")
        self.interval_input.valueChanged.connect(self._mark_dirty)
        form.addRow("自动检查间隔", self.interval_input)
        self.news_check = QCheckBox("扫描新闻")
        self.news_check.setChecked(True)
        self.news_check.toggled.connect(self._mark_dirty)
        form.addRow("新闻", self.news_check)
        self.source_checks = self._checks(
            {"finnhub": "Finnhub", "newsapi": "NewsAPI", "akshare": "东方财富"}
        )
        form.addRow("新闻来源", self._check_row(self.source_checks))
        form.addRow(
            "",
            _label(
                "未指定来源时沿用已配置新闻源。Finnhub、NewsAPI 需要有效的来源凭据。"
            ),
        )
        self.ai_check = QCheckBox("启用 AI 提取与投资研究")
        self.ai_check.toggled.connect(self._mark_dirty)
        form.addRow("AI 分析", self.ai_check)
        self.ai_configuration_hint = _label(
            "AI 默认关闭。支持 Anthropic API 或本机已登录的模型客户端；仅主动扫描时分析。"
        )
        form.addRow("", self.ai_configuration_hint)
        self.content_configuration_hint = _label(
            "浏览器读取和本地转写需要配置后使用，添加订阅不会自动运行。"
        )
        form.addRow("内容获取", self.content_configuration_hint)
        self.media_check = QCheckBox("无字幕时本地转写视频（需原速播放）")
        self.media_check.toggled.connect(self._mark_dirty)
        form.addRow("视频转写", self.media_check)
        self.max_media_input = QSpinBox()
        self.max_media_input.setRange(1, 5)
        self.max_media_input.setValue(2)
        self.max_media_input.valueChanged.connect(self._mark_dirty)
        form.addRow("每轮转写视频上限", self.max_media_input)
        self.media_minutes_input = QSpinBox()
        self.media_minutes_input.setRange(1, 10)
        self.media_minutes_input.setValue(10)
        self.media_minutes_input.setSuffix(" 分钟")
        self.media_minutes_input.valueChanged.connect(self._mark_dirty)
        form.addRow("单条视频时长上限", self.media_minutes_input)
        form.addRow("", _label("仅主动扫描时运行；播放器静音，可随时停止。长访谈优先读取完整字幕，或导入完整音视频。机器转写需核对后再用于方向性判断。"))
        self.max_items_input = QSpinBox()
        self.max_items_input.setRange(1, 100)
        self.max_items_input.setValue(20)
        self.max_items_input.valueChanged.connect(self._mark_dirty)
        self.max_ai_input = QSpinBox()
        self.max_ai_input.setRange(0, 50)
        self.max_ai_input.setValue(10)
        self.max_ai_input.valueChanged.connect(self._mark_dirty)
        form.addRow("每轮材料上限", self.max_items_input)
        form.addRow("每轮 AI 调用上限", self.max_ai_input)
        form.addRow(
            "",
            _label(
                "短线判断需要及时行情与成交信息；A 股日线和未验证的交易约束会限制短线建议。"
            ),
        )
        self.save_button = QPushButton("保存研究设置")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self._save_settings)
        form.addRow("", self.save_button)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.settings_content = content
        self.tabs.addTab(scroll, "研究设置")

    def _checks(self, labels: dict[str, str]) -> dict[str, QCheckBox]:
        result = {key: QCheckBox(label) for key, label in labels.items()}
        for widget in result.values():
            widget.toggled.connect(self._mark_dirty)
        return result

    @staticmethod
    def _check_row(checks: dict[str, QCheckBox]) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        for check in checks.values():
            layout.addWidget(check)
        layout.addStretch(1)
        return row

    def _build_subscriptions(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(
            _label(
                "添加作者主页。自动跟踪只在你开启后运行；平台需要登录时，可先打开专用浏览器完成登录，或提供授权订阅源。"
            )
        )
        form = QFormLayout()
        self.platform_input = QComboBox()
        for code, title in PLATFORMS.items():
            self.platform_input.addItem(title, code)
        self.subscription_url = QLineEdit()
        self.subscription_url.setPlaceholderText("作者主页或 RSS / Atom 地址")
        self.subscription_label = QLineEdit()
        self.subscription_label.setPlaceholderText("作者或频道名称")
        self.subscription_feed = QLineEdit()
        self.subscription_feed.setPlaceholderText("可选：授权的 RSS / Atom 地址")
        form.addRow("平台", self.platform_input)
        form.addRow("主页", self.subscription_url)
        form.addRow("名称", self.subscription_label)
        form.addRow("订阅源", self.subscription_feed)
        layout.addLayout(form)
        add = QPushButton("添加订阅")
        add.clicked.connect(self._subscribe)
        layout.addWidget(add)
        self.subscription_table = _table(["名称", "平台", "跟踪", "最近状态"])
        self.subscription_table.setMinimumHeight(150)
        layout.addWidget(self.subscription_table, 1)
        actions = QHBoxLayout()
        self.toggle_subscription_button = QPushButton("暂停 / 恢复所选订阅")
        self.delete_subscription_button = QPushButton("移除所选订阅")
        self.toggle_subscription_button.clicked.connect(self._toggle_subscription)
        self.delete_subscription_button.clicked.connect(self._delete_subscription)
        actions.addWidget(self.toggle_subscription_button)
        actions.addWidget(self.delete_subscription_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.subscription_status = _label("尚未添加订阅。")
        layout.addWidget(self.subscription_status)
        self.subscription_table.itemSelectionChanged.connect(
            self._show_subscription_status
        )
        login_row = QHBoxLayout()
        self.login_platform_input = QComboBox()
        self.login_platform_input.setAccessibleName("需要登录的平台")
        for code, title in PLATFORMS.items():
            if code != "rss":
                self.login_platform_input.addItem(title, code)
        self.open_login_button = QPushButton("打开平台登录")
        self.open_login_button.clicked.connect(self._open_platform_login)
        self.platform_input.currentIndexChanged.connect(self._sync_login_platform)
        login_row.addWidget(self.login_platform_input)
        login_row.addWidget(self.open_login_button)
        login_row.addStretch(1)
        layout.insertLayout(3, login_row)
        self.login_hint = _label("等待读取浏览器配置。打开登录页不会启动研究扫描。")
        layout.insertWidget(4, self.login_hint)
        panel.setMinimumHeight(490)
        self._add_scroll_tab(panel, "博主订阅")

    def _build_import(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(
            _label(
                "补充正文、字幕或已获授权的音视频。材料导入后，运行一次扫描即可参与研究。"
            )
        )
        form = QFormLayout()
        self.import_title = QLineEdit()
        self.import_url = QLineEdit()
        self.import_url.setPlaceholderText("原文或视频链接；用于核验和跳转")
        self.import_author = QLineEdit()
        self.import_date = QLineEdit()
        self.import_date.setPlaceholderText("可选，例如 2026-09-30T10:00:00+08:00")
        self.import_kind = QComboBox()
        for label, kind in (
            ("完整正文", "article"),
            ("SRT 字幕", "srt"),
            ("VTT 字幕", "vtt"),
            ("访谈文字", "transcript"),
            ("摘要", "summary"),
        ):
            self.import_kind.addItem(label, kind)
        form.addRow("标题", self.import_title)
        form.addRow("原始来源", self.import_url)
        form.addRow("作者", self.import_author)
        form.addRow("发布时间", self.import_date)
        form.addRow("内容类型", self.import_kind)
        layout.addLayout(form)
        self.import_text = QPlainTextEdit()
        self.import_text.setMinimumHeight(150)
        self.import_text.setPlaceholderText(
            "粘贴正文或字幕。字幕的时间点和说话人会保留；缺失信息不会被自动补造。"
        )
        layout.addWidget(self.import_text, 1)
        actions = QHBoxLayout()
        self.import_button = QPushButton("导入材料")
        self.import_button.clicked.connect(self._import_material)
        load_file = QPushButton("选择文本 / 字幕文件")
        load_file.clicked.connect(self._choose_text)
        self.transcribe_button = QPushButton("选择音视频并本地转写")
        self.transcribe_button.clicked.connect(self._choose_media)
        for button in (self.import_button, load_file, self.transcribe_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        layout.addWidget(
            _label(
                "本地转写需要先配置转写器。未配置时会显示待配置状态；也可以直接导入现成字幕。"
            )
        )
        panel.setMinimumHeight(530)
        self._add_scroll_tab(panel, "导入材料")

    def _add_scroll_tab(self, panel: QWidget, title: str) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        self.tabs.addTab(scroll, title)

    def _build_models(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(_label(
            "用自己的 API key 接入 OpenAI 兼容或 Anthropic 兼容服务；密钥只存本机钥匙串，"
            "可以保存 ZCode 等多个服务并随时激活切换；使用默认模型可切回本机 Codex（默认提供者为 Codex 时）。"
        ))
        self.model_current = _label("当前使用：尚未读取")
        layout.addWidget(self.model_current)
        self.model_table = _table(["名称", "协议", "模型", "端点", "密钥", "状态"])
        self.model_table.setMinimumHeight(140)
        self.model_table.itemSelectionChanged.connect(self._show_model_status)
        layout.addWidget(self.model_table, 1)
        form = QFormLayout()
        self.model_name = QLineEdit()
        self.model_name.setPlaceholderText("例如 ZCode-模型名、Codex API-模型名")
        self.model_protocol = QComboBox()
        self.model_protocol.addItem("OpenAI 兼容 /chat/completions", "openai_compatible")
        self.model_protocol.addItem("Anthropic 兼容 /v1/messages", "anthropic_compatible")
        self.model_url = QLineEdit()
        self.model_url.setPlaceholderText("https://api.example.com/v1（公网须 HTTPS）")
        self.model_id_input = QLineEdit()
        self.model_id_input.setPlaceholderText("例如 gpt-5.5、deepseek-chat")
        self.model_key = QLineEdit()
        self.model_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.model_key.setPlaceholderText("输入新密钥以更新；留空保留已存密钥")
        self.model_timeout = QSpinBox()
        self.model_timeout.setRange(5, 600)
        self.model_timeout.setSuffix(" 秒")
        self.model_timeout.setValue(180)
        self.model_private = QCheckBox("允许本机/私网端点（Ollama、LM Studio 等）")
        self.model_enabled = QCheckBox("启用该 profile")
        self.model_enabled.setChecked(True)
        form.addRow("名称", self.model_name)
        form.addRow("协议", self.model_protocol)
        form.addRow("端点", self.model_url)
        form.addRow("模型", self.model_id_input)
        form.addRow("API 密钥", self.model_key)
        form.addRow("超时", self.model_timeout)
        form.addRow("", self.model_private)
        form.addRow("", self.model_enabled)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.model_save_button = QPushButton("保存 / 新建")
        self.model_save_button.setProperty("primary", True)
        self.model_delete_button = QPushButton("删除所选")
        self.model_activate_button = QPushButton("激活所选")
        self.model_deactivate_button = QPushButton("使用默认模型")
        self.model_test_button = QPushButton("测试连接")
        self.model_save_button.clicked.connect(self._save_model_profile)
        self.model_delete_button.clicked.connect(self._delete_model_profile)
        self.model_activate_button.clicked.connect(self._activate_model_profile)
        self.model_deactivate_button.clicked.connect(self._deactivate_model_profile)
        self.model_test_button.clicked.connect(self._test_model_profile)
        for button in (self.model_save_button, self.model_delete_button,
                       self.model_activate_button, self.model_deactivate_button,
                       self.model_test_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.model_status = _label("尚未保存模型 profile。")
        layout.addWidget(self.model_status)
        panel.setMinimumHeight(520)
        self._add_scroll_tab(panel, "模型")

    def _build_digest(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(_label(
            "按关注清单生成每日摘要：证券沿用研究设置的“关注股票”，主题在此填写；"
            "默认关闭，显式开启后 Guardian 重启也会按设定时刻继续执行。"
        ))
        form = QFormLayout()
        self.digest_enabled = QCheckBox("开启每日摘要")
        self.digest_time = QTimeEdit()
        self.digest_time.setDisplayFormat("HH:mm")
        self.digest_time.setTime(QTime(8, 0))
        self.digest_topics = QPlainTextEdit()
        self.digest_topics.setPlaceholderText(
            "每行一个主题：标签: 关键词1, 关键词2\n例如\n半导体: 芯片, 光刻, 先进制程\n创新药: 临床, FDA"
        )
        self.digest_topics.setMinimumHeight(80)
        self.digest_ai = QCheckBox("AI 原文选读（每轮最多一次模型调用）")
        self.digest_notify = QCheckBox("生成后发送通知")
        self.digest_telegram = QCheckBox("Telegram · 摘要正文")
        self.digest_whatsapp = QCheckBox("WhatsApp · 模板通知")
        self.digest_telegram.setChecked(True)
        self.digest_whatsapp.setChecked(True)
        self.digest_max_items = QSpinBox()
        self.digest_max_items.setRange(1, 100)
        self.digest_max_items.setValue(20)
        for widget in (self.digest_enabled, self.digest_ai, self.digest_notify,
                       self.digest_telegram, self.digest_whatsapp):
            widget.toggled.connect(self._mark_digest_dirty)
        self.digest_time.timeChanged.connect(self._mark_digest_dirty)
        self.digest_topics.textChanged.connect(self._mark_digest_dirty)
        self.digest_max_items.valueChanged.connect(self._mark_digest_dirty)
        form.addRow("计划", self.digest_enabled)
        form.addRow("每日时刻", self.digest_time)
        form.addRow("主题", self.digest_topics)
        form.addRow("", self.digest_ai)
        form.addRow("", self.digest_notify)
        channels = QHBoxLayout()
        channels.addWidget(self.digest_telegram)
        channels.addWidget(self.digest_whatsapp)
        channels.addStretch(1)
        form.addRow("接收渠道（可多选）", channels)
        form.addRow("", _label("渠道需先配置凭据和接收端。WhatsApp 当前只发送已批准模板，摘要正文请在桌面查看。"))
        form.addRow("每组条目上限", self.digest_max_items)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.digest_save_button = QPushButton("保存摘要设置")
        self.digest_save_button.setProperty("primary", True)
        self.digest_run_button = QPushButton("立即生成一次")
        self.digest_save_button.clicked.connect(self._save_digest_settings)
        self.digest_run_button.clicked.connect(self._run_digest_once)
        actions.addWidget(self.digest_save_button)
        actions.addWidget(self.digest_run_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.digest_status = _label("每日摘要默认关闭。")
        layout.addWidget(self.digest_status)
        self.digest_history = _table(["生成时间", "触发", "状态", "命中/窗口"])
        self.digest_history.setMinimumHeight(110)
        self.digest_history.itemSelectionChanged.connect(self._show_digest_record)
        layout.addWidget(_label("历史摘要（选择一行查看完整内容；离线读取）。"))
        layout.addWidget(self.digest_history)
        history_controls = QHBoxLayout()
        self.digest_history_label = _label("正在读取历史摘要…")
        self.digest_history_previous = QPushButton("上一页")
        self.digest_history_next = QPushButton("下一页")
        self.digest_history_refresh = QPushButton("刷新历史")
        self.digest_history_previous.clicked.connect(
            lambda: self._request_digest_history(max(0, self._digest_history_offset - 20)))
        self.digest_history_next.clicked.connect(self._next_digest_history)
        self.digest_history_refresh.clicked.connect(lambda: self._request_digest_history(0))
        history_controls.addWidget(self.digest_history_label, 1)
        for button in (self.digest_history_previous, self.digest_history_next, self.digest_history_refresh):
            history_controls.addWidget(button)
        layout.addLayout(history_controls)
        detail_controls = QHBoxLayout()
        self.digest_detail_label = _label("选择历史摘要查看完整内容")
        self.digest_detail_previous = QPushButton("上一组条目")
        self.digest_detail_next = QPushButton("下一组条目")
        self.digest_detail_latest = QPushButton("查看最近一份")
        self.digest_detail_previous.clicked.connect(lambda: self._request_digest_detail(
            self._selected.get("digest", ""), max(0, int(self._digest_display.get("digest_offset", 0)) - 20)))
        self.digest_detail_next.clicked.connect(self._next_digest_detail)
        self.digest_detail_latest.clicked.connect(self._show_latest_digest)
        self.digest_detail_previous.setEnabled(False)
        self.digest_detail_next.setEnabled(False)
        detail_controls.addWidget(self.digest_detail_label, 1)
        for button in (self.digest_detail_previous, self.digest_detail_next, self.digest_detail_latest):
            detail_controls.addWidget(button)
        layout.addLayout(detail_controls)
        self.digest_detail = QTextBrowser()
        self.digest_detail.setOpenLinks(False)
        self.digest_detail.setOpenExternalLinks(False)
        self.digest_detail.anchorClicked.connect(self._open_digest_source)
        self.digest_detail.setPlainText("尚无摘要。开启计划或点击“立即生成一次”。")
        layout.addWidget(self.digest_detail, 1)
        panel.setMinimumHeight(520)
        self._add_scroll_tab(panel, "每日摘要")

    def _mark_dirty(self, *_args: object) -> None:
        if self._loading_settings:
            return
        self._dirty = True
        self.message_label.setText("研究设置有未保存修改，保存后可开始扫描。")
        self._update_controls()

    def _mark_digest_dirty(self, *_args: object) -> None:
        if self._loading_settings:
            return
        self._digest_dirty = True
        self.digest_status.setText("摘要设置有未保存修改。")
        self._update_controls()

    def _save_settings(self) -> None:
        try:
            markets = [
                key for key, widget in self.market_checks.items() if widget.isChecked()
            ]
            horizons = [
                key for key, widget in self.horizon_checks.items() if widget.isChecked()
            ]
            if not markets or not horizons:
                raise ValueError("请至少选择一个市场和一个投资周期。")
            instruments = []
            previous = {
                (item.get("market"), item.get("ticker")): item
                for item in self._snapshot.get("settings", {}).get("instruments", [])
            }
            seen = set()
            for token in re.split(r"[,，\s]+", self.instruments_input.text().strip()):
                if not token:
                    continue
                match = re.fullmatch(
                    r"(US|HK|CN):([A-Za-z0-9][A-Za-z0-9._-]{0,63})", token.upper()
                )
                if match is None:
                    raise ValueError("股票格式应为 US:AAPL、HK:00700 或 CN:600519。")
                market, ticker = match.groups()
                if (market, ticker) in seen:
                    continue
                seen.add((market, ticker))
                old = previous.get((market, ticker), {})
                instruments.append(
                    {
                        "ticker": ticker,
                        "market": market,
                        "name": old.get("name", ""),
                        "exchange": old.get("exchange", ""),
                    }
                )
            if len(instruments) > 100:
                raise ValueError("最多可配置 100 个关注标的。")
            if any(item["market"] not in markets for item in instruments):
                raise ValueError("关注股票包含未选中的市场，请勾选对应市场。")
            settings = {
                "markets": markets,
                "horizons": horizons,
                "instruments": instruments,
                "interval_minutes": self.interval_input.value(),
                "ai_enabled": self.ai_check.isChecked(),
                "news_enabled": self.news_check.isChecked(),
                "news_sources": [
                    key
                    for key, widget in self.source_checks.items()
                    if widget.isChecked()
                ],
                "max_items": self.max_items_input.value(),
                "max_ai_calls": self.max_ai_input.value(),
                "media_enabled": self.media_check.isChecked(),
                "max_media_items": self.max_media_input.value(),
                "max_media_seconds": self.media_minutes_input.value() * 60,
            }
        except ValueError as exc:
            self.message_label.setText(str(exc))
            return
        self._send("configure", {"settings": settings})

    def _subscribe(self) -> None:
        url = self.subscription_url.text().strip()
        feed = self.subscription_feed.text().strip()
        if not _safe_url(url) or (feed and not _safe_url(feed)):
            self.message_label.setText(
                "请填写公开的 HTTP(S) 主页和订阅源，不要包含账号密码。"
            )
            return
        self._send(
            "subscribe",
            {
                "subscription": {
                    "platform": self.platform_input.currentData(),
                    "url": url,
                    "label": self.subscription_label.text().strip()[:200],
                    "feed_url": feed or None,
                    "enabled": True,
                }
            },
        )

    def _subscription(self) -> dict[str, Any] | None:
        row = self.subscription_table.currentRow()
        item = self.subscription_table.item(row, 0)
        identity = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return next(
            (
                entry
                for entry in self._snapshot.get("subscriptions", [])
                if entry.get("id") == identity
            ),
            None,
        )

    def _toggle_subscription(self) -> None:
        item = self._subscription()
        if item:
            self._send(
                "subscription",
                {"id": item["id"], "enabled": not item.get("enabled", True)},
            )

    def _delete_subscription(self) -> None:
        item = self._subscription()
        if item:
            self._send("delete", {"id": item["id"]})

    def _show_subscription_status(self) -> None:
        item = self._subscription()
        if item:
            detail = self._subscription_health(str(item["id"]))
            self.subscription_status.setText(f"{item.get('url', '')}\n{detail}")
            index = self.login_platform_input.findData(item.get("platform"))
            if index >= 0:
                self.login_platform_input.setCurrentIndex(index)

    def _sync_login_platform(self) -> None:
        index = self.login_platform_input.findData(self.platform_input.currentData())
        if index >= 0:
            self.login_platform_input.setCurrentIndex(index)

    def _open_platform_login(self) -> None:
        if self.open_login_button.isEnabled():
            self._send("open_login", {"platform": self.login_platform_input.currentData()})

    def _import_material(self) -> None:
        text = self.import_text.toPlainText().strip()
        url = self.import_url.text().strip()
        date = self.import_date.text().strip()
        if not text or len(text) > 250_000:
            self.message_label.setText("请提供 1 至 250,000 字符的正文或字幕。")
            return
        if url and not _safe_url(url):
            self.message_label.setText("原始来源必须是公开的 HTTP(S) 链接。")
            return
        if date:
            try:
                parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            except ValueError:
                self.message_label.setText(
                    "发布时间需要时区，例如 2026-09-30T10:00:00+08:00。"
                )
                return
        self._send(
            "import",
            {
                "material": {
                    "text": text,
                    "kind": self.import_kind.currentData(),
                    "source_url": url,
                    "title": self.import_title.text().strip()[:500] or "用户导入材料",
                    "author": self.import_author.text().strip()[:200],
                    "platform": "manual",
                    "published_at": date or None,
                }
            },
        )

    def _choose_text(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "选择正文或字幕", "", "文本与字幕 (*.txt *.md *.srt *.vtt)"
        )
        if not filename:
            return
        try:
            path = Path(filename)
            if path.stat().st_size > 1_000_000:
                raise ValueError
            text = path.read_text(encoding="utf-8-sig")
            if len(text) > 250_000:
                raise ValueError
        except (OSError, UnicodeError, ValueError):
            self.message_label.setText(
                "无法读取文件，请选择不超过 250,000 字符的 UTF-8 文本或字幕。"
            )
            return
        self.import_text.setPlainText(text)
        if not self.import_title.text():
            self.import_title.setText(path.stem)
        kind = path.suffix.lower().lstrip(".")
        self.import_kind.setCurrentIndex(max(0, self.import_kind.findData(kind)))

    def _choose_media(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择需要转写的音视频",
            "",
            "音视频 (*.mp3 *.mp4 *.wav *.m4a *.mov *.mkv *.webm *.ogg *.flac *.aac)",
        )
        if not filename:
            return
        url = self.import_url.text().strip()
        if url and not _safe_url(url):
            self.message_label.setText("请先填写有效的原始来源链接或清空该字段。")
            return
        self._send(
            "transcribe",
            {
                "path": filename,
                "source_url": url,
                "title": self.import_title.text().strip() or Path(filename).stem,
            },
        )

    def _selected_model(self) -> dict[str, Any] | None:
        row = self.model_table.currentRow()
        profiles = self._snapshot.get("model_profiles")
        if row < 0 or not isinstance(profiles, list) or row >= len(profiles):
            return None
        profile = profiles[row]
        return profile if isinstance(profile, dict) else None

    def _show_model_status(self) -> None:
        profile = self._selected_model()
        if profile is None:
            self.model_status.setText("选择一个 profile 查看状态，或直接填写新 profile。")
            return
        active = (self._snapshot.get("capabilities") or {}).get("model_profile_active") or {}
        state = "当前激活" if active.get("id") == profile.get("id") else ("启用" if profile.get("enabled") else "已停用")
        key = "已配置" if profile.get("key_configured") else "未配置（本机推理可不填）"
        self.model_status.setText(
            f"{profile.get('name')} · {state} · 密钥{key} · {profile.get('protocol')}"
        )
        self.model_name.setText(str(profile.get("name") or ""))
        self.model_protocol.setCurrentIndex(
            1 if profile.get("protocol") == "anthropic_compatible" else 0)
        self.model_url.setText(str(profile.get("base_url") or ""))
        self.model_id_input.setText(str(profile.get("model") or ""))
        self.model_timeout.setValue(int(profile.get("timeout_seconds") or 180))
        self.model_private.setChecked(profile.get("allow_private_network") is True)
        self.model_enabled.setChecked(profile.get("enabled") is not False)
        self.model_key.clear()

    def _save_model_profile(self) -> None:
        profile = self._selected_model()
        payload: dict[str, Any] = {
            "name": self.model_name.text().strip(),
            "protocol": self.model_protocol.currentData(),
            "base_url": self.model_url.text().strip(),
            "model": self.model_id_input.text().strip(),
            "timeout_seconds": float(self.model_timeout.value()),
            "allow_private_network": self.model_private.isChecked(),
            "enabled": self.model_enabled.isChecked(),
        }
        fields: dict[str, Any] = {"profile": payload}
        if profile is not None:
            payload["id"] = profile.get("id")
        if self.model_key.text():
            fields["api_key"] = self.model_key.text()
        self._send("model_save", fields)

    def _delete_model_profile(self) -> None:
        profile = self._selected_model()
        if profile is None:
            self.message_label.setText("请先在表格中选择要删除的模型 profile。")
            return
        confirm = QMessageBox.question(
            self, "删除模型 profile",
            f"删除 {profile.get('name')}？其密钥会一并从钥匙串移除。",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._send("model_delete", {"id": profile.get("id")})

    def _activate_model_profile(self) -> None:
        profile = self._selected_model()
        if profile is None:
            self.message_label.setText("请先在表格中选择要激活的模型 profile。")
            return
        self._send("model_activate", {"id": profile.get("id")})

    def _deactivate_model_profile(self) -> None:
        self._send("model_activate", {"id": ""})

    def _test_model_profile(self) -> None:
        profile = self._selected_model()
        if profile is None:
            self.message_label.setText("请先在表格中选择要测试的模型 profile。")
            return
        confirm = QMessageBox.question(
            self, "测试连接",
            "将向该 profile 发起一次极小的真实模型请求（可能产生费用）。继续？",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._send("model_test", {"id": profile.get("id")})

    def _save_digest_settings(self) -> None:
        channels = [name for name, checkbox in (("telegram", self.digest_telegram),
                                                ("whatsapp", self.digest_whatsapp)) if checkbox.isChecked()]
        if self.digest_notify.isChecked() and not channels:
            self.message_label.setText("请至少选择一个接收渠道，或关闭生成后发送通知。")
            return
        topics = []
        for line in self.digest_topics.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            label, _, rest = line.partition(":")
            keywords = [value.strip() for value in rest.split(",") if value.strip()]
            topics.append({"label": label.strip(), "keywords": keywords[:8]})
        value = {
            "enabled": self.digest_enabled.isChecked(),
            "time_local": self.digest_time.time().toString("HH:mm"),
            "topics": topics[:20],
            "ai_summary": self.digest_ai.isChecked(),
            "notify": self.digest_notify.isChecked(),
            "notify_channels": channels,
            "max_items": self.digest_max_items.value(),
        }
        self._send("digest_save", {"settings": value})

    def _run_digest_once(self) -> None:
        if self._digest_dirty or "digest_save" in self._pending:
            self.message_label.setText("请先保存摘要设置，确认接收渠道生效后再生成。")
            return
        self._send("digest_once")

    def _populate_models(self) -> None:
        profiles = self._snapshot.get("model_profiles")
        if not isinstance(profiles, list):
            profiles = []
        active = (self._snapshot.get("capabilities") or {}).get("model_profile_active") or {}
        provider = str((self._snapshot.get("capabilities") or {}).get("model_provider") or "尚未确认")
        self.model_deactivate_button.setToolTip("停用 API 配置，使用本机默认提供者；不会删除 API Key。")
        current = f"{active.get('name')} · {active.get('model')}" if active else {"codex_cli": "本机 Codex", "claude_cli": "本机 Claude"}.get(provider, provider)
        self.model_current.setText(f"当前使用：{current}；切换仅影响后续生成，已有报告保留原模型记录。")
        self.model_table.setRowCount(0)
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            row = self.model_table.rowCount()
            self.model_table.insertRow(row)
            values = [
                str(profile.get("name") or ""),
                "Anthropic 兼容" if profile.get("protocol") == "anthropic_compatible" else "OpenAI 兼容",
                str(profile.get("model") or ""),
                str(profile.get("base_url") or ""),
                "已配置" if profile.get("key_configured") else "未配置",
                ("当前激活 · " if active.get("id") == profile.get("id") else "")
                + ("启用" if profile.get("enabled") else "已停用"),
            ]
            for column, value in enumerate(values):
                self.model_table.setItem(row, column, QTableWidgetItem(value))

    def _populate_digest(self) -> None:
        digest = self._snapshot.get("digest")
        if not isinstance(digest, dict):
            return
        settings = digest.get("settings") or {}
        plan = digest.get("plan") or {}
        if not self._digest_dirty:
            self._loading_settings = True
            try:
                self.digest_enabled.setChecked(settings.get("enabled") is True)
                hour, minute = str(settings.get("time_local") or "08:00").split(":", 1)
                self.digest_time.setTime(QTime(int(hour), int(minute)))
                self.digest_topics.setPlainText("\n".join(
                    f"{topic.get('label')}: " + ", ".join(str(v) for v in (topic.get("keywords") or []))
                    for topic in settings.get("topics") or [] if isinstance(topic, dict)))
                self.digest_ai.setChecked(settings.get("ai_summary") is True)
                self.digest_notify.setChecked(settings.get("notify") is True)
                channels = settings.get("notify_channels", ["telegram", "whatsapp"])
                self.digest_telegram.setChecked("telegram" in channels)
                self.digest_whatsapp.setChecked("whatsapp" in channels)
                self.digest_max_items.setValue(int(settings.get("max_items") or 20))
            finally:
                self._loading_settings = False
        state = "已开启" if settings.get("enabled") else "已关闭"
        nxt = f" · 下次 {_time(plan.get('next_run_at'))}" if plan.get("next_run_at") else ""
        self.digest_status.setText(f"每日摘要{state}{nxt}；“热点”仅为已配置来源内的相对排序。")
        if self._digest_history_page is None:
            history = digest.get("history")
            self._render_digest_history(history if isinstance(history, list) else [])
            self._request_digest_history(0)
        latest = digest.get("latest")
        if "digest" not in self._selected:
            self._digest_display = latest if isinstance(latest, dict) else {}
            self.digest_detail.setHtml(self._digest_html(latest) if isinstance(latest, dict) else
                                       "尚无摘要。开启计划或点击“立即生成一次”。")
            if isinstance(latest, dict) and latest.get("id"):
                self._request_digest_detail(str(latest["id"]), 0)

    def _render_digest_history(self, history: list[dict[str, Any]]) -> None:
        self._digest_history_ids = [str(record.get("id")) for record in history]
        self.digest_history.blockSignals(True)
        self.digest_history.setRowCount(len(history))
        self.digest_history.clearSelection()
        for row, record in enumerate(history):
            counts = record.get("counts") or {}
            for column, value in enumerate([
                _time(record.get("created_at")),
                "计划" if record.get("trigger") == "schedule" else "手动",
                STATUSES.get(str(record.get("status")), str(record.get("status"))),
                f"{counts.get('matched', 0)}/{counts.get('evidence_window', 0)}",
            ]):
                self.digest_history.setItem(row, column, QTableWidgetItem(str(value)))
            if record.get("id") == self._selected.get("digest"):
                self.digest_history.selectRow(row)
        self.digest_history.blockSignals(False)

    def _request_digest_history(self, offset: int) -> None:
        if self._digest_history_requested is not None:
            return
        self._digest_history_requested = offset
        self.digest_history_label.setText("正在读取本地历史…")
        self._update_digest_history_controls()
        self.client.research_detail("digest_history", "history", offset=offset)

    def _next_digest_history(self) -> None:
        offset = (self._digest_history_page or {}).get("next_offset")
        if type(offset) is int:
            self._request_digest_history(offset)

    def _update_digest_history_controls(self) -> None:
        pending = self._digest_history_requested is not None
        self.digest_history_previous.setEnabled(not pending and self._digest_history_offset > 0)
        self.digest_history_next.setEnabled(not pending and
            (self._digest_history_page or {}).get("next_offset") is not None)
        self.digest_history_refresh.setEnabled(not pending)

    def _show_digest_record(self) -> None:
        row = self.digest_history.currentRow()
        if row < 0 or row >= len(self._digest_history_ids):
            return
        identity = self._digest_history_ids[row]
        self._digest_display = {}
        self.digest_detail.setPlainText("正在读取本地摘要…")
        self._request_digest_detail(identity, 0)

    def _request_digest_detail(self, identity: str, offset: int) -> None:
        if not identity:
            return
        self._selected["digest"] = identity
        self._digest_detail_requested = (identity, offset)
        self._digest_detail_expected = (identity, offset)
        self.digest_detail_label.setText("正在读取本地摘要…")
        self._update_digest_detail_controls()
        self.client.research_detail("digest", identity, offset=offset)

    def _next_digest_detail(self) -> None:
        offset = self._digest_display.get("digest_next_offset")
        if type(offset) is int:
            self._request_digest_detail(self._selected.get("digest", ""), offset)

    def _show_latest_digest(self) -> None:
        latest = (self._snapshot.get("digest") or {}).get("latest")
        if isinstance(latest, dict) and latest.get("id"):
            self._request_digest_detail(str(latest["id"]), 0)

    def _update_digest_detail_controls(self) -> None:
        pending = self._digest_detail_requested is not None
        self.digest_detail_previous.setEnabled(not pending and int(self._digest_display.get("digest_offset", 0)) > 0)
        self.digest_detail_next.setEnabled(not pending and self._digest_display.get("digest_next_offset") is not None)

    def _open_digest_source(self, url: QUrl) -> None:
        if url.scheme() != "alpha-guard-evidence":
            self._open_source(url)
            return
        identity = unquote(url.path())
        known = {str(entry.get("evidence_id") or "")
                 for section in self._digest_display.get("sections") or []
                 for entry in section.get("entries") or []}
        known.update(str(reference.get("evidence_id") or "")
                     for references in (self._digest_display.get("ai_summary") or {}).get("citations", {}).values()
                     for reference in references)
        if not identity or identity not in known or url.hasQuery() or url.hasFragment():
            return
        # The evidence can be older than the recent-list window; the same detail
        # reader still opens it directly from its persisted identity.
        self.evidence_table.blockSignals(True)
        self.evidence_table.clearSelection()
        for row in range(self.evidence_table.rowCount()):
            cell = self.evidence_table.item(row, 0)
            if cell is not None and cell.data(Qt.ItemDataRole.UserRole) == identity:
                self.evidence_table.selectRow(row)
                break
        self.evidence_table.blockSignals(False)
        self._select_detail("evidence", identity)
        for index in range(self.tabs.count()):
            panel = self.tabs.widget(index)
            if panel is not None and panel.isAncestorOf(self.evidence_table):
                self.tabs.setCurrentIndex(index)
                break

    @staticmethod
    def _digest_html(latest: dict[str, Any]) -> str:
        parts = [
            f"<h2>摘要 · {_escape(_time(latest.get('created_at')))}</h2>",
            f"<p>{_escape(STATUSES.get(str(latest.get('status')), '未知'))}"
            + (f" · {_escape(str(latest.get('detail')))}" if latest.get("detail") else "") + "</p>",
        ]
        counts = latest.get("counts") or {}
        parts.append(
            f"<p>窗口材料 {_escape(counts.get('evidence_window', 0))} 条 · "
            f"命中 {_escape(counts.get('matched', 0))} 条 · 分组 {_escape(counts.get('sections', 0))} 个</p>"
        )
        ai = latest.get("ai_summary") or {}
        if ai.get("enabled"):
            parts.append(f"<p>AI 原文选读：{_escape(DIGEST_AI_STATUSES.get(str(ai.get('status')), str(ai.get('status'))))}</p>")
        delivery = latest.get("delivery") or {}
        if delivery.get("enabled"):
            parts.append(f"<p>投递：{_escape(DIGEST_DELIVERY_STATUSES.get(str(delivery.get('status')), str(delivery.get('status'))))}</p>")
            for channel in delivery.get("channels") or []:
                name = {"telegram": "Telegram", "whatsapp": "WhatsApp"}.get(channel.get("channel"), "未知渠道")
                content = "已批准模板；不含摘要正文" if channel.get("content") == "static_template" else "摘要正文"
                status = DIGEST_DELIVERY_STATUSES.get(str(channel.get("status")), "结果未知")
                parts.append(f"<p>{name} · {_escape(content)} · {_escape(status)}</p>")
            if delivery.get("detail") == "whatsapp_static_template_only" and not delivery.get("channels"):
                parts.append("<p>WhatsApp 只发送模板通知，不含摘要正文。</p>")
        for section in latest.get("sections") or []:
            entries = section.get("entries") or []
            summary = (ai.get("sections") or {}).get(section.get("key"))
            total = section.get("entry_total", len(entries))
            start = section.get("entry_offset", 0)
            count_label = (f"共 {total} 条 · 当前 {start + 1}–{start + len(entries)}"
                           if total != len(entries) else f"{len(entries)} 条")
            block = [f"<h3>{_escape(section.get('label'))}（{count_label}）</h3>"]
            references = (ai.get("citations") or {}).get(section.get("key")) or []
            if references:
                for reference in references:
                    identity = quote(str(reference.get("evidence_id") or ""), safe="")
                    block.append(f'<blockquote>{_escape(reference.get("quote"))} '
                                 f'<a href="alpha-guard-evidence:{identity}">核对本地原文</a></blockquote>')
            elif isinstance(summary, str) and summary:
                block.append(f"<p>{_escape(summary)}</p>")
            block.append("<ul>")
            for entry in entries:
                identity = str(entry.get("evidence_id") or "")
                local = (f' · <a href="alpha-guard-evidence:{quote(identity, safe="")}">查看本地证据</a>'
                         if identity else "")
                block.append(f"<li>{_source_link(str(entry.get('source_url') or ''), str(entry.get('title') or '（无标题）'), _safe_url)}{local}</li>")
            block.append("</ul>")
            parts.append("".join(block))
        gaps = latest.get("coverage_gaps") or []
        if gaps:
            parts.append("<p>覆盖说明：" + "；".join(_escape(ISSUES.get(str(gap), str(gap))) for gap in gaps) + "</p>")
        return "".join(parts)

    def _send(self, command: str, fields: dict[str, Any] | None = None) -> None:
        if command in self._pending:
            return
        self._pending.add(command)
        self.message_label.setText(
            "正在提交本地请求…"
            if command != "stop"
            else "正在停止；当前有界请求结束后不再继续采集。"
        )
        self._update_controls()
        self.client.research_command(command, fields or {})

    def _action_completed(self, receipt: object) -> None:
        if not isinstance(receipt, ActionReceipt) or not receipt.action.startswith(
            "research-"
        ):
            return
        command = receipt.action.removeprefix("research-")
        self._pending.discard(command)
        if receipt.accepted:
            if command == "configure":
                self._dirty = False
            if command == "digest_save":
                self._digest_dirty = False
            if "演示" not in receipt.message:
                if command in {"once", "start", "transcribe"}:
                    self._runtime_state = "RUNNING"
                elif command == "stop" and self._runtime_state == "RUNNING":
                    self._runtime_state = "STOPPING"
            messages = {
                "configure": "研究设置已保存。",
                "subscribe": "订阅已保存，开启跟踪后检查更新。",
                "subscription": "订阅状态已更新。",
                "delete": "订阅已移除。",
                "import": "材料已导入，运行一次扫描可生成研究报告。",
                "transcribe": "已开始本地转写，可随时停止。",
                "once": "已开始本轮研究，完成后停止。",
                "start": "已开启自动跟踪。",
                "stop": "停止请求已接受。",
                "open_login": "已请求打开专用 Chrome 的平台主页。请在窗口中自行登录；尚未验证登录成功。完成后可手动扫描。",
                "model_save": "模型 profile 已保存；密钥仅存本机钥匙串。",
                "model_delete": "模型 profile 已删除。",
                "model_activate": "已更新激活的模型提供者。",
                "digest_save": "每日摘要设置已保存；开启后按设定时刻执行。",
                "digest_once": "已开始生成一轮摘要，完成后在历史中查看。",
            }
            self.message_label.setText(
                receipt.message
                if "演示" in receipt.message
                else messages.get(command, receipt.message)
            )
        else:
            self.message_label.setText(receipt.message if command == "open_login" else
                                       "研究请求未被接受，请刷新状态后重试。")
        self._update_controls()

    def _request_failed(self, purpose: str, code: str) -> None:
        if not purpose.startswith("research-"):
            return
        if purpose == "research-detail":
            if self._digest_history_requested is not None:
                self._digest_history_requested = None
                self.digest_history_label.setText("历史读取失败，保留上次结果，可刷新重试")
                self._update_digest_history_controls()
            if self._digest_detail_requested is not None:
                self._digest_detail_requested = None
                self.digest_detail_label.setText("摘要读取失败，可重新选择或重试翻页")
                self._update_digest_detail_controls()
            for kind in tuple(self._detail_pending):
                self._detail_pending.discard(kind)
                self._page_moves.pop(kind, None)
                self._update_pager(kind)
                self._page_labels[kind].setText("详情读取失败，可重读或重试翻页")
        self._pending.discard(purpose.removeprefix("research-"))
        message = {
            "request_timeout": "本地请求超时，请刷新状态后再试。",
            "guardian_unavailable": "Guardian 未连接，请先恢复本地连接。",
            "invalid_params": "输入未通过校验，或研究仍在运行。请检查设置后重试。",
            "fixture_unavailable": "离线演示中尚无真实研究详情。",
        }.get(code, "研究请求未完成，请检查输入与运行状态后重试。")
        self.message_label.setText(message)
        if purpose == "research-get":
            last_state = STATUSES.get(self._runtime_state, "未知")
            self.runtime_label.setText(
                f"状态读取失败 · 上次状态：{last_state}，尚未确认当前状态"
            )
        self._update_controls()

    def _update_controls(self) -> None:
        running = self._runtime_state in {"RUNNING", "STOPPING"}
        launching = bool(self._pending & {"start", "once", "transcribe"})
        ready = (
            self._loaded
            and not running
            and not launching
            and not self._dirty
            and not self._pending
        )
        self.once_button.setEnabled(ready)
        self.start_button.setEnabled(ready)
        self.stop_button.setEnabled(running or launching)
        self.transcribe_button.setEnabled(
            self._loaded and not running and not launching
        )
        self.save_button.setEnabled(self._loaded and not running and not self._pending)
        model_ready = self._loaded and not running and not launching
        busy_commands = {"model_save", "model_delete", "model_activate", "model_test"}
        for button in (self.model_save_button, self.model_delete_button,
                       self.model_activate_button, self.model_deactivate_button,
                       self.model_test_button):
            button.setEnabled(model_ready and not (self._pending & busy_commands))
        self.digest_save_button.setEnabled(
            self._loaded and not (self._pending & {"digest_save"}))
        self.digest_run_button.setEnabled(
            self._loaded and not running and not self._digest_dirty
            and not (self._pending & {"digest_once", "digest_save"}))
        for control in (self.digest_enabled, self.digest_time, self.digest_topics,
                        self.digest_ai, self.digest_notify, self.digest_max_items):
            control.setEnabled("digest_save" not in self._pending)
        for checkbox in (self.digest_telegram, self.digest_whatsapp):
            checkbox.setEnabled(self.digest_notify.isChecked() and "digest_save" not in self._pending)
        self.settings_content.setEnabled(
            not running and not launching and "configure" not in self._pending
        )
        capabilities = self._snapshot.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
        browser_enabled = capabilities.get("browser_enabled") is True
        dedicated = capabilities.get("browser_mode") == "dedicated"
        self.open_login_button.setEnabled(self._loaded and browser_enabled and dedicated
                                          and "open_login" not in self._pending)
        if not browser_enabled:
            self.login_hint.setText("浏览器读取尚未启用。启用 ALPHA_GUARD_BROWSER_ENABLED 后可在专用 Chrome 中登录。")
        elif not dedicated:
            self.login_hint.setText("当前使用现有浏览器模式，请在已连接的 Chrome 中自行登录。此按钮仅用于专用模式。")
        else:
            self.login_hint.setText("打开固定平台主页，由你自行登录；不会启动扫描，也不会验证登录成功。登录页会保留。")

    def set_research_snapshot(self, value: object) -> None:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("settings"), dict)
            or not isinstance(value.get("runtime"), dict)
        ):
            self._request_failed("research-get", "invalid_response")
            return
        self._snapshot = value
        self._update_capabilities(value.get("capabilities"))
        runtime = value["runtime"]
        self._runtime_state = str(runtime.get("state", "STOPPED"))
        prefix = "离线演示 · " if value.get("fixture") else ""
        state = STATUSES.get(self._runtime_state, "状态未知")
        stage = STAGES.get(str(runtime.get("stage")), "处理中")
        next_run = (
            f" · 下次检查 {_time(runtime['next_run_at'])}"
            if runtime.get("next_run_at")
            else ""
        )
        last = runtime.get("last_run")
        last_text = ""
        if isinstance(last, dict):
            last_text = f" · 最近一轮：{STATUSES.get(str(last.get('status')), '未知')}"
            if last.get("detail"):
                last_text += (
                    f"（{ISSUES.get(str(last['detail']), str(last['detail']))}）"
                )
        self.runtime_label.setText(f"{prefix}{state} · {stage}{next_run}{last_text}")
        if not self._dirty:
            self._populate_settings(value["settings"])
        self._loaded = True
        self._populate_results()
        self._populate_models()
        self._populate_digest()
        self.run_detail.setHtml(self._runs_html())
        if self._active and self._runtime_state in {"RUNNING", "STOPPING"}:
            self._timer.start()
        else:
            self._timer.stop()
        self._update_controls()

    def _update_capabilities(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        if value.get("model_provider") in {"claude_cli", "codex_cli"}:
            name = "Codex" if value["model_provider"] == "codex_cli" else "Claude"
            text = (f"已接入本机 {name} 客户端，分析时使用其现有登录并消耗相应额度；登录有效性在实际调用时检查。"
                    "仅主动扫描或跟踪时调用，可停止。" if value.get("model_installed") is True else
                    f"未找到本机 {name} 客户端。请安装并登录，或配置 Anthropic API。")
        elif value.get("model_configured") is not True:
            text = "尚未配置模型凭据。请在现有环境或 .env 中设置 ANTHROPIC_API_KEY；仅勾选 AI 开关不会配置模型。"
        elif value.get("model_installed") is not True:
            text = "模型凭据已配置，但当前安装缺少 AI 分析组件。补齐组件后才能生成分析；材料仍可保存和查看。"
        else:
            text = "模型凭据与 AI 组件已就绪。仅在你主动扫描或开启跟踪时调用模型，并受每轮调用上限约束。"
        self.ai_configuration_hint.setText(text)
        if value.get("browser_enabled") is not True:
            browser = "浏览器读取未启用；使用公开来源、授权 feed 或导入材料。"
        elif value.get("browser_mode") == "existing":
            browser = "浏览器读取使用现有 Chrome；平台需要登录时，请在已连接的浏览器中自行登录。"
        else:
            browser = "浏览器读取已启用：扫描会打开并前置 Alpha Guard 专用 Chrome 页面；可在博主订阅页打开平台登录。"
        transcription = ("本地转写已配置。" if value.get("transcription_configured") is True else
                         "本地转写尚未配置，可先导入字幕。")
        self.content_configuration_hint.setText(browser + " " + transcription)

    def _populate_settings(self, settings: dict[str, Any]) -> None:
        self._loading_settings = True
        try:
            for mapping, field in (
                (self.market_checks, "markets"),
                (self.horizon_checks, "horizons"),
                (self.source_checks, "news_sources"),
            ):
                for key, widget in mapping.items():
                    widget.setChecked(key in settings.get(field, []))
            self.instruments_input.setText(
                ", ".join(
                    f"{item['market']}:{item['ticker']}"
                    for item in settings.get("instruments", [])
                )
            )
            self.interval_input.setValue(int(settings.get("interval_minutes", 15)))
            self.ai_check.setChecked(settings.get("ai_enabled") is True)
            self.news_check.setChecked(settings.get("news_enabled") is True)
            self.max_items_input.setValue(int(settings.get("max_items", 20)))
            self.max_ai_input.setValue(int(settings.get("max_ai_calls", 10)))
            self.media_check.setChecked(settings.get("media_enabled") is True)
            self.max_media_input.setValue(int(settings.get("max_media_items", 2)))
            self.media_minutes_input.setValue(max(1, int(settings.get("max_media_seconds", 600)) // 60))
        finally:
            self._loading_settings = False

    def _populate_results(self) -> None:
        for kind, table, key in (
            ("report", self.report_table, "reports"),
            ("evidence", self.evidence_table, "evidence"),
        ):
            entries = self._snapshot.get(key, [])
            table.blockSignals(True)
            table.setRowCount(len(entries))
            selected_row = None
            for row, item in enumerate(entries):
                identity = str(item.get("id", ""))
                if kind == "report":
                    instrument = item.get("instrument", {})
                    values = [
                        f"{instrument.get('market', '')}:{instrument.get('ticker', '')}",
                        HORIZONS.get(item.get("horizon"), "未知"),
                        ACTIONS.get(item.get("action"), "待核验"),
                        STATUSES.get(item.get("status"), "未知"),
                        _time(item.get("valid_until")),
                    ]
                else:
                    values = [
                        item.get("title", "未命名材料"),
                        item.get("author") or item.get("platform", ""),
                        {
                            "full": "完整",
                            "partial": "部分内容",
                            "metadata": "仅元数据",
                        }.get(item.get("completeness"), "未知"),
                        _time(item.get("published_at")),
                    ]
                for column, value in enumerate(values):
                    cell = QTableWidgetItem(str(value))
                    cell.setData(Qt.ItemDataRole.UserRole, identity)
                    table.setItem(row, column, cell)
                if identity == self._selected.get(kind):
                    selected_row = row
            if selected_row is not None:
                table.selectRow(selected_row)
            table.blockSignals(False)
        subscriptions = self._snapshot.get("subscriptions", [])
        old = self._subscription()
        self.subscription_table.blockSignals(True)
        self.subscription_table.setRowCount(len(subscriptions))
        for row, item in enumerate(subscriptions):
            values = [
                item.get("label") or item.get("url", ""),
                PLATFORMS.get(item.get("platform"), "其他"),
                "已订阅" if item.get("enabled") else "已暂停",
                self._subscription_health(str(item["id"])),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.ItemDataRole.UserRole, item["id"])
                self.subscription_table.setItem(row, column, cell)
            if old and old.get("id") == item.get("id"):
                self.subscription_table.selectRow(row)
        self.subscription_table.blockSignals(False)
        self.subscription_status.setText(
            f"共 {len(subscriptions)} 个订阅。监控停止时不会自动获取更新。"
        )

    def _subscription_health(self, identity: str) -> str:
        last = self._snapshot.get("runtime", {}).get("last_run") or {}
        for item in last.get("sources", []):
            if item.get("subscription_id") == identity:
                detail = str(item.get("detail", ""))
                status = STATUSES.get(item.get("status"), "未知")
                return status + (f" · {ISSUES.get(detail, detail)}" if detail else "")
        return "尚未检查"

    def _runs_html(self) -> str:
        last = self._snapshot.get("runtime", {}).get("last_run") or {}
        parts = ["<h2>来源状态</h2>"]
        sources = last.get("sources", [])
        if not sources:
            parts.append("<p>当前会话尚无来源检查结果；打开本页不会发起采集。</p>")
        for source in sources:
            label = source.get("label") or (
                "新闻来源" if source.get("sources") else "订阅来源"
            )
            status = STATUSES.get(str(source.get("status")), "未知")
            parts.append(f"<h3>{_escape(label)} · {_escape(status)}</h3>")
            detail = str(source.get("detail", ""))
            if detail:
                parts.append(f"<p>{_escape(ISSUES.get(detail, detail))}</p>")
            parts.append("<ul>")
            for provider in source.get("sources", []):
                name = {
                    "finnhub": "Finnhub",
                    "newsapi": "NewsAPI",
                    "akshare": "东方财富",
                }.get(str(provider.get("provider")), "新闻来源")
                provider_status = STATUSES.get(str(provider.get("status")), "未知")
                code = str(provider.get("error_code") or "")
                reason = f" · {ISSUES.get(code, code)}" if code else ""
                parts.append(
                    f"<li>{name} · {provider_status} · {_escape(provider.get('article_count', 0))} 条{_escape(reason)}</li>"
                )
            parts.append("</ul>")
        parts.append("<h2>最近运行</h2>")
        runs = self._snapshot.get("runs", [])
        if not runs:
            parts.append("<p>暂无保存的运行记录。</p>")
        for run in runs:
            status = STATUSES.get(str(run.get("status")), "未知")
            detail = str(run.get("detail", ""))
            stats = run.get("stats") or {}
            parts.append(
                f"<p><b>{_escape(status)}</b> · {_escape(_time(run.get('finished_at')))}<br>材料 {_escape(stats.get('fetched', '—'))} · 报告 {_escape(stats.get('reports', '—'))}<br>{_escape(ISSUES.get(detail, detail))}</p>"
            )
            if "media_attempts" in stats:
                parts.append(
                    f"<p>视频读取尝试 {_escape(stats.get('media_attempts', 0))} · "
                    f"本地转写 {_escape(stats.get('transcription_calls', 0))} · "
                    f"复用已有转写 {_escape(stats.get('media_cache_hits', 0))}</p>"
                )
        return "".join(parts)

    def _selection_changed(self, kind: str, table: QTableWidget) -> None:
        item = table.item(table.currentRow(), 0)
        if item is None:
            return
        identity = str(item.data(Qt.ItemDataRole.UserRole))
        self._select_detail(kind, identity)

    def _select_detail(self, kind: str, identity: str) -> None:
        self._selected[kind] = identity
        if kind == "report":
            self.review_report_button.setEnabled(bool(identity))
            self.report_instrument_button.setEnabled(False)
        self._detail_items.pop(kind, None)
        self._page_moves.pop(kind, None)
        for field in ("content", "segment") if kind == "evidence" else ("claim",):
            self._page_history[field].clear()
        if kind == "evidence":
            self._evidence_view_initialized = False
        detail = self.report_detail if kind == "report" else self.evidence_detail
        detail.setPlainText("正在读取本地详情…")
        self._request_detail(kind, 0, 0)

    def _request_detail(self, kind: str, offset: int, segment_offset: int) -> None:
        self._detail_requests[kind] = (offset, segment_offset)
        self._detail_pending.add(kind)
        self._update_pager(kind)
        self.client.research_detail(
            kind, self._selected[kind], offset=offset, segment_offset=segment_offset
        )

    def _reload_detail(self, kind: str) -> None:
        if kind in self._detail_pending or kind not in self._selected:
            return
        item = self._detail_items.get(kind, {})
        offset = int(
            item.get("content_offset" if kind == "evidence" else "claim_offset", 0)
        )
        self._request_detail(kind, offset, int(item.get("segment_offset", 0)))

    def _turn_detail_page(self, kind: str, direction: int) -> None:
        if kind in self._detail_pending or kind not in self._detail_items:
            return
        item = self._detail_items[kind]
        field = str(self.evidence_view.currentData()) if kind == "evidence" else "claim"
        current = int(item.get(f"{field}_offset", 0))
        if direction > 0:
            destination = item.get(f"{field}_next_offset")
            if type(destination) is not int or destination <= current:
                return
        else:
            history = self._page_history[field]
            if not history:
                return
            destination = history[-1]
        offset = int(
            item.get("content_offset" if kind == "evidence" else "claim_offset", 0)
        )
        segment_offset = int(item.get("segment_offset", 0))
        if field == "segment":
            segment_offset = destination
        else:
            offset = destination
        self._page_moves[kind] = (field, direction, current)
        self._request_detail(kind, offset, segment_offset)

    def _change_evidence_view(self) -> None:
        item = self._detail_items.get("evidence")
        if item is not None:
            self.evidence_detail.setHtml(
                self._evidence_html(item, view=str(self.evidence_view.currentData()))
            )
            self._update_pager("evidence")

    def _update_pager(self, kind: str) -> None:
        item = self._detail_items.get(kind, {})
        field = str(self.evidence_view.currentData()) if kind == "evidence" else "claim"
        pending = kind in self._detail_pending
        self._reload_buttons[kind].setEnabled(not pending and kind in self._selected)
        self._previous_buttons[kind].setEnabled(
            not pending and bool(self._page_history[field])
        )
        self._next_buttons[kind].setEnabled(
            not pending and item.get(f"{field}_next_offset") is not None
        )
        if kind == "evidence":
            self.evidence_view.setEnabled(not pending)
        if pending:
            self._page_labels[kind].setText("正在读取本地片段…")
            return
        if not item:
            self._page_labels[kind].setText("选择一项查看详情")
            return
        offset = int(item.get(f"{field}_offset", 0))
        if field == "content":
            count = len(item.get("content_text", ""))
            label = (
                f"正文第 {offset + 1}–{offset + count} 字" if count else "当前没有正文"
            )
        else:
            count = (
                len(item.get("segments", []))
                if field == "segment"
                else max(
                    len(item.get(key, []))
                    for key in (
                        "claims", "catalysts", "counter_evidence", "conditions",
                        "invalidation", "verification_steps",
                    )
                )
            )
            unit = "字幕" if field == "segment" else "论据"
            label = (
                f"{unit}第 {offset + 1}–{offset + count} 项"
                if count
                else f"当前没有{unit}"
            )
        suffix = (
            " · 还有后续内容"
            if item.get(f"{field}_next_offset") is not None
            else " · 已到末段"
        )
        self._page_labels[kind].setText(label + (suffix if count else ""))

    def set_detail(self, payload: object) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("item"), dict):
            return
        kind = payload.get("kind")
        if kind == "digest_history":
            item = payload["item"]
            offset = item.get("offset")
            records = item.get("items")
            if (payload.get("id") != "history" or type(offset) is not int
                    or offset != self._digest_history_requested or not isinstance(records, list)
                    or any(not isinstance(record, dict) for record in records)):
                return
            self._digest_history_requested = None
            self._digest_history_offset = offset
            self._digest_history_page = item
            self._render_digest_history(records)
            self.digest_history_label.setText(
                f"历史第 {offset + 1}–{offset + len(records)} 条" if records else "没有更多历史摘要")
            self._update_digest_history_controls()
            return
        if self._selected.get(str(kind)) != payload.get("id"):
            return
        item = payload["item"]
        if kind == "digest":
            offset = item.get("digest_offset", 0)
            if self._digest_detail_expected is not None and self._digest_detail_expected != (payload["id"], offset):
                return
            self._digest_detail_requested = None
            self._digest_display = item
            self.digest_detail.setHtml(self._digest_html(item))
            total = item.get("digest_total")
            self.digest_detail_label.setText(
                f"摘要条目 {offset + 1}–{min(offset + 20, total)} / {total}"
                if type(total) is int and total else "完整摘要")
            self._update_digest_detail_controls()
            return
        if kind not in {"evidence", "report"}:
            return
        returned_offset = int(
            item.get("content_offset" if kind == "evidence" else "claim_offset", 0)
        )
        returned_segment = int(item.get("segment_offset", 0))
        if self._detail_requests.get(kind, (0, 0)) != (
            returned_offset,
            returned_segment,
        ):
            return
        move = self._page_moves.pop(kind, None)
        if move is not None:
            field, direction, previous = move
            if direction > 0:
                self._page_history[field].append(previous)
            else:
                self._page_history[field].pop()
        self._detail_pending.discard(kind)
        self._detail_items[kind] = item
        if kind == "report":
            self.report_instrument_button.setEnabled(bool(self._report_instrument()))
            self.report_detail.setHtml(self._report_html(item))
        elif kind == "evidence":
            if not self._evidence_view_initialized:
                self._evidence_view_initialized = True
                self.evidence_view.setCurrentIndex(1 if item.get("segments") else 0)
            self.evidence_detail.setHtml(
                self._evidence_html(item, view=str(self.evidence_view.currentData()))
            )
        self._update_pager(kind)

    def _report_instrument(self) -> str:
        instrument = self._detail_items.get("report", {}).get("instrument") or {}
        market, ticker = instrument.get("market"), instrument.get("ticker")
        return f"{market}:{ticker}" if market in {"US", "HK", "CN"} and isinstance(ticker, str) and ticker else ""

    def _open_report_instrument(self) -> None:
        key = self._report_instrument()
        if key:
            self.instrument_requested.emit(key)

    @staticmethod
    def _report_html(item: dict[str, Any]) -> str:
        instrument = item.get("instrument", {})
        title = f"{instrument.get('market', '')}:{instrument.get('ticker', '')} · {HORIZONS.get(str(item.get('horizon')), '未知周期')}"
        parts = [
            f"<h2>{_escape(title)}</h2>",
            f"<p><b>{_escape(ACTIONS.get(str(item.get('action')), '待核验'))}</b> · {_escape(STATUSES.get(str(item.get('status')), '未知'))}</p>",
            f"<p>{_escape(item.get('summary', ''))}</p>",
            f"<p>{_escape(item.get('thesis', ''))}</p>",
            f"<p>生成：{_escape(_time(item.get('created_at')))}<br>有效至：{_escape(_time(item.get('valid_until')))}<br>参考价格：{_escape(item.get('reference_price'))}</p>",
        ]
        for key, title in (
            ("conditions", "行动条件"),
            ("catalysts", "催化因素"),
            ("counter_evidence", "反向证据"),
            ("invalidation", "失效条件"),
            ("verification_steps", "下一步核验"),
            ("quality_issues", "证据限制"),
        ):
            values = item.get(key) or []
            if values:
                parts.append(
                    f"<h3>{title}</h3><ul>"
                    + "".join(
                        f"<li>{_escape(ISSUES.get(str(value), str(value)))}</li>"
                        for value in values
                    )
                    + "</ul>"
                )
        parts.append("<h3>原始证据</h3>")
        for claim in item.get("claims", []):
            citation = claim.get("citation", {})
            kind = {
                "fact": "来源陈述的事实（待核验）",
                "opinion": "作者观点",
                "inference": "模型推断",
            }.get(claim.get("kind"), "待分类")
            parts.append(
                f"<p><b>{kind}</b>：{_escape(claim.get('text', ''))}<br>关联：{_escape(claim.get('relationship', ''))}</p>"
            )
            parts.append(
                f"<blockquote>{_escape(citation.get('quote', ''))}</blockquote>"
            )
            timestamp = citation.get("start_seconds")
            reference = f" · {_escape(timestamp)} 秒" if timestamp is not None else ""
            speaker = (
                f" · {_escape(citation['speaker'])}" if citation.get("speaker") else ""
            )
            parts.append(
                f"<p>{_source_link(citation.get('source_url'), start_seconds=timestamp)}{reference}{speaker}</p>"
            )
        if not item.get("claims"):
            parts.append("<p>本组没有引文。</p>")
        context = item.get("market_context") or {}
        parts.append(
            f"<p>行情来源时间：{_escape(_time(context.get('source_as_of')))}<br>独立内容组：{_escape(item.get('independent_source_count', 1))}，不代表已独立证实。</p>"
        )
        parts.append(generation_html(item.get("generation_context")))
        return "".join(parts)

    @staticmethod
    def _evidence_html(item: dict[str, Any], *, view: str = "content") -> str:
        parts = [
            f"<h2>{_escape(item.get('title', ''))}</h2>",
            f"<p>{_escape(item.get('author', ''))} · {_escape(item.get('platform', ''))}<br>发布时间：{_escape(_time(item.get('published_at')))}<br>获取时间：{_escape(_time(item.get('retrieved_at')))}</p>",
            f"<p>{_source_link(item.get('source_url'))}</p>",
        ]
        issues = item.get("quality_issues") or []
        if issues:
            parts.append(
                "<p><b>材料限制：</b>"
                + "；".join(
                    _escape(ISSUES.get(str(value), str(value))) for value in issues
                )
                + "</p>"
            )
        if item.get("summary"):
            parts.append(f"<h3>摘要</h3><p>{_escape(item['summary'])}</p>")
        segments = item.get("segments") or []
        if view == "segment" and segments:
            parts.append("<h3>访谈内容</h3>")
            for segment in segments:
                stamp = str(segment.get("start_seconds", "?"))
                speaker = f" · {segment['speaker']}" if segment.get("speaker") else ""
                parts.append(
                    f"<p><b>{_escape(stamp)} 秒{_escape(speaker)}</b><br>{_escape(segment.get('text', ''))}</p>"
                )
        elif view == "content" and item.get("content_text"):
            parts.append(
                "<h3>原文内容</h3><p>"
                + _escape(item["content_text"]).replace("\n", "<br>")
                + "</p>"
            )
        elif view == "segment":
            parts.append(
                "<p>当前没有带时间点的字幕内容，可以切换原文正文或补充字幕。</p>"
            )
        else:
            parts.append("<p>当前仅有标题或摘要，仍需完整正文或带时间点的字幕。</p>")
        return "".join(parts)

    def _open_source(self, url: QUrl) -> None:
        if _safe_url(url.toString()):
            QDesktopServices.openUrl(url)
        else:
            self.message_label.setText("来源链接格式不受支持。")
