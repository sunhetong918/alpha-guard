"""Main desktop shell: compact rail, duty header, and five operational views."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .client import GuardianClient
from .formatters import format_time, state_label
from .models import ActionReceipt, ChannelKind, DashboardSnapshot, StatusColor
from .pages import (
    AssetsPage,
    IncidentsPage,
    OverviewPage,
    ProvidersPage,
    SettingsPage,
)
from .widgets import StatusBadge


NAV_ITEMS = (
    ("overview", "◎", "值班摘要"),
    ("assets", "◇", "标的责任"),
    ("incidents", "!", "事故记录"),
    ("providers", "≋", "外部能力"),
    ("settings", "⚙", "设置与通道"),
)


SAFE_ERROR_MESSAGES = {
    "request_timeout": "Guardian 请求超时",
    "guardian_disconnected": "Guardian 连接已断开",
    "connection_refusederror": "Guardian 未启动或拒绝连接",
    "servernotfounderror": "找不到 Guardian 本地端点",
    "invalid_response": "Guardian 返回了无效回执",
    "incomplete_response": "Guardian 回执不完整",
    "response_too_large": "Guardian 回执超过安全大小限制",
    "unsupported_channel": "该通道不支持从桌面发起测试",
    "fixture_unavailable": "离线 fixture 无法读取",
}


class Sidebar(QFrame):
    page_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(218)
        self._compact = False
        root = QVBoxLayout(self)
        root.setContentsMargins(13, 18, 13, 14)
        root.setSpacing(5)
        brand = QLabel("ALPHA\nGUARD")
        brand.setObjectName("brandMark")
        brand.setAccessibleName("Alpha Guard")
        self.brand = brand
        self.brand_sub = QLabel("TRUSTED SILENCE / LOCAL")
        self.brand_sub.setObjectName("brandSub")
        root.addWidget(brand)
        root.addWidget(self.brand_sub)
        root.addSpacing(22)
        self.buttons = QButtonGroup(self)
        self.buttons.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        for index, (key, glyph, title) in enumerate(NAV_ITEMS):
            button = QPushButton(f"{glyph}   {title}")
            button.setCheckable(True)
            button.setProperty("nav", True)
            button.setProperty("pageKey", key)
            button.setAccessibleName(title)
            button.setToolTip(title)
            button.clicked.connect(
                lambda checked=False, current=key: self.page_requested.emit(current)
            )
            self.buttons.addButton(button, index)
            self.nav_buttons[key] = button
            root.addWidget(button)
        self.nav_buttons["overview"].setChecked(True)
        root.addStretch(1)
        local = QFrame()
        local.setObjectName("noticePanel")
        local_layout = QVBoxLayout(local)
        local_layout.setContentsMargins(10, 9, 10, 9)
        self.local_title = QLabel("LOCAL ONLY")
        self.local_title.setObjectName("eyebrow")
        self.local_detail = QLabel("无券商连接\n无交易能力")
        self.local_detail.setObjectName("helpText")
        self.local_detail.setWordWrap(True)
        local_layout.addWidget(self.local_title)
        local_layout.addWidget(self.local_detail)
        root.addWidget(local)

    def set_compact(self, compact: bool) -> None:
        if self._compact == compact:
            return
        self._compact = compact
        self.setFixedWidth(78 if compact else 218)
        self.brand.setText("AG" if compact else "ALPHA\nGUARD")
        self.brand_sub.setVisible(not compact)
        self.local_detail.setVisible(not compact)
        self.local_title.setText("LCL" if compact else "LOCAL ONLY")
        for key, glyph, title in NAV_ITEMS:
            self.nav_buttons[key].setText(glyph if compact else f"{glyph}   {title}")
            self.nav_buttons[key].setStyleSheet(
                "text-align: center;" if compact else ""
            )


class MainWindow(QMainWindow):
    def __init__(
        self,
        client: GuardianClient,
        *,
        auto_refresh: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.snapshot: DashboardSnapshot | None = None
        self._page_keys = [item[0] for item in NAV_ITEMS]
        self.setObjectName("alphaGuardMainWindow")
        self.setWindowTitle("Alpha Guard · 可信沉默值班台")
        self.resize(1360, 860)
        self.setMinimumSize(960, 640)

        root_widget = QWidget()
        root_widget.setObjectName("appRoot")
        root = QHBoxLayout(root_widget)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.sidebar = Sidebar()
        self.sidebar.page_requested.connect(self.show_page)
        root.addWidget(self.sidebar)

        stage = QWidget()
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(0)
        stage_layout.addWidget(self._build_topbar())
        self.stack = QStackedWidget()
        self.stack.setObjectName("pageStack")
        self.overview_page = OverviewPage()
        self.assets_page = AssetsPage()
        self.incidents_page = IncidentsPage()
        self.providers_page = ProvidersPage()
        self.settings_page = SettingsPage()
        self.pages = (
            self.overview_page,
            self.assets_page,
            self.incidents_page,
            self.providers_page,
            self.settings_page,
        )
        for page in self.pages:
            self.stack.addWidget(page)
        stage_layout.addWidget(self.stack, 1)
        stage_layout.addWidget(self._build_safety_footer())
        root.addWidget(stage, 1)
        self.setCentralWidget(root_widget)

        status_bar = QStatusBar()
        status_bar.setSizeGripEnabled(False)
        self.setStatusBar(status_bar)
        status_bar.showMessage("正在等待 Guardian 回执…")

        self._connect_client()
        self.settings_page.test_channel_requested.connect(self._test_channel)
        self.settings_page.save_requested.connect(self._save_preferences)
        QShortcut(QKeySequence("Ctrl+R"), self, self.client.refresh)
        QShortcut(QKeySequence("Ctrl+Shift+R"), self, self._request_scan)
        QShortcut(QKeySequence("Meta+1"), self, lambda: self.show_page("overview"))
        QShortcut(QKeySequence("Meta+2"), self, lambda: self.show_page("assets"))
        QShortcut(QKeySequence("Meta+3"), self, lambda: self.show_page("incidents"))
        if auto_refresh:
            QTimer.singleShot(0, self.client.refresh)

    def _build_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(67)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(12)
        duty = QVBoxLayout()
        duty.setSpacing(2)
        duty_code = QLabel("DUTY CONSOLE / 本地值班")
        duty_code.setObjectName("eyebrow")
        self.connection_label = QLabel("等待本地 Guardian")
        self.connection_label.setObjectName("helpText")
        duty.addWidget(duty_code)
        duty.addWidget(self.connection_label)
        layout.addLayout(duty)
        layout.addSpacing(12)
        self.overall_badge = StatusBadge(StatusColor.GRAY, "等待回执")
        self.guardian_badge = StatusBadge(StatusColor.GRAY, "Guardian 未连接")
        layout.addWidget(self.overall_badge)
        layout.addWidget(self.guardian_badge)
        layout.addStretch(1)
        self.heartbeat_label = QLabel("HEARTBEAT —")
        self.heartbeat_label.setObjectName("mono")
        layout.addWidget(self.heartbeat_label)
        self.refresh_button = QPushButton("刷新回执")
        self.refresh_button.setObjectName("refreshButton")
        self.refresh_button.setProperty("quiet", True)
        self.refresh_button.clicked.connect(self.client.refresh)
        self.scan_button = QPushButton("运行一次扫描")
        self.scan_button.setObjectName("scanButton")
        self.scan_button.setProperty("primary", True)
        self.scan_button.clicked.connect(self._request_scan)
        layout.addWidget(self.refresh_button)
        layout.addWidget(self.scan_button)
        return bar

    def _build_safety_footer(self) -> QFrame:
        footer = QFrame()
        footer.setObjectName("safetyFooter")
        footer.setFixedHeight(37)
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(14)
        guardrail = QLabel("READ-ONLY DECISION SUPPORT")
        guardrail.setObjectName("eyebrow")
        copy = QLabel("仅供人工核验 · 不构成投资建议 · 不连接券商 · 不执行交易")
        copy.setObjectName("helpText")
        layout.addWidget(guardrail)
        layout.addWidget(copy)
        layout.addStretch(1)
        self.source_label = QLabel("SOURCE / —")
        self.source_label.setObjectName("eyebrow")
        layout.addWidget(self.source_label)
        return footer

    def _connect_client(self) -> None:
        self.client.snapshot_ready.connect(self.set_snapshot)
        self.client.request_failed.connect(self._show_error)
        self.client.busy_changed.connect(self._set_busy)
        self.client.action_completed.connect(self._show_action)
        self.client.configuration_saved.connect(self._show_configuration_saved)
        self.client.connection_state_changed.connect(self._connection_changed)

    def show_page(self, key: str) -> None:
        try:
            index = self._page_keys.index(key)
        except ValueError:
            return
        self.stack.setCurrentIndex(index)
        button = self.sidebar.nav_buttons[key]
        button.setChecked(True)

    def set_snapshot(self, snapshot: DashboardSnapshot) -> None:
        self.snapshot = snapshot
        self.overall_badge.set_status(
            snapshot.cockpit.overall_color,
            f"可信沉默：{state_label(snapshot.cockpit.state)}",
        )
        self.guardian_badge.set_status(
            snapshot.health.color,
            f"Guardian {state_label(snapshot.health.state.value)}",
        )
        self.heartbeat_label.setText(
            "HEARTBEAT "
            + format_time(
                snapshot.health.last_heartbeat_at,
                snapshot.preferences.timezone,
                include_date=False,
            )
        )
        self.source_label.setText(f"SOURCE / {snapshot.health.source.upper()}")
        self.connection_label.setText(
            f"Guardian {snapshot.health.version} · PID {snapshot.health.pid or '—'}"
        )
        for page in self.pages:
            page.set_snapshot(snapshot)
        self.statusBar().showMessage(
            f"已载入回执 {snapshot.cockpit.receipt_id} · "
            f"{format_time(snapshot.cockpit.generated_at, snapshot.preferences.timezone)}",
            5_000,
        )

    def _request_scan(self) -> None:
        self.client.request_scan()
        self.statusBar().showMessage("已请求 Guardian 运行一次扫描…")

    def _test_channel(self, channel: object) -> None:
        if isinstance(channel, ChannelKind):
            self.client.test_channel(channel)
            self.statusBar().showMessage(f"正在请求测试 {channel.value}…")

    def _save_preferences(self, preferences: object, revision: int) -> None:
        from .models import Preferences

        if isinstance(preferences, Preferences):
            self.client.update_preferences(preferences, revision=revision)
            self.statusBar().showMessage("正在由 Guardian 保存公开偏好…")

    def _set_busy(self, busy: bool) -> None:
        self.refresh_button.setEnabled(not busy)
        self.scan_button.setEnabled(not busy)
        self.settings_page.set_busy(busy)
        if busy:
            self.connection_label.setText("Guardian 正在处理请求…")

    def _connection_changed(self, state: str) -> None:
        labels = {
            "fixture": "离线 fixture · 不访问网络",
            "connecting": "正在连接本地 Guardian…",
            "starting": "Guardian 未运行，正在启动并重连…",
            "connected": "Guardian 本地通道已连接",
            "disconnected": "Guardian 本地通道已断开",
            "error": "Guardian 本地通道异常",
        }
        self.connection_label.setText(labels.get(state, state))

    def _show_error(self, purpose: str, code: str) -> None:
        suffix = code.split(":", 1)[-1]
        message = SAFE_ERROR_MESSAGES.get(suffix, "Guardian 请求未完成")
        self.statusBar().showMessage(f"{message} · {purpose} · {suffix}", 10_000)
        if purpose in {"refresh", "transport"}:
            self.guardian_badge.set_status(StatusColor.RED, "Guardian 不可用")

    def _show_action(self, receipt: object) -> None:
        if not isinstance(receipt, ActionReceipt):
            return
        state = "已接受" if receipt.accepted else "已拒绝"
        self.statusBar().showMessage(
            f"{state} · {receipt.message} · {receipt.request_id}", 8_000
        )
        QTimer.singleShot(250, self.client.refresh)

    def _show_configuration_saved(self, _payload: object) -> None:
        self.statusBar().showMessage("公开偏好已由 Guardian 保存", 6_000)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        compact_rail = event.size().width() < 1_120
        compact_overview = event.size().width() < 1_080
        self.sidebar.set_compact(compact_rail)
        self.overview_page.set_compact(compact_overview)
        self.guardian_badge.setVisible(not compact_rail)
        self.heartbeat_label.setVisible(event.size().width() >= 1_040)
        super().resizeEvent(event)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            self.statusBar().showMessage("Alpha Guard 继续由 Guardian 在后台值班")
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.client.close()
        super().closeEvent(event)
