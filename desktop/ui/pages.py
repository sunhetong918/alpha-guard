"""The five read-only operational views of the Alpha Guard desktop app."""

from __future__ import annotations

import sys
from collections.abc import Iterable

from PySide6.QtCore import QTime, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from .formatters import (
    CIRCUIT_LABELS,
    COLOR_STATE_LABELS,
    DEADLINE_COLORS,
    DEADLINE_LABELS,
    DECISION_COLORS,
    DECISION_LABELS,
    GRADE_LABELS,
    INCIDENT_LABELS,
    format_percent,
    format_time,
    reason_label,
    state_label,
)
from .models import (
    AssetStatus,
    ChannelKind,
    DashboardSnapshot,
    DeliveryChannel,
    Incident,
    IncidentState,
    Preferences,
    ProviderCapability,
    StatusColor,
)
from .widgets import (
    Meter,
    PageHeading,
    QuestionPanel,
    StateLegend,
    StatusBadge,
    clear_layout,
    text_lines,
)


def _scroll_page(content: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(content)
    return scroll


def _table_item(text: str, *, mono: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if mono:
        if sys.platform == "win32":
            family = "Cascadia Mono"
        elif sys.platform.startswith("linux"):
            family = "DejaVu Sans Mono"
        else:
            family = "Menlo"
        font = QFont(family)
        font.setStyleHint(QFont.StyleHint.Monospace)
        item.setFont(font)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _badge_cell(color: StatusColor, text: str) -> QWidget:
    wrapper = QWidget()
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(6, 5, 6, 5)
    layout.addWidget(StatusBadge(color, text))
    layout.addStretch(1)
    return wrapper


def _segment(label: str) -> QPushButton:
    button = QPushButton(label)
    button.setCheckable(True)
    button.setProperty("segment", True)
    return button


class OverviewPage(QWidget):
    """The receipt-first answer to the Reliability Cockpit's four questions."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("overviewPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.content = QWidget()
        layout = QVBoxLayout(self.content)
        layout.setContentsMargins(26, 24, 26, 30)
        layout.setSpacing(18)
        layout.addWidget(
            PageHeading(
                "01 / OVERVIEW",
                "可信沉默值班台",
                "先回答系统是否可靠履责，再看任何市场方向。绿色只代表扫描责任健康，不代表资产安全。",
            )
        )

        receipt = QFrame()
        receipt.setObjectName("receiptStrip")
        receipt_layout = QHBoxLayout(receipt)
        receipt_layout.setContentsMargins(14, 9, 14, 9)
        receipt_layout.setSpacing(18)
        self.receipt_id = QLabel("回执 —")
        self.receipt_id.setObjectName("receiptId")
        self.generated_at = QLabel("生成 —")
        self.generated_at.setObjectName("mono")
        self.delivery_mode = QLabel("PREVIEW")
        self.delivery_mode.setObjectName("eyebrow")
        receipt_layout.addWidget(self.receipt_id)
        receipt_layout.addWidget(self.generated_at)
        receipt_layout.addStretch(1)
        receipt_layout.addWidget(self.delivery_mode)
        receipt_layout.addWidget(StatusBadge(StatusColor.GRAY, "只读 · 不执行交易"))
        layout.addWidget(receipt)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(14)
        self.scan_panel = QuestionPanel(
            "Q1",
            "该跑的扫描跑了吗？",
            "按市场核对最近预期 full scan、deadline 与 30 天窗口，不用进程存活代替完成证据。",
        )
        self.scan_panel.setObjectName("questionPanel")
        self.silence_panel = QuestionPanel(
            "Q2",
            "当前沉默可信吗？",
            "可信沉默要求完整覆盖、新鲜字段、可信账本和可用送达链路同时成立。",
        )
        self.provider_panel = QuestionPanel(
            "Q3",
            "哪个外部能力正在退化？",
            "能力按 provider × operation × market 隔离；缓存命中不会制造健康样本。",
        )
        self.channel_panel = QuestionPanel(
            "Q4",
            "提醒与 Guardian 可用吗？",
            "Telegram、WhatsApp 和外部 Watcher 分开举证；通道失败会让可信沉默 fail closed。",
        )
        self.panels = (
            self.scan_panel,
            self.silence_panel,
            self.provider_panel,
            self.channel_panel,
        )
        for panel in self.panels:
            panel.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
        self.grid.addWidget(self.scan_panel, 0, 0)
        self.grid.addWidget(self.silence_panel, 0, 1)
        self.grid.addWidget(self.provider_panel, 1, 0)
        self.grid.addWidget(self.channel_panel, 1, 1)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        layout.addLayout(self.grid)

        self._build_silence_body()
        layout.addWidget(StateLegend())
        layout.addStretch(1)
        root.addWidget(_scroll_page(self.content))
        self._compact = False
        self._timezone = "Asia/Shanghai"

    def _build_silence_body(self) -> None:
        row = QHBoxLayout()
        self.silence_badge = StatusBadge(StatusColor.GRAY, "等待回执")
        self.silence_state = QLabel("—")
        self.silence_state.setObjectName("metricValue")
        row.addWidget(self.silence_badge)
        row.addStretch(1)
        row.addWidget(self.silence_state)
        self.silence_panel.body.addLayout(row)
        self.fresh_meter = Meter("字段新鲜覆盖")
        self.trusted_meter = Meter("可信决策覆盖")
        self.silence_panel.body.addWidget(self.fresh_meter)
        self.silence_panel.body.addWidget(self.trusted_meter)
        self.affected = QLabel("受影响标的：—")
        self.affected.setObjectName("helpText")
        self.affected.setWordWrap(True)
        self.silence_panel.body.addWidget(self.affected)

    def set_compact(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for panel in self.panels:
            self.grid.removeWidget(panel)
        if compact:
            for row, panel in enumerate(self.panels):
                self.grid.addWidget(panel, row, 0)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 0)
        else:
            self.grid.addWidget(self.scan_panel, 0, 0)
            self.grid.addWidget(self.silence_panel, 0, 1)
            self.grid.addWidget(self.provider_panel, 1, 0)
            self.grid.addWidget(self.channel_panel, 1, 1)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1)

    def set_snapshot(self, snapshot: DashboardSnapshot) -> None:
        self._timezone = snapshot.preferences.timezone
        receipt = snapshot.cockpit
        self.receipt_id.setText(f"回执  {receipt.receipt_id}")
        self.generated_at.setText(
            f"生成  {format_time(receipt.generated_at, self._timezone)}"
        )
        self.delivery_mode.setText(f"DELIVERY / {receipt.delivery_mode}")
        self._update_scan(snapshot)
        self._update_silence(snapshot)
        self._update_providers(snapshot.providers)
        self._update_channels(snapshot)

    def _update_scan(self, snapshot: DashboardSnapshot) -> None:
        clear_layout(self.scan_panel.body)
        markets = snapshot.cockpit.markets
        completed = sum(item.deadline_state == "completed" for item in markets)
        color = (
            StatusColor.GREEN
            if completed == len(markets) and markets
            else snapshot.cockpit.overall_color
        )
        summary = QHBoxLayout()
        summary.addWidget(
            StatusBadge(color, f"最近窗口 {completed}/{len(markets)} 完成")
        )
        summary.addStretch(1)
        self.scan_panel.body.addLayout(summary)
        if not markets:
            empty = QLabel("没有启用的扫描责任。GRAY 不等于健康。")
            empty.setObjectName("helpText")
            self.scan_panel.body.addWidget(empty)
            return
        for market in markets:
            row_widget = QWidget()
            row = QGridLayout(row_widget)
            row.setContentsMargins(0, 2, 0, 4)
            market_label = QLabel(market.market)
            market_label.setObjectName("metricValue")
            market_label.setStyleSheet("font-size: 18px;")
            deadline_color = DEADLINE_COLORS.get(
                market.deadline_state, StatusColor.GRAY
            )
            deadline = StatusBadge(
                deadline_color,
                DEADLINE_LABELS.get(market.deadline_state, market.deadline_state),
            )
            detail = QLabel(
                "预期 "
                f"{format_time(market.expected_at, self._timezone)}  ·  "
                f"截止 {format_time(market.deadline_at, self._timezone)}  ·  "
                f"30天 {market.slo_30d.good}/{market.slo_30d.expected}"
            )
            detail.setObjectName("helpText")
            detail.setWordWrap(True)
            row.addWidget(market_label, 0, 0)
            row.addWidget(deadline, 0, 1, Qt.AlignmentFlag.AlignRight)
            row.addWidget(detail, 1, 0, 1, 2)
            self.scan_panel.body.addWidget(row_widget)

    def _update_silence(self, snapshot: DashboardSnapshot) -> None:
        receipt = snapshot.cockpit
        self.silence_badge.set_status(
            receipt.silence_color, state_label(receipt.silence_state)
        )
        self.silence_state.setText(state_label(receipt.silence_state))
        self.fresh_meter.set_value(
            receipt.fresh_data.ratio,
            StatusColor.GREEN
            if receipt.fresh_data.ratio == 1
            else StatusColor.AMBER,
        )
        self.trusted_meter.set_value(
            receipt.trusted_decision.ratio, receipt.silence_color
        )
        affected = receipt.trusted_decision.affected
        self.affected.setText(
            "受影响标的：" + ("、".join(affected) if affected else "无")
        )

    def _update_providers(
        self, capabilities: Iterable[ProviderCapability]
    ) -> None:
        clear_layout(self.provider_panel.body)
        ordered = sorted(
            capabilities,
            key=lambda item: (
                item.color not in {StatusColor.RED, StatusColor.AMBER},
                item.provider,
            ),
        )
        attention = sum(
            item.color in {StatusColor.RED, StatusColor.AMBER} for item in ordered
        )
        self.provider_panel.body.addWidget(
            StatusBadge(
                StatusColor.AMBER if attention else StatusColor.GREEN,
                f"{attention} 项需要关注" if attention else "能力均正常",
            )
        )
        for item in ordered[:3]:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 2, 0, 2)
            identity = QLabel(
                f"{item.provider}  ×  {item.operation}  ×  {item.market}"
            )
            identity.setObjectName("mono")
            identity.setWordWrap(True)
            grade = GRADE_LABELS.get(item.grade, item.grade)
            row_layout.addWidget(identity, 1)
            row_layout.addWidget(StatusBadge(item.color, grade))
            self.provider_panel.body.addWidget(row)
        if len(ordered) > 3:
            more = QLabel(f"另有 {len(ordered) - 3} 项能力，请到 Providers 查看。")
            more.setObjectName("helpText")
            self.provider_panel.body.addWidget(more)

    def _update_channels(self, snapshot: DashboardSnapshot) -> None:
        clear_layout(self.channel_panel.body)
        guardian_row = QWidget()
        guardian_layout = QHBoxLayout(guardian_row)
        guardian_layout.setContentsMargins(0, 0, 0, 0)
        guardian_layout.addWidget(QLabel("后台 Guardian"))
        guardian_layout.addStretch(1)
        guardian_layout.addWidget(
            StatusBadge(
                snapshot.health.color, state_label(snapshot.health.state.value)
            )
        )
        self.channel_panel.body.addWidget(guardian_row)
        for channel in snapshot.channels:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 2, 0, 2)
            label = QLabel(channel.label)
            detail = QLabel(
                "最近成功 "
                + format_time(
                    channel.last_success_at,
                    snapshot.preferences.timezone,
                    include_date=False,
                )
            )
            detail.setObjectName("helpText")
            row_layout.addWidget(label)
            row_layout.addStretch(1)
            row_layout.addWidget(detail)
            row_layout.addWidget(
                StatusBadge(
                    channel.color,
                    "可用"
                    if channel.color is StatusColor.GREEN
                    else "未证明可用",
                )
            )
            self.channel_panel.body.addWidget(row)


class AssetsPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("assetsPage")
        self._assets: tuple[AssetStatus, ...] = ()
        self._timezone = "Asia/Shanghai"
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 24)
        root.setSpacing(16)
        root.addWidget(
            PageHeading(
                "02 / ASSETS",
                "标的责任范围",
                "这里展示证据覆盖与人工核验类别，不展示盘口，也不提供下单入口。",
            )
        )
        controls = QHBoxLayout()
        self.filters = QButtonGroup(self)
        self.filters.setExclusive(True)
        for key, label in (
            ("ALL", "全部"),
            ("US", "美股"),
            ("HK", "港股"),
            ("ATTENTION", "需关注"),
        ):
            button = _segment(label)
            button.setProperty("filterKey", key)
            button.clicked.connect(self._apply_filter)
            self.filters.addButton(button)
            controls.addWidget(button)
            if key == "ALL":
                button.setChecked(True)
        controls.addSpacing(10)
        self.search = QLineEdit()
        self.search.setObjectName("assetSearch")
        self.search.setPlaceholderText("搜索代码或名称")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        controls.addWidget(self.search, 1)
        self.count = QLabel("0 项责任")
        self.count.setObjectName("mono")
        controls.addWidget(self.count)
        root.addLayout(controls)

        self.table = QTableWidget(0, 8)
        self.table.setObjectName("assetsTable")
        self.table.setHorizontalHeaderLabels(
            ["保护状态", "标的", "市场", "证据覆盖", "人工核验", "观测时间", "下次扫描", "原因"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 128)
        self.table.setColumnWidth(4, 238)
        self.table.setColumnWidth(1, 150)
        root.addWidget(self.table, 1)

        notice = QFrame()
        notice.setObjectName("noticePanel")
        notice_layout = QHBoxLayout(notice)
        notice_layout.setContentsMargins(13, 9, 13, 9)
        note = QLabel(
            "BUY_REVIEW / SELL_REVIEW 只是用户规则触发后的人工核验类别，不是买卖指令。UNKNOWN 不会被当作未触发。"
        )
        note.setWordWrap(True)
        note.setObjectName("helpText")
        notice_layout.addWidget(note)
        root.addWidget(notice)

    def set_snapshot(self, snapshot: DashboardSnapshot) -> None:
        self._assets = snapshot.assets
        self._timezone = snapshot.preferences.timezone
        self._apply_filter()

    def _current_filter(self) -> str:
        checked = self.filters.checkedButton()
        if checked is None:
            return "ALL"
        return str(checked.property("filterKey"))

    def _apply_filter(self, *_args: object) -> None:
        mode = self._current_filter()
        query = self.search.text().strip().casefold()
        visible = []
        for asset in self._assets:
            if mode in {"US", "HK"} and asset.market != mode:
                continue
            if mode == "ATTENTION" and asset.color not in {
                StatusColor.AMBER,
                StatusColor.RED,
                StatusColor.BLUE,
            }:
                continue
            if query and query not in f"{asset.symbol} {asset.name}".casefold():
                continue
            visible.append(asset)
        self.count.setText(f"{len(visible)} / {len(self._assets)} 项责任")
        self.table.setRowCount(len(visible))
        for row, asset in enumerate(visible):
            self.table.setRowHeight(row, 55)
            self.table.setCellWidget(
                row, 0, _badge_cell(asset.color, COLOR_STATE_LABELS[asset.color])
            )
            identity = f"{asset.symbol}\n{asset.name}"
            self.table.setItem(row, 1, _table_item(identity, mono=True))
            self.table.setItem(row, 2, _table_item(asset.market, mono=True))
            self.table.setItem(
                row, 3, _table_item(format_percent(asset.evidence_coverage))
            )
            decision_color = DECISION_COLORS[asset.decision]
            self.table.setCellWidget(
                row,
                4,
                _badge_cell(decision_color, DECISION_LABELS[asset.decision]),
            )
            self.table.setItem(
                row,
                5,
                _table_item(format_time(asset.observed_at, self._timezone), mono=True),
            )
            self.table.setItem(
                row,
                6,
                _table_item(format_time(asset.next_scan_at, self._timezone), mono=True),
            )
            self.table.setItem(row, 7, _table_item(asset.reason))


class IncidentsPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("incidentsPage")
        self._incidents: tuple[Incident, ...] = ()
        self._timezone = "Asia/Shanghai"
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 24)
        root.setSpacing(16)
        heading_row = QHBoxLayout()
        heading_row.addWidget(
            PageHeading(
                "03 / INCIDENTS",
                "事故与恢复证据",
                "事故只在需要关注的边沿出现；先保留回执、定位原因，再考虑最小范围修复。",
            ),
            1,
        )
        self.open_badge = StatusBadge(StatusColor.GRAY, "0 个处理中")
        heading_row.addWidget(self.open_badge, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(heading_row)

        filter_row = QHBoxLayout()
        self.incident_filters = QButtonGroup(self)
        self.incident_filters.setExclusive(True)
        for key, label in (("OPEN", "当前事故"), ("ALL", "全部记录")):
            button = _segment(label)
            button.setProperty("filterKey", key)
            button.clicked.connect(self._populate)
            self.incident_filters.addButton(button)
            filter_row.addWidget(button)
            if key == "OPEN":
                button.setChecked(True)
        filter_row.addStretch(1)
        root.addLayout(filter_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.setObjectName("incidentList")
        self.list.currentItemChanged.connect(self._show_incident)
        splitter.addWidget(self.list)
        self.detail = QFrame()
        self.detail.setObjectName("detailPanel")
        detail_layout = QVBoxLayout(self.detail)
        detail_layout.setContentsMargins(20, 18, 20, 20)
        detail_layout.setSpacing(12)
        self.detail_badge = StatusBadge(StatusColor.GRAY, "选择事故")
        self.detail_id = QLabel("—")
        self.detail_id.setObjectName("eyebrow")
        self.detail_title = QLabel("选择左侧事故查看证据")
        self.detail_title.setObjectName("pageTitle")
        self.detail_title.setWordWrap(True)
        self.detail_scope = QLabel("—")
        self.detail_scope.setObjectName("mono")
        self.detail_time = QLabel("—")
        self.detail_time.setObjectName("helpText")
        self.detail_summary = QLabel("事故摘要将在这里显示。")
        self.detail_summary.setWordWrap(True)
        self.detail_summary.setObjectName("pageSubtitle")
        reason_heading = QLabel("REASON CODES")
        reason_heading.setObjectName("eyebrow")
        self.detail_reasons = QLabel("—")
        self.detail_reasons.setWordWrap(True)
        actions_heading = QLabel("建议动作 / RUNBOOK")
        actions_heading.setObjectName("eyebrow")
        self.detail_actions = QLabel("—")
        self.detail_actions.setWordWrap(True)
        self.detail_actions.setObjectName("helpText")
        detail_layout.addWidget(self.detail_badge)
        detail_layout.addWidget(self.detail_id)
        detail_layout.addWidget(self.detail_title)
        detail_layout.addWidget(self.detail_scope)
        detail_layout.addWidget(self.detail_time)
        detail_layout.addSpacing(6)
        detail_layout.addWidget(self.detail_summary)
        detail_layout.addSpacing(8)
        detail_layout.addWidget(reason_heading)
        detail_layout.addWidget(self.detail_reasons)
        detail_layout.addSpacing(8)
        detail_layout.addWidget(actions_heading)
        detail_layout.addWidget(self.detail_actions)
        detail_layout.addStretch(1)
        splitter.addWidget(self.detail)
        splitter.setSizes([340, 720])
        root.addWidget(splitter, 1)

    def set_snapshot(self, snapshot: DashboardSnapshot) -> None:
        self._incidents = snapshot.incidents
        self._timezone = snapshot.preferences.timezone
        open_count = sum(
            item.state is not IncidentState.RESOLVED for item in self._incidents
        )
        color = StatusColor.AMBER if open_count else StatusColor.GREEN
        self.open_badge.set_status(color, f"{open_count} 个处理中")
        self._populate()

    def _populate(self, *_args: object) -> None:
        checked = self.incident_filters.checkedButton()
        mode = str(checked.property("filterKey")) if checked else "OPEN"
        self.list.clear()
        visible = [
            incident
            for incident in self._incidents
            if mode == "ALL" or incident.state is not IncidentState.RESOLVED
        ]
        for incident in visible:
            label = (
                f"●  {incident.incident_id}  ·  "
                f"{INCIDENT_LABELS[incident.state]}\n{incident.title}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, incident)
            item.setToolTip(incident.scope)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._clear_detail()

    def _clear_detail(self) -> None:
        self.detail_badge.set_status(StatusColor.GREEN, "当前无开放事故")
        self.detail_id.setText("INCIDENT LEDGER")
        self.detail_title.setText("没有需要处理的事故")
        self.detail_scope.setText("—")
        self.detail_time.setText("—")
        self.detail_summary.setText(
            "没有事故只说明账本当前没有开放边沿；仍应以 Overview 四问判断可信沉默。"
        )
        self.detail_reasons.setText("—")
        self.detail_actions.setText("• 保持 Guardian 运行并观察下一预期窗口")

    def _show_incident(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            return
        incident = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(incident, Incident):
            return
        self.detail_badge.set_status(
            incident.color, INCIDENT_LABELS[incident.state]
        )
        self.detail_id.setText(incident.incident_id)
        self.detail_title.setText(incident.title)
        self.detail_scope.setText(incident.scope)
        self.detail_time.setText(
            f"首次 {format_time(incident.opened_at, self._timezone)}  ·  "
            f"更新 {format_time(incident.updated_at, self._timezone)}"
        )
        self.detail_summary.setText(incident.summary)
        self.detail_reasons.setText(
            text_lines(reason_label(code) for code in incident.reason_codes)
        )
        self.detail_actions.setText(text_lines(incident.next_actions))


class ProvidersPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("providersPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 24)
        root.setSpacing(16)
        heading_row = QHBoxLayout()
        heading_row.addWidget(
            PageHeading(
                "04 / PROVIDERS",
                "外部能力隔离",
                "每一行是稳定的 provider × operation × market 能力，不把 ticker、URL 或异常正文做成标签。",
            ),
            1,
        )
        self.summary = StatusBadge(StatusColor.GRAY, "等待样本")
        heading_row.addWidget(self.summary, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(heading_row)
        self.table = QTableWidget(0, 8)
        self.table.setObjectName("providersTable")
        self.table.setHorizontalHeaderLabels(
            ["等级", "Provider", "Operation", "Market", "真实样本", "成功率", "Wilson 95% 下界", "Circuit"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 126)
        self.table.setColumnWidth(7, 145)
        root.addWidget(self.table, 1)
        notice = QFrame()
        notice.setObjectName("noticePanel")
        notice_layout = QGridLayout(notice)
        notice_layout.setContentsMargins(14, 11, 14, 11)
        title = QLabel("为什么同时看样本数、成功率和 Wilson 下界？")
        title.setObjectName("panelTitle")
        body = QLabel(
            "少量成功不能证明稳定。健康等级只统计真实网络尝试；fresh cache hit 不增加 sample count。熔断开启时该能力快速拒绝，不拖垮其他市场。"
        )
        body.setObjectName("helpText")
        body.setWordWrap(True)
        notice_layout.addWidget(title, 0, 0)
        notice_layout.addWidget(body, 1, 0)
        root.addWidget(notice)

    def set_snapshot(self, snapshot: DashboardSnapshot) -> None:
        capabilities = snapshot.providers
        attention = sum(
            item.color in {StatusColor.RED, StatusColor.AMBER}
            for item in capabilities
        )
        self.summary.set_status(
            StatusColor.AMBER if attention else StatusColor.GREEN,
            f"{attention} 项需关注" if attention else "全部能力正常",
        )
        self.table.setRowCount(len(capabilities))
        for row, capability in enumerate(capabilities):
            self.table.setRowHeight(row, 48)
            self.table.setCellWidget(
                row,
                0,
                _badge_cell(
                    capability.color,
                    GRADE_LABELS.get(capability.grade, capability.grade),
                ),
            )
            self.table.setItem(
                row, 1, _table_item(capability.provider, mono=True)
            )
            self.table.setItem(
                row, 2, _table_item(capability.operation, mono=True)
            )
            self.table.setItem(row, 3, _table_item(capability.market, mono=True))
            self.table.setItem(
                row, 4, _table_item(str(capability.sample_count), mono=True)
            )
            self.table.setItem(
                row,
                5,
                _table_item(format_percent(capability.success_rate), mono=True),
            )
            self.table.setItem(
                row,
                6,
                _table_item(
                    format_percent(capability.wilson_lower_bound), mono=True
                ),
            )
            circuit_color = {
                "closed": StatusColor.GREEN,
                "open": StatusColor.RED,
                "half_open": StatusColor.BLUE,
            }.get(capability.circuit_state, StatusColor.GRAY)
            self.table.setCellWidget(
                row,
                7,
                _badge_cell(
                    circuit_color,
                    CIRCUIT_LABELS.get(
                        capability.circuit_state, capability.circuit_state
                    ),
                ),
            )


class ChannelCard(QFrame):
    test_requested = Signal(object)

    def __init__(
        self, kind: ChannelKind, title: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.kind = kind
        self.setObjectName("channelCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 15, 16, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.title = QLabel(title)
        self.title.setObjectName("panelTitle")
        self.badge = StatusBadge(StatusColor.GRAY, "未配置")
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.badge)
        self.recipient = QLabel("目标：未公开")
        self.recipient.setObjectName("mono")
        self.last_success = QLabel("最近成功：尚无证据")
        self.last_success.setObjectName("helpText")
        self.error = QLabel("错误码：无")
        self.error.setObjectName("helpText")
        self.test_button = QPushButton("发送测试回执")
        self.test_button.setProperty("quiet", True)
        self.test_button.clicked.connect(
            lambda: self.test_requested.emit(self.kind)
        )
        if kind is ChannelKind.EXTERNAL_WATCHER:
            self.test_button.setVisible(False)
        layout.addLayout(header)
        layout.addWidget(self.recipient)
        layout.addWidget(self.last_success)
        layout.addWidget(self.error)
        layout.addStretch(1)
        layout.addWidget(self.test_button, 0, Qt.AlignmentFlag.AlignLeft)

    def set_channel(self, channel: DeliveryChannel, timezone: str) -> None:
        self.title.setText(channel.label)
        detail = {
            StatusColor.GREEN: "已验证",
            StatusColor.RED: "不可用",
            StatusColor.BLUE: "待验证",
            StatusColor.AMBER: "局部退化",
            StatusColor.GRAY: "预览 / 未配置",
        }[channel.color]
        self.badge.set_status(channel.color, detail)
        self.recipient.setText(f"目标：{channel.recipient_hint}")
        self.last_success.setText(
            f"最近成功：{format_time(channel.last_success_at, timezone)}"
        )
        self.error.setText(f"错误码：{channel.error_code or '无'}")
        self.test_button.setEnabled(channel.configured)


class SettingsPage(QWidget):
    test_channel_requested = Signal(object)
    save_requested = Signal(object, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._revision = 0
        self._preferences: Preferences | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(26, 24, 26, 30)
        layout.setSpacing(18)
        layout.addWidget(
            PageHeading(
                "05 / SETTINGS + CHANNELS",
                "Guardian 与移动提醒",
                "桌面 App 只编辑公开偏好并请求 Guardian 测试通道；token、Chat ID、手机号凭据与 SQLite 永不进入界面。",
            )
        )

        security = QFrame()
        security.setObjectName("noticePanel")
        security_layout = QHBoxLayout(security)
        security_layout.setContentsMargins(14, 10, 14, 10)
        shield = StatusBadge(StatusColor.GREEN, "密钥隔离")
        security_text = QLabel(
            "凭据由后台 Guardian 管理。本界面只显示脱敏目标和低基数错误码，无法查看或复制密钥。"
        )
        security_text.setWordWrap(True)
        security_text.setObjectName("helpText")
        security_layout.addWidget(shield)
        security_layout.addWidget(security_text, 1)
        layout.addWidget(security)

        channels_heading = QLabel("移动提醒与外部存活证明")
        channels_heading.setObjectName("panelTitle")
        layout.addWidget(channels_heading)
        channels_grid = QGridLayout()
        channels_grid.setHorizontalSpacing(12)
        channels_grid.setVerticalSpacing(12)
        self.channel_cards = {
            ChannelKind.TELEGRAM: ChannelCard(
                ChannelKind.TELEGRAM, "Telegram Bot"
            ),
            ChannelKind.WHATSAPP: ChannelCard(
                ChannelKind.WHATSAPP, "WhatsApp Cloud API"
            ),
            ChannelKind.EXTERNAL_WATCHER: ChannelCard(
                ChannelKind.EXTERNAL_WATCHER, "外部 Guardian Watcher"
            ),
        }
        for column, card in enumerate(self.channel_cards.values()):
            card.test_requested.connect(self.test_channel_requested)
            channels_grid.addWidget(card, 0, column)
            channels_grid.setColumnStretch(column, 1)
        layout.addLayout(channels_grid)

        settings = QFrame()
        settings.setObjectName("settingsCard")
        settings_layout = QGridLayout(settings)
        settings_layout.setContentsMargins(18, 16, 18, 18)
        settings_layout.setHorizontalSpacing(24)
        settings_layout.setVerticalSpacing(13)
        title = QLabel("桌面与值班偏好")
        title.setObjectName("panelTitle")
        subtitle = QLabel("这些偏好不包含规则阈值、通道凭据或数据库内容。")
        subtitle.setObjectName("helpText")
        self.launch_at_login = QCheckBox("登录后启动 Guardian")
        self.quiet_enabled = QCheckBox("启用免打扰时段")
        self.quiet_start = QTimeEdit()
        self.quiet_start.setDisplayFormat("HH:mm")
        self.quiet_end = QTimeEdit()
        self.quiet_end.setDisplayFormat("HH:mm")
        self.timezone_value = QLabel("Asia/Shanghai")
        self.timezone_value.setObjectName("mono")
        self.save_button = QPushButton("保存公开偏好")
        self.save_button.setObjectName("saveSettingsButton")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self._save)
        settings_layout.addWidget(title, 0, 0, 1, 2)
        settings_layout.addWidget(subtitle, 1, 0, 1, 2)
        settings_layout.addWidget(self.launch_at_login, 2, 0)
        settings_layout.addWidget(self.quiet_enabled, 3, 0)
        quiet_row = QHBoxLayout()
        quiet_row.addWidget(self.quiet_start)
        quiet_row.addWidget(QLabel("至"))
        quiet_row.addWidget(self.quiet_end)
        quiet_row.addStretch(1)
        settings_layout.addLayout(quiet_row, 3, 1)
        settings_layout.addWidget(QLabel("显示时区"), 4, 0)
        settings_layout.addWidget(self.timezone_value, 4, 1)
        settings_layout.addWidget(
            self.save_button, 5, 1, Qt.AlignmentFlag.AlignRight
        )
        settings_layout.setColumnStretch(1, 1)
        layout.addWidget(settings)
        layout.addStretch(1)
        root.addWidget(_scroll_page(content))

    def set_snapshot(self, snapshot: DashboardSnapshot) -> None:
        self._revision = snapshot.config_revision
        self._preferences = snapshot.preferences
        preferences = snapshot.preferences
        self.launch_at_login.setChecked(preferences.launch_at_login)
        self.quiet_enabled.setChecked(preferences.quiet_hours_enabled)
        self.quiet_start.setTime(QTime.fromString(preferences.quiet_hours_start, "HH:mm"))
        self.quiet_end.setTime(QTime.fromString(preferences.quiet_hours_end, "HH:mm"))
        self.timezone_value.setText(preferences.timezone)
        by_kind = {channel.kind: channel for channel in snapshot.channels}
        for kind, card in self.channel_cards.items():
            channel = by_kind.get(kind)
            if channel is not None:
                card.set_channel(channel, preferences.timezone)

    def _save(self) -> None:
        if self._preferences is None:
            return
        updated = self._preferences.with_updates(
            launch_at_login=self.launch_at_login.isChecked(),
            quiet_hours_enabled=self.quiet_enabled.isChecked(),
            quiet_hours_start=self.quiet_start.time().toString("HH:mm"),
            quiet_hours_end=self.quiet_end.time().toString("HH:mm"),
        )
        self.save_requested.emit(updated, self._revision)

    def set_busy(self, busy: bool) -> None:
        self.save_button.setEnabled(not busy)
        for card in self.channel_cards.values():
            if card.kind is not ChannelKind.EXTERNAL_WATCHER:
                card.test_button.setEnabled(not busy)
