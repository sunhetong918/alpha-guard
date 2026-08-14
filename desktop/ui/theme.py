"""Industrial night-watch visual system for the desktop cockpit."""

from __future__ import annotations

import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


COLORS = {
    "canvas": "#0D100E",
    "rail": "#111612",
    "panel": "#151A16",
    "panel_raised": "#1A211B",
    "line": "#2B342D",
    "line_bright": "#3B473E",
    "text": "#EDF2EB",
    "muted": "#98A39A",
    "faint": "#6F7971",
    "gray": "#A0A8A1",
    "green": "#63D590",
    "amber": "#E8B45C",
    "red": "#F06B64",
    "blue": "#71A8FF",
    "black": "#080A09",
}


APP_QSS = f"""
* {{
    font-family: "Avenir Next Condensed";
    font-size: 13px;
    color: {COLORS["text"]};
}}
QMainWindow, QWidget#appRoot {{ background: {COLORS["canvas"]}; }}
QWidget {{ selection-background-color: #335941; selection-color: #FFFFFF; }}
QFrame#sidebar {{
    background: {COLORS["rail"]};
    border-right: 1px solid {COLORS["line"]};
}}
QFrame#topbar {{
    background: #101411;
    border-bottom: 1px solid {COLORS["line"]};
}}
QFrame#safetyFooter {{
    background: #0A0D0B;
    border-top: 1px solid {COLORS["line"]};
}}
QFrame#panel, QFrame#questionPanel, QFrame#receiptStrip,
QFrame#settingsCard, QFrame#channelCard, QFrame#detailPanel {{
    background: {COLORS["panel"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 3px;
}}
QFrame#questionPanel[accent="true"] {{
    border-top: 3px solid {COLORS["amber"]};
}}
QFrame#noticePanel {{
    background: #111A14;
    border: 1px solid #2C4835;
    border-left: 3px solid {COLORS["green"]};
    border-radius: 2px;
}}
QLabel#brandMark {{
    color: {COLORS["green"]};
    font-family: "Menlo";
    font-size: 21px;
    font-weight: 700;
    letter-spacing: 2px;
}}
QLabel#brandSub, QLabel#eyebrow, QLabel#mono, QLabel#receiptId,
QLabel#tableCode {{
    font-family: "Menlo";
}}
QLabel#brandSub, QLabel#eyebrow, QLabel#tableCode {{
    color: {COLORS["faint"]};
    font-size: 11px;
    letter-spacing: 1px;
}}
QLabel#pageTitle {{ font-size: 25px; font-weight: 650; }}
QLabel#pageSubtitle {{ color: {COLORS["muted"]}; font-size: 13px; }}
QLabel#panelTitle {{ font-size: 16px; font-weight: 650; }}
QLabel#questionNumber {{
    color: {COLORS["faint"]};
    font-family: "Menlo";
    font-size: 12px;
}}
QLabel#metricValue {{
    font-family: "Menlo";
    font-size: 25px;
    font-weight: 650;
}}
QLabel#metricLabel, QLabel#muted, QLabel#helpText {{
    color: {COLORS["muted"]};
}}
QLabel#helpText {{ font-size: 12px; line-height: 1.4; }}
QLabel#dangerText {{ color: {COLORS["red"]}; }}
QLabel#amberText {{ color: {COLORS["amber"]}; }}
QLabel#greenText {{ color: {COLORS["green"]}; }}
QLabel#mono {{ color: #C1CBC2; }}
QLabel[statusColor="GRAY"] {{ color: {COLORS["gray"]}; }}
QLabel[statusColor="GREEN"] {{ color: {COLORS["green"]}; }}
QLabel[statusColor="AMBER"] {{ color: {COLORS["amber"]}; }}
QLabel[statusColor="RED"] {{ color: {COLORS["red"]}; }}
QLabel[statusColor="BLUE"] {{ color: {COLORS["blue"]}; }}
QLabel#statusBadge {{
    font-family: "Menlo";
    font-size: 11px;
    font-weight: 650;
    padding: 4px 7px;
    background: #0C100D;
    border: 1px solid {COLORS["line_bright"]};
    border-radius: 2px;
}}
QPushButton {{
    min-height: 30px;
    padding: 0 13px;
    background: {COLORS["panel_raised"]};
    border: 1px solid {COLORS["line_bright"]};
    border-radius: 2px;
    font-weight: 600;
}}
QPushButton:hover {{ background: #222B24; border-color: #536156; }}
QPushButton:pressed {{ background: #101511; }}
QPushButton:disabled {{ color: #5E685F; border-color: #2A302B; }}
QPushButton[primary="true"] {{
    background: #DDE8DF;
    color: #111612;
    border-color: #F4F8F5;
}}
QPushButton[primary="true"]:hover {{ background: #FFFFFF; }}
QPushButton[quiet="true"] {{ background: transparent; }}
QPushButton[nav="true"] {{
    min-height: 42px;
    padding: 0 14px;
    text-align: left;
    color: {COLORS["muted"]};
    background: transparent;
    border: 1px solid transparent;
    border-left: 3px solid transparent;
    font-weight: 550;
}}
QPushButton[nav="true"]:hover {{ background: #171D18; color: {COLORS["text"]}; }}
QPushButton[nav="true"]:checked {{
    background: #1A221B;
    color: {COLORS["text"]};
    border-left-color: {COLORS["green"]};
}}
QPushButton[segment="true"] {{
    min-height: 27px;
    padding: 0 10px;
    color: {COLORS["muted"]};
    background: #101411;
}}
QPushButton[segment="true"]:checked {{
    color: {COLORS["text"]};
    background: #29332B;
    border-color: #536156;
}}
QLineEdit, QComboBox, QTimeEdit {{
    min-height: 31px;
    padding: 0 9px;
    background: #0F1310;
    border: 1px solid {COLORS["line_bright"]};
    border-radius: 2px;
}}
QLineEdit:focus, QComboBox:focus, QTimeEdit:focus {{
    border-color: {COLORS["green"]};
}}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 17px; height: 17px;
    border: 1px solid {COLORS["line_bright"]};
    background: #0F1310;
    border-radius: 2px;
}}
QCheckBox::indicator:checked {{
    background: {COLORS["green"]};
    border-color: {COLORS["green"]};
}}
QProgressBar {{
    min-height: 8px;
    max-height: 8px;
    background: #090C0A;
    border: 1px solid {COLORS["line"]};
    border-radius: 1px;
    text-align: center;
}}
QProgressBar::chunk {{ background: {COLORS["green"]}; }}
QProgressBar[statusColor="AMBER"]::chunk {{ background: {COLORS["amber"]}; }}
QProgressBar[statusColor="RED"]::chunk {{ background: {COLORS["red"]}; }}
QProgressBar[statusColor="BLUE"]::chunk {{ background: {COLORS["blue"]}; }}
QProgressBar[statusColor="GRAY"]::chunk {{ background: {COLORS["gray"]}; }}
QTableWidget, QListWidget {{
    background: {COLORS["panel"]};
    alternate-background-color: #121713;
    border: 1px solid {COLORS["line"]};
    gridline-color: {COLORS["line"]};
    outline: none;
}}
QTableWidget::item {{ padding: 8px 7px; border-bottom: 1px solid #242C26; }}
QTableWidget::item:selected, QListWidget::item:selected {{
    background: #263128;
    color: {COLORS["text"]};
}}
QHeaderView::section {{
    min-height: 32px;
    padding: 0 7px;
    background: #101411;
    color: {COLORS["muted"]};
    border: none;
    border-right: 1px solid {COLORS["line"]};
    border-bottom: 1px solid {COLORS["line_bright"]};
    font-family: "Menlo";
    font-size: 11px;
    font-weight: 600;
}}
QListWidget::item {{
    min-height: 54px;
    padding: 8px;
    border-bottom: 1px solid {COLORS["line"]};
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: #101411; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #39433B; min-height: 36px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QSplitter::handle {{ background: {COLORS["line"]}; width: 1px; }}
QToolTip {{
    background: #202721;
    color: {COLORS["text"]};
    border: 1px solid #48544A;
    padding: 5px;
}}
QStatusBar {{
    background: #101411;
    color: {COLORS["muted"]};
    border-top: 1px solid {COLORS["line"]};
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["canvas"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLORS["panel"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#121713"))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLORS["panel_raised"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#335941"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(palette)
    stylesheet = APP_QSS
    if sys.platform == "win32":
        stylesheet = stylesheet.replace("Avenir Next Condensed", "Bahnschrift")
        stylesheet = stylesheet.replace("Menlo", "Cascadia Mono")
    elif sys.platform.startswith("linux"):
        stylesheet = stylesheet.replace(
            "Avenir Next Condensed", "DejaVu Sans Condensed"
        )
        stylesheet = stylesheet.replace("Menlo", "DejaVu Sans Mono")
    app.setStyleSheet(stylesheet)
