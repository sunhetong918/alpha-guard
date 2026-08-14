"""Reusable cockpit widgets with accessible non-color status labels."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .formatters import status_label
from .models import StatusColor


def clear_layout(layout: QVBoxLayout | QHBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.deleteLater()
        elif child_layout is not None:
            clear_layout(child_layout)  # type: ignore[arg-type]


def repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class StatusBadge(QLabel):
    def __init__(
        self,
        color: StatusColor = StatusColor.GRAY,
        text: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("statusBadge")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.set_status(color, text)

    def set_status(self, color: StatusColor, text: str | None = None) -> None:
        readable = status_label(color, text)
        self.setProperty("statusColor", color.value)
        self.setText(f"●  {readable}")
        self.setAccessibleName(f"状态：{readable}")
        repolish(self)


class Meter(QWidget):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        row = QHBoxLayout()
        self.label = QLabel(label)
        self.label.setObjectName("metricLabel")
        self.value = QLabel("—")
        self.value.setObjectName("mono")
        row.addWidget(self.label)
        row.addStretch(1)
        row.addWidget(self.value)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        layout.addLayout(row)
        layout.addWidget(self.bar)

    def set_value(
        self, ratio: float | None, color: StatusColor = StatusColor.GREEN
    ) -> None:
        self.bar.setProperty("statusColor", color.value)
        self.bar.setValue(0 if ratio is None else round(ratio * 1000))
        self.value.setText("未知" if ratio is None else f"{ratio * 100:.1f}%")
        repolish(self.bar)


class PageHeading(QWidget):
    def __init__(
        self,
        code: str,
        title: str,
        subtitle: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        eyebrow = QLabel(code)
        eyebrow.setObjectName("eyebrow")
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        description = QLabel(subtitle)
        description.setObjectName("pageSubtitle")
        description.setWordWrap(True)
        layout.addWidget(eyebrow)
        layout.addWidget(heading)
        layout.addWidget(description)


class QuestionPanel(QFrame):
    def __init__(
        self,
        number: str,
        question: str,
        explanation: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("questionPanel")
        self.setMinimumHeight(214)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(18, 16, 18, 18)
        self.root.setSpacing(11)
        head = QHBoxLayout()
        number_label = QLabel(number)
        number_label.setObjectName("questionNumber")
        question_label = QLabel(question)
        question_label.setObjectName("panelTitle")
        question_label.setWordWrap(True)
        head.addWidget(number_label)
        head.addWidget(question_label, 1)
        self.root.addLayout(head)
        explainer = QLabel(explanation)
        explainer.setObjectName("helpText")
        explainer.setWordWrap(True)
        self.root.addWidget(explainer)
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setStyleSheet("color: #2B342D;")
        self.root.addWidget(rule)
        self.body = QVBoxLayout()
        self.body.setSpacing(9)
        self.root.addLayout(self.body, 1)


class KeyValueRow(QWidget):
    def __init__(
        self,
        label: str,
        value: str = "—",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.label = QLabel(label)
        self.label.setObjectName("muted")
        self.value = QLabel(value)
        self.value.setObjectName("mono")
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.label)
        layout.addStretch(1)
        layout.addWidget(self.value)


class StateLegend(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        title = QLabel("保护状态")
        title.setObjectName("eyebrow")
        layout.addWidget(title)
        for color, detail in (
            (StatusColor.GRAY, "未配置"),
            (StatusColor.GREEN, "健康"),
            (StatusColor.AMBER, "退化"),
            (StatusColor.RED, "失明"),
            (StatusColor.BLUE, "恢复"),
        ):
            layout.addWidget(StatusBadge(color, detail))
        layout.addStretch(1)


def text_lines(values: Iterable[str], empty: str = "暂无") -> str:
    lines = tuple(values)
    if not lines:
        return empty
    return "\n".join(f"• {value}" for value in lines)
